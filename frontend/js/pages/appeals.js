/* Экран проверяющего: апелляции (#appeals, #appeals/<id>) */
CQA.pages = CQA.pages || {};
CQA.pages.appeals = async function (app, selId) {
  const { esc, art, tilt, parallax, toast, countUp } = CQA.ui;
  const S = CQA.scoring;
  app.innerHTML = `<div class="wrap"><div class="loading">Загружаю апелляции…</div></div>`;

  let appeals, checklist;
  try { [appeals, checklist] = await Promise.all([CQA.api.getAppeals(), CQA.api.getChecklist()]); }
  catch (e) { app.innerHTML = `<div class="wrap"><div class="empty"><h2>Не удалось загрузить апелляции</h2><p>${esc(e.message)}</p></div></div>`; return; }
  const defs = Object.fromEntries(checklist.criteria.map(c => [c.id, c]));
  const dialogs = {};
  let view = 'open', current = selId ? +selId : null, painted = false;
  if (current && appeals.find(a => a.id == current)?.status === 'resolved') view = 'resolved';

  app.innerHTML = `
  <section class="hero hero-sm"><div class="wrap">
    <div>
      <div class="crumb">Проверка оценок</div>
      <h1>Апелляции</h1>
      <p class="lead">Проверяющий видит спорную оценку, комментарий и фрагмент диалога — и подтверждает или меняет её. Итог пересчитывается автоматически.</p>
    </div>
    <div class="scene sm">
      <div class="obj" style="left:18%;top:18%;z-index:3" data-d="26"><div class="float">${art.bubble()}</div></div>
      <div class="obj" style="left:48%;top:30%;z-index:4" data-d="14"><div class="float">${art.shield()}</div></div>
      <div class="obj" style="right:8%;top:6%;z-index:2" data-d="20"><div class="float">${art.sphere()}</div></div>
      <div class="obj" style="right:16%;bottom:6%;z-index:4" data-d="18"><div class="float">${art.cube()}</div></div>
    </div>
  </div></section>
  <div class="wrap">
    <div class="bento">
      <div class="tile" data-v="open"><div class="v" id="a0">0</div><div class="l">на рассмотрении</div><div class="art">${art.bubble(.7)}</div></div>
      <div class="tile" data-v="resolved"><div class="v" id="a1">0</div><div class="l">решено</div><div class="art">${art.shield()}</div></div>
      <div class="tile" data-v="changed"><div class="v" id="a2">0</div><div class="l">оценка изменена</div><div class="art">${art.bars()}</div></div>
      <div class="tile" data-v="confirmed"><div class="v" id="a3">0</div><div class="l">оценка подтверждена</div><div class="art">${art.orb(false)}</div></div>
    </div>
    <main class="ap-grid">
      <section class="panel">
        <h2>Обращения <div class="seg" id="seg"><button data-v="open">Открытые</button><button data-v="resolved">Решённые</button><button data-v="all">Все</button></div></h2>
        <div id="alist"></div>
      </section>
      <section class="panel" id="detail"></section>
    </main>
  </div>`;

  const counts = () => [appeals.filter(a => a.status === 'open').length, appeals.filter(a => a.status === 'resolved').length,
                        appeals.filter(a => a.decision?.action === 'change').length, appeals.filter(a => a.decision?.action === 'confirm').length];
  let shownCounts = [0, 0, 0, 0];
  function renderStats() { counts().forEach((v, i) => { if (v !== shownCounts[i]) countUp(app.querySelector('#a' + i), shownCounts[i], v, 0, 600); }); shownCounts = counts(); }

  const matchView = a => view === 'all' || (view === 'open' && a.status === 'open') || (view === 'resolved' && a.status === 'resolved')
    || (view === 'changed' && a.decision?.action === 'change') || (view === 'confirmed' && a.decision?.action === 'confirm');
  const time = iso => iso ? new Date(iso).toLocaleString('ru-RU', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) : '';

  function renderList() {
    app.querySelectorAll('#seg button').forEach(b => b.classList.toggle('on', b.dataset.v === view));
    app.querySelectorAll('.tile').forEach(t => t.classList.toggle('on', t.dataset.v === view));
    const list = appeals.filter(matchView).sort((a, b) => b.id - a.id);
    const box = app.querySelector('#alist');
    if (!appeals.length) { box.innerHTML = `<div class="empty-mini">Пока нет апелляций.<br>Откройте диалог и нажмите «Оспорить» у критерия.<br><a href="#dialogs">К диалогам →</a></div>`; return; }
    if (!list.length) { box.innerHTML = `<div class="empty-mini">Здесь пусто</div>`; app.querySelector('#detail').innerHTML = '<div class="empty-mini">Выберите другой фильтр</div>'; return; }
    box.innerHTML = list.map((a, k) => {
      const c = defs[a.criterion_id] || { title: 'Критерий ' + a.criterion_id };
      const st = a.status === 'open' ? '<span class="pill part">На рассмотрении</span>'
        : a.decision.action === 'change' ? '<span class="pill zero">Изменена</span>' : '<span class="pill full">Подтверждена</span>';
      return `<button class="acard ${painted ? 'still' : ''} ${a.id === current ? 'on' : ''}" data-id="${a.id}" style="--k:${k}">
        <div class="ac-top"><b>№${a.id} · ${esc(a.dialog_id)}</b>${st}</div>
        <div class="ac-c">${esc(a.criterion_id)} ${esc(c.title)}</div>
        <div class="ac-q">«${esc(a.comment)}»</div>
        <div class="ac-t">${time(a.created_at)}</div></button>`;
    }).join(''); painted = true;
    if (!current && list[0]) select(list[0].id);
  }

  async function select(idv) {
    current = +idv;
    app.querySelectorAll('.acard').forEach(b => b.classList.toggle('on', +b.dataset.id === current));
    history.replaceState(null, '', '#appeals/' + current);
    const a = appeals.find(x => x.id === current), det = app.querySelector('#detail');
    if (!a) { det.innerHTML = '<div class="empty-mini">Выберите обращение слева</div>'; return; }
    det.innerHTML = '<div class="loading">Загружаю диалог…</div>';
    try { dialogs[a.dialog_id] = await CQA.api.getDialog(a.dialog_id); }
    catch (e) { det.innerHTML = `<div class="empty-mini">Не удалось загрузить диалог: ${esc(e.message)}</div>`; return; }
    renderDetail(a);
  }

  function fragment(d, ev) {
    const t = d.transcript, idx = ev ? t.findIndex(m => m.i === ev) : -1;
    const part = idx >= 0 ? t.slice(Math.max(0, idx - 2), idx + 3) : t.slice(0, 6);
    return `<div class="chat frag">${part.map(m => (m.s === 'bot' || m.s === 'system')
      ? `<div class="sys">${esc(m.x)} · ${esc(m.t)}</div>`
      : `<div class="msg ${m.s === 'operator' ? 'op' : ''} ${m.i === ev ? 'hl' : ''}"><div class="who">${m.s === 'operator' ? 'Оператор' : 'Клиент'} · #${m.i} · ${esc(m.t)}</div><div class="b">${esc(m.x)}</div></div>`).join('')}</div>
      ${idx < 0 ? '<div class="hint">В оценке нет ссылки на конкретную реплику — показано начало диалога.</div>' : ''}`;
  }

  function renderDetail(a) {
    const d = dialogs[a.dialog_id], c = d.criteria.find(x => x.id === a.criterion_id) || { ...defs[a.criterion_id], answer: a.original.answer, evidence: [] };
    const orig = { ...c, answer: a.original.answer };
    const det = app.querySelector('#detail');
    const head = `
      <div class="d-head">
        <div><div class="crumb"><a href="#dialog/${encodeURIComponent(a.dialog_id)}">${esc(a.dialog_id)}</a> · апелляция №${a.id} · ${time(a.created_at)}</div>
        <h3>${esc(c.id)} ${esc(c.title)}</h3></div>
        ${a.status === 'open' ? '<span class="pill part">На рассмотрении</span>' : '<span class="pill full">Решено</span>'}
      </div>
      <div class="d-two">
        <div class="box"><small>Исходная оценка</small>
          <div class="big"><span class="pill ${S.state(orig)}">${esc(S.fmt(c, a.original.answer))}</span>${a.original.points !== null && a.original.points !== undefined ? `<b>${a.original.points} <small>/ ${c.weight}</small></b>` : ''}</div>
          <p>${esc(a.original.explanation || c.why || '')}</p></div>
        <div class="box"><small>Комментарий к апелляции</small>
          <p class="cq">«${esc(a.comment)}»</p>
          ${a.proposed !== null && a.proposed !== undefined ? `<div class="prop">Предлагает: <span class="pill na">${esc(S.fmt(c, a.proposed))}</span></div>` : ''}</div>
      </div>
      <div class="box"><small>Фрагмент диалога</small>${fragment(d, (c.evidence || [])[0])}
        <a class="lnk" href="#dialog/${encodeURIComponent(a.dialog_id)}">Открыть весь диалог →</a></div>`;

    if (a.status === 'resolved') {
      det.innerHTML = head + `
        <div class="box decided"><small>Решение проверяющего</small>
          <div class="steps3">
            <div><small>Исходная оценка</small><b>${esc(S.fmt(c, a.original.answer))}</b></div>
            <div><small>Решение</small><b>${a.decision.action === 'confirm' ? 'Подтверждена' : 'Изменена на ' + esc(S.fmt(c, a.decision.answer))}</b></div>
            <div><small>Итог диалога</small><b>${a.original_total?.toFixed(2) ?? '—'} → ${a.final_total?.toFixed(2) ?? '—'}</b></div>
          </div>
          <p>Причина: ${esc(a.decision.reason)}</p></div>`;
      return;
    }

    const opts = S.options(c, d.channel);
    det.innerHTML = head + `
      <div class="box decide">
        <small>Решение</small>
        <div class="seg big-seg" id="act"><button class="on" data-a="confirm">Подтвердить оценку</button><button data-a="change">Изменить оценку</button></div>
        <div class="chg" id="chg">
          <label class="fl">Новая оценка</label>
          <div class="opts" id="newOpts">${opts.map(o => `<button data-o="${o}" class="${a.proposed !== null && o == a.proposed ? 'sel' : ''}">${esc(S.fmt(c, o))}</button>`).join('')}</div>
        </div>
        <label class="fl">Причина решения <small>(обязательно)</small></label>
        <textarea id="reason" placeholder="Например: по фрагменту видно, что оператор представился в первом ответе"></textarea>
        <div class="preview" id="preview"></div>
        <button class="btn btn-g" id="save">Сохранить решение</button>
      </div>`;

    let action = 'confirm';
    const preview = () => {
      const sel = det.querySelector('#newOpts [data-o].sel')?.dataset.o;
      const ans = action === 'change' && sel !== undefined ? (c.type === 'scale' && sel !== 'N/A' ? +sel : sel) : c.answer;
      const next = d.criteria.map(x => x.id === c.id ? { ...x, answer: ans } : x);
      const before = d.total, after = S.total(next).total;
      const diff = S.r2((after ?? 0) - (before ?? 0));
      det.querySelector('#preview').innerHTML = `Итог диалога: <b>${before?.toFixed(2) ?? '—'}</b> → <b>${after?.toFixed(2) ?? '—'}</b>
        ${diff ? `<span class="pill ${diff < 0 ? 'zero' : 'full'}">${diff > 0 ? '+' : ''}${diff.toFixed(2)}</span>` : '<span class="pill na">без изменений</span>'}`;
      return ans;
    };
    det.querySelector('#act').addEventListener('click', e => {
      const b = e.target.closest('button'); if (!b) return;
      action = b.dataset.a;
      det.querySelectorAll('#act button').forEach(x => x.classList.toggle('on', x === b));
      det.querySelector('#chg').classList.toggle('show', action === 'change');
      if (action === 'change' && !det.querySelector('#newOpts .sel')) det.querySelector(`#newOpts [data-o]:not([data-o="${c.answer}"])`)?.classList.add('sel');
      preview();
    });
    det.querySelector('#newOpts').addEventListener('click', e => {
      const o = e.target.closest('[data-o]'); if (!o) return;
      det.querySelectorAll('#newOpts [data-o]').forEach(x => x.classList.remove('sel')); o.classList.add('sel'); preview();
    });
    preview();
    det.querySelector('#save').onclick = async () => {
      const reason = det.querySelector('#reason').value.trim();
      if (!reason) { det.querySelector('#reason').classList.add('err'); det.querySelector('#reason').focus(); toast('Укажите причину решения', true); return; }
      const ans = preview(), btn = det.querySelector('#save');
      if (action === 'change' && ans == c.answer) { toast('Выберите оценку, отличную от исходной', true); return; }
      btn.disabled = true; btn.textContent = 'Сохраняю…';
      try {
        const res = await CQA.api.decideAppeal(a.id, { action, answer: action === 'change' ? ans : c.answer, reason });
        Object.assign(a, res.appeal); dialogs[a.dialog_id] = res.dialog;
      } catch (err) { toast('Не удалось сохранить: ' + err.message, true); btn.disabled = false; btn.textContent = 'Сохранить решение'; return; }
      toast(action === 'change' ? `Оценка изменена, итог ${a.original_total?.toFixed(2)} → ${a.final_total?.toFixed(2)}` : 'Оценка подтверждена');
      if (view === 'open') view = 'resolved';
      renderStats(); renderList(); renderDetail(a);
      det.querySelector('.decided')?.classList.add('flashbox');
    };
  }

  app.querySelector('#seg').addEventListener('click', e => { const b = e.target.closest('button'); if (b) { view = b.dataset.v; current = null; renderList(); } });
  app.querySelectorAll('.tile').forEach(t => t.addEventListener('click', () => { view = view === t.dataset.v ? 'all' : t.dataset.v; current = null; renderList(); }));
  app.querySelector('#alist').addEventListener('click', e => { const b = e.target.closest('.acard'); if (b) select(b.dataset.id); });

  tilt(app); parallax(app.querySelector('.hero')); renderStats(); renderList();
  if (current) select(current);
  else if (!appeals.length) app.querySelector('#detail').innerHTML = `<div class="empty-mini big">${art.shield()}<p>Когда кто-то оспорит оценку, обращение появится здесь.</p></div>`;
};
