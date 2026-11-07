/* Экран: аналитика по набору + сверка с эталоном (#analytics) */
CQA.pages = CQA.pages || {};
CQA.state = CQA.state || {};

CQA.pages.analytics = async function (app) {
  const { esc, art, ringHTML, ring, countUp, tilt, parallax, toast } = CQA.ui;
  const S = CQA.scoring, cfg = CQA.config;
  app.innerHTML = `<div class="wrap"><div class="loading">Считаю аналитику…</div></div>`;

  let results, appeals, checklist;
  try { [results, appeals, checklist] = await Promise.all([CQA.api.getAllResults(cfg.GROUP_ID), CQA.api.getAppeals(), CQA.api.getChecklist()]); }
  catch (e) { app.innerHTML = `<div class="wrap"><div class="empty"><h2>Не удалось загрузить данные</h2><p>${esc(e.message)}</p></div></div>`; return; }
  if (!results.length) { app.innerHTML = `<div class="wrap"><div class="empty"><h2>Пока нечего анализировать</h2><p>Оцените набор на экране «Диалоги».</p><a class="btn btn-o" href="#dialogs">К диалогам</a></div></div>`; return; }

  /* ---------- расчёты ---------- */
  const totals = results.map(r => r.total).filter(v => v !== null);
  const avg = totals.length ? S.r2(totals.reduce((s, v) => s + v, 0) / totals.length) : 0;
  const grossN = results.filter(r => r.gross).length;
  const decided = appeals.filter(a => a.status === 'resolved');
  const overturned = decided.filter(a => a.decision?.action === 'change').length;

  const crit = checklist.criteria.map(def => {
    const o = { def, full: 0, part: 0, zero: 0, na: 0, none: 0, fails: [] };
    results.forEach(r => {
      const c = r.criteria.find(x => x.id === def.id);
      if (!c || !S.evaluated(c)) { o.none++; return; }
      const st = S.state(c); o[st]++;
      if (st === 'zero' || st === 'part') o.fails.push({ id: r.id, label: S.label(c) });
    });
    o.n = o.full + o.part + o.zero + o.na;
    return o;
  });

  const byOp = {};
  results.forEach(r => {
    const k = r.operator || '—'; (byOp[k] = byOp[k] || { name: k, list: [] }).list.push(r);
  });
  const ops = Object.values(byOp).map(o => {
    const t = o.list.map(r => r.total).filter(v => v !== null);
    const weak = checklist.criteria.filter(d => d.type !== 'flag').map(d => {
      const ap = o.list.map(r => r.criteria.find(c => c.id === d.id)).filter(c => c && S.applicable(c));
      return { d, ratio: ap.length ? ap.reduce((s, c) => s + S.points(c) / c.weight, 0) / ap.length : 1 };
    }).sort((a, b) => a.ratio - b.ratio)[0];
    return { name: o.name, n: o.list.length, avg: t.length ? S.r2(t.reduce((s, v) => s + v, 0) / t.length) : null,
             gross: o.list.filter(r => r.gross).length, weak: weak && weak.ratio < 1 ? weak.d : null };
  }).sort((a, b) => (b.avg ?? -1) - (a.avg ?? -1));

  const buckets = [[0, 50, 'до 50'], [50, 60, '50–60'], [60, 70, '60–70'], [70, 80, '70–80'], [80, 90, '80–90'], [90, 100.01, '90–100']]
    .map(([a, b, l]) => ({ l, n: totals.filter(v => v >= a && v < b).length }));
  const bMax = Math.max(1, ...buckets.map(b => b.n));
  const langs = ['RU', 'KZ', 'EN'].map(l => {
    const t = results.filter(r => r.language === l).map(r => r.total).filter(v => v !== null);
    return { l, n: t.length, avg: t.length ? S.r2(t.reduce((s, v) => s + v, 0) / t.length) : null };
  }).filter(x => x.n);

  const pct = (a, n) => n ? Math.round(a / n * 100) : 0;

  /* ---------- разметка ---------- */
  app.innerHTML = `
  <section class="hero hero-sm"><div class="wrap">
    <div>
      <div class="crumb">Набор ${esc(cfg.GROUP_ID)} · ${results.length} оценённых диалогов</div>
      <h1>Аналитика</h1>
      <p class="lead">Где операторы теряют баллы, кто проседает и насколько оценки системы совпадают с эталоном.</p>
    </div>
    <div class="scene sm">
      <div class="obj score sm-score" data-d="8"><div class="float"><div class="glass small">${ringHTML('an', 56)}<div class="cap">Средний балл</div></div></div></div>
      <div class="obj" style="left:4%;top:12%;z-index:4" data-d="24"><div class="float">${art.bars()}</div></div>
      <div class="obj" style="right:4%;top:4%;z-index:2" data-d="18"><div class="float">${art.sphere()}</div></div>
      <div class="obj" style="right:8%;bottom:4%;z-index:4" data-d="16"><div class="float">${art.cube()}</div></div>
    </div>
  </div></section>

  <div class="wrap">
    <div class="bento">
      <div class="tile"><div class="v"><span id="k0">0</span><small> / ${results.length}</small></div><div class="l">диалогов оценено</div><div class="art">${art.bubble(.7)}</div></div>
      <div class="tile"><div class="v"><span id="k1">0</span><small>%</small></div><div class="l">с грубыми нарушениями · ${grossN}</div><div class="art">${art.orb(true)}</div></div>
      <div class="tile"><div class="v"><span id="k2">0</span></div><div class="l">апелляций решено</div><div class="art">${art.shield()}</div></div>
      <div class="tile"><div class="v"><span id="k3">0</span><small>%</small></div><div class="l">оценок изменено после апелляции</div><div class="art">${art.cube()}</div></div>
    </div>

    <section class="panel an-panel">
      <h2>Выполнение критериев <span class="h-note">нажмите на строку — покажем диалоги, где критерий не выполнен</span></h2>
      <div class="legend-row">
        <span><i class="lg full"></i>Выполнен</span><span><i class="lg part"></i>Частично</span><span><i class="lg zero"></i>Не выполнен / нарушение</span><span><i class="lg na"></i>N/A</span>
      </div>
      <div class="cbars" id="cbars">${crit.map((o, k) => {
        const seg = (key, cls, name) => o[key] ? `<i class="${cls}" style="flex:${o[key]}" data-tip="<b>${esc(o.def.id)} ${esc(o.def.title)}</b><br>${name}: ${o[key]} из ${o.n} (${pct(o[key], o.n)}%)">${pct(o[key], o.n) >= 12 ? pct(o[key], o.n) + '%' : ''}</i>` : '';
        return `<button class="cbar" data-id="${esc(o.def.id)}" style="--k:${k}">
          <span class="cb-n">${esc(o.def.id)}</span><span class="cb-t">${esc(o.def.title)}</span>
          <span class="cb-track">${o.n ? seg('full', 'full', o.def.type === 'flag' ? 'Нарушений нет' : 'Выполнен') + seg('part', 'part', 'Частично') + seg('zero', 'zero', o.def.type === 'flag' ? 'Нарушение' : 'Не выполнен') + seg('na', 'na', 'N/A') : '<em>не оценён</em>'}</span>
          <span class="cb-v">${o.def.type === 'flag' ? (o.zero ? `<b class="bad">${o.zero}</b> нар.` : '—') : `<b>${pct(o.full, o.n - o.na)}%</b>`}</span></button>
          <div class="cb-fails" data-for="${esc(o.def.id)}">${o.fails.length ? o.fails.map(f => `<a href="#dialog/${encodeURIComponent(f.id)}">${esc(f.id)} <span>${esc(f.label)}</span></a>`).join('') : '<span class="muted">Во всех диалогах выполнен</span>'}</div>`;
      }).join('')}</div>
    </section>

    <main class="an-two">
      <section class="panel">
        <h2>Операторы</h2>
        <div class="optable">
          <div class="oh"><span>ОПЕРАТОР</span><span>ДИАЛОГОВ</span><span>СРЕДНИЙ БАЛЛ</span><span>НАРУШЕНИЯ</span><span>СЛАБОЕ МЕСТО</span></div>
          ${ops.map((o, k) => `<div class="orow" style="--k:${k}">
            <span class="opn"><i class="av-s">${esc(o.name[0] || '?')}</i>${esc(o.name)}</span><span>${o.n}</span>
            <span class="sc"><b>${o.avg?.toFixed(2) ?? '—'}</b><span class="mini"><i style="width:0" data-w="${o.avg ?? 0}"></i></span></span>
            <span>${o.gross ? `<span class="pill zero">${o.gross}</span>` : '<span class="muted">—</span>'}</span>
            <span class="weak">${o.weak ? esc(o.weak.id + ' ' + o.weak.title) : '<span class="muted">—</span>'}</span></div>`).join('')}
        </div>
      </section>
      <section class="panel">
        <h2>Распределение итоговых баллов</h2>
        <div class="hist-chart">${buckets.map(b => `<div class="hc" data-tip="Балл ${b.l}: <b>${b.n}</b> диалогов"><span class="hv">${b.n || ''}</span><i style="height:0" data-h="${b.n / bMax * 100}"></i><span class="hl">${b.l}</span></div>`).join('')}</div>
        <h2 class="mt">По языкам</h2>
        <div class="langbars">${langs.map(x => `<div class="lb" data-tip="${x.l}: средний балл <b>${x.avg?.toFixed(2)}</b> · ${x.n} диалогов"><span class="lng">${x.l}</span><span class="mini big"><i style="width:0" data-w="${x.avg ?? 0}"></i></span><b>${x.avg?.toFixed(2) ?? '—'}</b><small>${x.n} дл.</small></div>`).join('')}</div>
      </section>
    </main>

    <section class="panel gt-panel" id="gt">
      <div class="gt-head">
        <div><h2>Сверка с эталоном <span class="h-note">Ground Truth</span></h2>
          <p class="muted">Загрузите размеченные вручную оценки — покажем, насколько ответы системы совпадают с эталоном по каждому критерию. Так команда проверяет точность модели до контрольного этапа.</p></div>
        <div class="gt-acts">
          <label class="btn btn-o file-btn">Загрузить эталон<input type="file" id="gtFile" accept=".json,.csv"></label>
          ${CQA.api.demoGroundTruth ? '<button class="btn btn-g" id="gtDemo">Демо-эталон</button>' : ''}
          <button class="btn-l" id="gtFmt">Формат файла</button>
        </div>
      </div>
      <pre class="gt-fmt" id="fmt">JSON:
[{"conversation_id": "p1-ru-001", "criteria": {"01": "Да", "02": "N/A", "04": 15, "08": "Нет"}}]

или CSV:
conversation_id,criterion_id,answer
p1-ru-001,01,Да
p1-ru-001,04,15</pre>
      <div id="gtRes"></div>
    </section>
  </div>`;

  /* ---------- анимации ---------- */
  ring('an', 56)(avg);
  countUp(app.querySelector('#k0'), 0, results.length, 0, 700);
  countUp(app.querySelector('#k1'), 0, pct(grossN, results.length), 0, 700);
  countUp(app.querySelector('#k2'), 0, decided.length, 0, 700);
  countUp(app.querySelector('#k3'), 0, pct(overturned, decided.length), 0, 700);
  requestAnimationFrame(() => setTimeout(() => {
    app.querySelectorAll('[data-w]').forEach(i => i.style.width = i.dataset.w + '%');
    app.querySelectorAll('[data-h]').forEach(i => i.style.height = Math.max(+i.dataset.h, 2) + '%');
    app.querySelectorAll('.cb-track').forEach(t => t.classList.add('grown'));
  }, 150));

  app.querySelector('#cbars').addEventListener('click', e => {
    const b = e.target.closest('.cbar'); if (!b) return;
    const box = app.querySelector(`.cb-fails[data-for="${b.dataset.id}"]`), open = box.classList.contains('show');
    app.querySelectorAll('.cb-fails').forEach(x => x.classList.remove('show')); app.querySelectorAll('.cbar').forEach(x => x.classList.remove('on'));
    if (!open) { box.classList.add('show'); b.classList.add('on'); }
  });

  /* ---------- сверка с эталоном ---------- */
  const norm = v => v === null || v === undefined ? '' : String(v).trim().toLowerCase().replace(/^yes$/, 'да').replace(/^no$/, 'нет').replace(/^na$|^n\/a$|^н\/д$/, 'n/a');
  function parse(text, name) {
    if (/\.csv$/i.test(name) || /^conversation_id\s*,/i.test(text.trim())) {
      const rows = text.trim().split(/\r?\n/).slice(1).map(l => l.split(',').map(x => x.trim()));
      const map = {};
      rows.forEach(([d, c, a]) => { if (d && c) (map[d] = map[d] || {})[c.padStart(2, '0')] = a; });
      return Object.entries(map).map(([conversation_id, criteria]) => ({ conversation_id, criteria }));
    }
    let j = JSON.parse(text); if (!Array.isArray(j)) j = j.records || j.data || [];
    return j.map(r => ({ conversation_id: r.conversation_id || r.id,
      criteria: Array.isArray(r.criteria) ? Object.fromEntries(r.criteria.map(c => [String(c.id), c.answer])) : (r.criteria || {}) }));
  }
  function compare(gt) {
    const per = Object.fromEntries(checklist.criteria.map(d => [d.id, { d, ok: 0, n: 0 }]));
    const miss = []; let dialogs = 0, exact = 0;
    gt.forEach(g => {
      const r = results.find(x => x.id === g.conversation_id); if (!r) return;
      dialogs++; let all = true;
      Object.entries(g.criteria).forEach(([cid, ans]) => {
        const p = per[cid]; const c = r.criteria.find(x => x.id === cid); if (!p || !c || !S.evaluated(c)) return;
        p.n++;
        if (norm(c.answer) === norm(ans)) p.ok++; else { all = false; miss.push({ id: r.id, c: p.d, sys: S.label(c), gt: S.fmt(p.d, ans) }); }
      });
      if (all) exact++;
    });
    const ok = Object.values(per).reduce((s, p) => s + p.ok, 0), n = Object.values(per).reduce((s, p) => s + p.n, 0);
    return { per: Object.values(per).filter(p => p.n), miss, dialogs, exact, acc: n ? S.r2(ok / n * 100) : 0, n };
  }
  function renderGT(gt, src) {
    const r = compare(gt), box = app.querySelector('#gtRes');
    if (!r.n) { box.innerHTML = '<div class="empty-mini">В файле нет совпадающих диалогов или критериев. Проверьте conversation_id и номера критериев.</div>'; return; }
    box.innerHTML = `
      <div class="gt-sum">
        <div class="gt-big"><b id="gtAcc">0</b><small>%</small><span>совпадение по критериям</span></div>
        <div class="gt-facts"><div><b>${r.dialogs}</b><span>диалогов в эталоне</span></div><div><b>${r.n}</b><span>ответов сравнено</span></div>
          <div><b>${r.exact}</b><span>диалогов совпали полностью</span></div><div><b>${r.miss.length}</b><span>расхождений</span></div></div>
        <div class="gt-src muted">Источник: ${esc(src)}</div>
      </div>
      <div class="gt-bars">${r.per.map(p => { const v = Math.round(p.ok / p.n * 100); return `
        <div class="gb" data-tip="<b>${esc(p.d.id)} ${esc(p.d.title)}</b><br>совпало ${p.ok} из ${p.n}">
          <span class="cb-n">${esc(p.d.id)}</span><span class="cb-t">${esc(p.d.title)}</span>
          <span class="mini big"><i style="width:0;background:${v >= 90 ? 'var(--green-d)' : v >= 75 ? 'var(--amber)' : 'var(--red)'}" data-w="${v}"></i></span>
          <b>${v}%</b><span class="gb-st">${v >= 90 ? '✓ хорошо' : v >= 75 ? '△ проверить' : '✕ слабо'}</span></div>`; }).join('')}</div>
      ${r.miss.length ? `<h3 class="gt-h3">Расхождения <span class="muted">— откройте диалог и сравните с объяснением модели</span></h3>
      <div class="miss">${r.miss.slice(0, 40).map(m => `<a href="#dialog/${encodeURIComponent(m.id)}"><b>${esc(m.id)}</b><span>${esc(m.c.id)} ${esc(m.c.title)}</span>
        <span class="vs"><span class="pill na">система: ${esc(m.sys)}</span><span class="pill full">эталон: ${esc(m.gt)}</span></span></a>`).join('')}</div>
        ${r.miss.length > 40 ? `<div class="muted">и ещё ${r.miss.length - 40}</div>` : ''}` : '<div class="empty-mini">Расхождений нет 🎯</div>'}`;
    countUp(app.querySelector('#gtAcc'), 0, r.acc, 1, 900);
    setTimeout(() => box.querySelectorAll('[data-w]').forEach(i => i.style.width = i.dataset.w + '%'), 80);
    CQA.state.gt = { gt, src };
  }
  app.querySelector('#gtFile').addEventListener('change', async e => {
    const f = e.target.files[0]; if (!f) return;
    try { renderGT(parse(await f.text(), f.name), f.name); toast('Эталон загружен'); }
    catch (err) { toast('Не удалось прочитать файл: ' + err.message, true); }
    e.target.value = '';
  });
  app.querySelector('#gtDemo')?.addEventListener('click', () => renderGT(CQA.api.demoGroundTruth(), 'демо-эталон (тестовые данные)'));
  app.querySelector('#gtFmt').onclick = () => app.querySelector('#fmt').classList.toggle('show');
  if (CQA.state.gt) renderGT(CQA.state.gt.gt, CQA.state.gt.src);

  tilt(app); parallax(app.querySelector('.hero'));
};
