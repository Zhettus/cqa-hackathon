/* Экран: карточка диалога (#dialog/<id>). Транскрипт с флагами реплик, оценка по критериям с доказательствами,
   подача апелляции. Все баллы и итоги — с бэкенда (scoring.py), фронт ничего не пересчитывает. */
CQA.pages = CQA.pages || {};
CQA.pages.dialog = async function (app, id) {
  const { esc, art, chev, ringHTML, ring, tilt, parallax, toast, reduce } = CQA.ui;
  const S = CQA.scoring, cfg = CQA.config;

  app.innerHTML = `<div class="wrap"><div class="loading">Загружаю диалог…</div></div>`;
  let d;
  try { d = await CQA.api.getDialog(id); }
  catch (e) { app.innerHTML = `<div class="wrap"><div class="empty"><h2>Не удалось открыть ${esc(id)}</h2><p>${esc(e.message)}</p><a class="btn btn-o" href="#dialogs">← К списку</a></div></div>`; return; }

  const appeals = d.appeals, sections = d.sections, isCall = d.channel === 'voice call';
  const replies = d.transcript.filter(m => m.s === 'client' || m.s === 'operator').length;
  const evaluated = d.status === 'evaluated';
  const totalChanged = d.final_total !== null && d.final_total !== undefined && d.total !== null && d.final_total !== d.total;
  let filter = null, first = true;

  const FLAG_RU = { ivr: 'IVR', client_not_hearing: 'клиент не слышит', after_end: 'после завершения', duplicate: 'дубль', dtmf: 'DTMF', asr_uncertain: 'ASR неуверенно' };
  const DIM = new Set(['ivr', 'client_not_hearing', 'after_end', 'duplicate']);
  const SEG_RU = { hold: 'удержание', consultation: 'консультация с коллегой', after_end: 'после завершения', ivr: 'IVR', pre_operator: 'до оператора' };
  const facts = d.facts || {};
  const factLine = [
    facts.first_response_s !== null && facts.first_response_s !== undefined ? `первая реакция ${Math.round(facts.first_response_s)} с` : null,
    facts.max_unwarned_gap_s ? `пауза без предупреждения ${Math.round(facts.max_unwarned_gap_s)} с` : null,
    facts.hold_s ? `удержание ${Math.round(facts.hold_s)} с` : null,
    facts.identity_verified_idx ? `идентификация #${facts.identity_verified_idx}` : 'идентификации нет',
  ].filter(Boolean).join(' · ');

  function message(m, k) {
    if (m.s === 'bot' || m.s === 'system')
      return `<div class="sys stag" style="animation-delay:${300 + k * 30}ms" id="m${m.i}" data-offset="${m.offset ?? ''}">#${m.i} · ${esc(m.x)} · ${esc(m.t)}</div>`;
    const dim = m.flags.some(f => DIM.has(f)) || (m.s === 'operator' && !m.counts);
    const flags = m.flags.map(f => `<span class="flag ${f === 'asr_uncertain' ? 'warn' : ''}">${esc(FLAG_RU[f] || f)}</span>`).join('');
    const seg = m.segment && SEG_RU[m.segment] && m.segment !== 'pre_operator' ? `<span class="flag">${esc(SEG_RU[m.segment])}</span>` : '';
    return `<div class="msg stag ${m.s === 'operator' ? 'op' : ''} ${dim ? 'dimmed' : ''} ${isCall && m.offset !== null && m.offset !== undefined ? 'seek' : ''}" style="animation-delay:${300 + k * 30}ms" id="m${m.i}" data-offset="${m.offset ?? ''}">
      <div class="who" ${isCall ? 'title="Перемотать плеер к этой реплике"' : ''}>${m.s === 'operator' ? 'Оператор' : 'Клиент'} · #${m.i} · ${esc(m.t)}${flags}${seg}</div>
      <div class="b"><span class="orig">${esc(m.x)}</span>${m.clean ? `<span class="cl">${esc(m.clean)}</span>` : ''}</div></div>`;
  }

  app.innerHTML = `
  <section class="hero"><div class="wrap">
    <div>
      <div class="crumb"><a href="#dialogs">Диалоги</a> → ${esc(d.id)}</div>
      <h1>${esc(d.id)}</h1>
      <p class="lead">${evaluated ? `Проверено по <b>${d.criteria.length} критериям</b> чек-листа v${d.checklist_version}.` : d.status === 'not_evaluable' ? 'Диалог не оценивается: нет реплик оператора.' : d.status === 'failed' ? 'Оценка завершилась ошибкой.' : 'Диалог ещё не оценён.'}</p>
      <div class="meta"><span>${isCall ? '📞 Звонок' : '💬 Чат'}</span><span>${esc(d.language)}</span><span>${replies} реплик</span>${d.transcript.length ? `<span>${esc(d.transcript.at(-1).t)}</span>` : ''}${d.model && d.model.name ? `<span title="модель">${esc(d.model.name)}</span>` : ''}</div>
      <div class="ro-note">${esc(factLine)}</div>
      ${totalChanged ? `<div class="note-chip">Итог изменён после апелляции · исходный ${d.total.toFixed(2)} → финальный ${d.final_total.toFixed(2)}</div>` : ''}
      ${d.status === 'failed' ? `<div class="st-note bad">${esc(d.status_reason || 'ошибка')}</div>` : ''}
      ${d.status === 'not_evaluable' ? `<div class="st-note">Итог = null. Реплик оператора нет (учтены удержание и реплики после завершения), модель не вызывалась.</div>` : ''}
      ${d.audio_url ? `<audio class="player" id="player" controls preload="none" src="${esc(cfg.API_BASE + d.audio_url)}"></audio><div class="ro-note">Нажмите на заголовок реплики, чтобы перемотать плеер.</div>` : ''}
      <div><button class="btn btn-g" id="rerun">Оценить заново</button>${evaluated ? '<button class="btn btn-o" id="toAppeal">Подать апелляцию</button>' : ''}</div>
    </div>
    <div class="scene">
      <div class="obj score" data-d="8"><div class="float"><div class="glass">${ringHTML('dl')}<div class="cap">${totalChanged ? 'Финальный балл' : 'Итоговый балл'}</div></div></div></div>
      <div class="obj" style="left:2%;top:10%;z-index:4" data-d="28"><div class="float">${art.bubble()}</div></div>
      <div class="obj" style="right:2%;top:4%;z-index:2" data-d="20"><div class="float">${art.sphere()}</div></div>
      <div class="obj" style="left:6%;bottom:6%;z-index:4" data-d="24"><div class="float">${art.langs()}</div></div>
      <div class="obj" style="right:6%;bottom:10%;z-index:4" data-d="16"><div class="float">${art.cube()}</div></div>
    </div>
  </div></section>

  <div class="wrap">
    <div class="bento">${sections.map((s, i) => `
      <div class="tile" data-sec="${i}" style="animation-delay:${80 + i * 70}ms">
        <div class="v" id="tv${i}"></div><div class="l">${esc(s)}</div>
        ${i < sections.length - 1 ? `<div class="mb"><i id="tb${i}"></i></div>` : ''}
        <div class="art" id="ta${i}">${[art.bubble(.7), art.shield(), art.bars(), art.orb(d.gross)][i] || art.cube()}</div>
      </div>`).join('')}</div>
    <main class="two">
      <section class="panel">
        <h2>Транскрипт <div class="seg" id="seg"><button class="on" data-v="all">Все реплики</button><button data-v="op">Только оператор</button>${isCall && d.transcript.some(m => m.clean) ? '<button data-v="clean">Очищенный текст</button>' : ''}</div></h2>
        <div class="chat" id="chat">${d.transcript.map(message).join('')}</div>
      </section>
      <section class="panel">
        <h2>Оценка по критериям <button class="reset" id="reset">Показать все ✕</button></h2>
        <div id="crits"></div>
        <div class="foot"><span id="naNote"></span><span>Итог <b id="total2">—</b></span></div>
      </section>
    </main>
  </div>`;

  const setTotal = ring('dl');
  const fmtPts = v => v === null || v === undefined ? '—' : (Math.round(v * 100) / 100);

  function appealBlock(c) {
    const list = appeals.filter(a => a.criterion_id === c.id);
    if (!list.length) return '';
    return list.map(a => a.status === 'open'
      ? `<div class="hist open ${a.status_raw === 'in_review' ? 'inrev' : ''}"><b>Апелляция ${a.status_raw === 'in_review' ? 'на рассмотрении у проверяющего' : 'подана'}</b><span>«${esc(a.comment)}»</span>
           <a href="#appeals/${esc(a.id)}">Открыть у проверяющего →</a></div>`
      : `<div class="hist done">
           <div class="steps3">
             <div><small>Исходная оценка</small><b>${esc(S.fmt(c, a.original.answer))}</b></div>
             <div><small>Решение</small><b>${a.decision.action === 'confirm' ? 'Подтверждена' : 'Изменена на ' + esc(S.fmt(c, a.decision.answer))}</b></div>
             <div><small>Итог диалога</small><b>${a.original_total?.toFixed(2) ?? '—'} → ${a.final_total?.toFixed(2) ?? '—'}</b></div>
           </div>
           <span>Причина: ${esc(a.decision.reason)}${a.decision.reviewer ? ` · ${esc(a.decision.reviewer)}` : ''}</span></div>`).join('');
  }

  function evidenceBlock(c) {
    const parts = [];
    c.quotes.forEach(qt => {
      const m = d.transcript.find(x => x.i === qt.n);
      parts.push(`<div class="quote">«${esc(qt.text)}»<small>реплика #${qt.n}${m ? ' · ' + esc(m.t) : ''}${qt.model_quote && qt.model_quote !== qt.text ? ' · цитата модели: «' + esc(qt.model_quote) + '»' : ''}</small></div>`);
    });
    if (c.range) parts.push(`<div class="quote dim-q"><span class="range">Проверенный участок: реплики #${c.range.from_n}–#${c.range.to_n}</span></div>`);
    if (c.answer === 'N/A' && c.na_reason) parts.push(`<div class="quote dim-q"><span class="tag-na">Условие N/A</span> ${esc(c.na_reason)}</div>`);
    (c.guards || []).forEach(g => parts.push(`<div class="guard">Правило <b>${esc(g.id)}</b>: ${esc((g.conditions || []).join('; '))} → ответ модели ${esc(String(S.fmt(c, CQA.api.toUi(g.before))))} → <b>${esc(String(S.fmt(c, CQA.api.toUi(g.after))))}</b></div>`));
    return parts.join('');
  }

  function renderCrits() {
    const box = app.querySelector('#crits');
    if (!evaluated) { box.innerHTML = `<div class="empty-mini">${d.status === 'not_evaluable' ? 'Критерии не оцениваются: итог null.' : d.status === 'failed' ? 'Оценка не получена.' : 'Нажмите «Оценить заново», чтобы запустить оценку.'}</div>`; return; }
    const open = [...box.querySelectorAll('.crit.open')].map(e => e.dataset.id);
    let html = '', last = -1;
    const finalById = d.final ? Object.fromEntries(d.final.criteria.map(x => [x.criterion_id, x])) : {};
    d.criteria.forEach((c, k) => {
      if (c.section !== last) { last = c.section; html += `<div class="sec" data-sec="${c.section}">${esc((sections[c.section] || '').toUpperCase())}</div>`; }
      const st = S.state(c), firstQ = c.quotes[0];
      const ap = appeals.filter(a => a.criterion_id === c.id);
      const fin = finalById[c.id];
      const changed = fin && fin.source === 'appeal';
      const hasOpen = ap.some(a => a.status === 'open');
      const conf = c.confidence !== null && c.confidence !== undefined && c.votes && c.votes.length > 1 ? `<span class="conf" title="доля голосов за итог">${Math.round(c.confidence * 100)}%</span>` : '';
      html += `<div class="crit ${first ? 'stag' : ''} ${open.includes(c.id) ? 'open' : ''}" data-id="${c.id}" data-sec="${c.section}" ${st === 'na' ? 'data-na' : ''} style="animation-delay:${380 + k * 40}ms">
        <div class="head"><span class="n">${esc(c.id)}</span><span class="t">${esc(c.title)}${hasOpen ? ' <span class="dot-amber" title="Апелляция на рассмотрении"></span>' : ''}${conf}</span>
          <span class="pw">${changed ? `<span class="was">было: ${esc(S.fmt(c, c.answer))}</span><span class="pill ${S.state({ ...c, answer: CQA.api.toUi(fin.answer) })}">${esc(S.fmt(c, CQA.api.toUi(fin.answer)))}</span>` : `<span class="pill ${st}">${esc(S.label(c))}</span>`}</span>
          <span class="pts">${c.type === 'flag' ? '<small>без баллов</small>' : (changed ? fin.applicable : c.applicable) ? `${fmtPts(changed ? fin.points : c.points)} <small>/ ${c.weight}</small>` : '<small>N/A</small>'}</span>${chev}</div>
        <div class="body"><div><div class="in">
          ${c.answer === 'N/A' ? '<span class="tag-na">Почему N/A</span> ' : ''}${esc(c.why)}
          ${evidenceBlock(c)}
          ${appealBlock(c)}
          <div class="acts">${firstQ ? `<button class="btn-l" data-go="${firstQ.n}">Показать в диалоге</button>` : c.range ? `<button class="btn-l" data-go="${c.range.from_n}">К началу участка</button>` : ''}${!hasOpen ? `<button class="btn-l" data-appeal>Оспорить</button>` : ''}</div>
          <div class="appeal">
            <label class="fl">С чем вы не согласны?</label>
            <textarea data-comment placeholder="Например: оператор представился во втором сообщении, потому что первое отправил бот"></textarea>
            <label class="fl">Какая оценка, по-вашему, верна? <small>(необязательно)</small></label>
            <div class="opts">${S.options(c, d.channel).filter(o => o != c.answer).map(o => `<button data-o="${o}">${esc(S.fmt(c, o))}</button>`).join('')}</div>
            <div><button class="btn btn-g btn-sm" data-send>Отправить апелляцию</button></div>
          </div>
        </div></div></div></div>`;
    });
    box.innerHTML = html; first = false;
    if (filter !== null) setFilter(filter);
  }

  function renderBento() {
    const byId = Object.fromEntries((d.sectionScores || []).map(s => [s.title, s]));
    const finById = d.final ? Object.fromEntries(d.final.sections.map(s => [s.title, s])) : {};
    sections.forEach((title, i) => {
      const v = app.querySelector('#tv' + i); if (!v) return;
      if (i === sections.length - 1 && d.criteria.some(c => c.section === i && c.type === 'flag')) {
        const gross = d.final ? d.final.flags.some(f => f.answer === 'yes') : d.gross;
        v.textContent = evaluated ? (gross ? 'Есть' : 'Нет') : '—'; app.querySelector('#ta' + i).innerHTML = art.orb(gross); return;
      }
      const s = finById[title] || byId[title];
      v.innerHTML = !s ? '—' : s.score === null ? 'N/A' : `${fmtPts(s.points)}<small> / ${fmtPts(s.max_points)}</small>`;
      setTimeout(() => { const b = app.querySelector('#tb' + i); if (b && s) b.style.width = (s.max_points ? s.points / s.max_points * 100 : 0) + '%'; }, 300 + i * 120);
    });
  }
  function refresh() {
    const applicable = d.criteria.filter(c => c.applicable);
    const W = applicable.reduce((s, c) => s + c.weight, 0);
    app.querySelector('#naNote').textContent = !evaluated ? '' : W && W < 100 ? `Вес N/A перераспределён: применимо ${W} из 100` : W ? 'Все критерии применимы' : 'Нет применимых критериев — итог не рассчитывается';
    const shown = d.final_total ?? d.total;
    app.querySelector('#total2').textContent = shown === null || shown === undefined
      ? (evaluated || d.status === 'not_evaluable' ? 'null' : '—')
      : shown.toFixed(2) + (totalChanged ? ` (было ${d.total.toFixed(2)})` : '');
    renderBento();
  }
  function setFilter(f) {
    filter = f;
    app.querySelectorAll('.tile').forEach(t => t.classList.toggle('on', +t.dataset.sec === f));
    app.querySelectorAll('.crit,.sec').forEach(el => el.classList.toggle('dim', f !== null && +el.dataset.sec !== f));
    app.querySelector('#reset').classList.toggle('show', f !== null);
  }
  function goTo(n, flash = true) {
    app.querySelectorAll('.msg,.sys').forEach(m => m.classList.remove('hl', 'flash'));
    const m = app.querySelector('#m' + n); if (!m) return;
    m.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'center' });
    m.classList.add('hl'); if (flash) { void m.offsetWidth; m.classList.add('flash'); }
    seek(m);
  }
  function seek(m) {
    const player = app.querySelector('#player'); if (!player || !m) return;
    const off = m.dataset.offset; if (off === '' || off === undefined) return;
    const apply = () => { try { player.currentTime = +off; } catch (_) { /* браузер не дал перемотать */ } };
    if (player.readyState >= 1) { apply(); return; }
    player.addEventListener('loadedmetadata', apply, { once: true });   // preload="none": метаданных ещё нет
    player.preload = 'metadata'; player.load();
  }

  /* --- события --- */
  app.querySelectorAll('.tile').forEach(t => t.addEventListener('click', () => setFilter(filter === +t.dataset.sec ? null : +t.dataset.sec)));
  app.querySelector('#reset').onclick = () => setFilter(null);
  app.querySelector('#seg').addEventListener('click', e => {
    const b = e.target.closest('button'); if (!b) return;
    const chat = app.querySelector('#chat');
    if (b.dataset.v === 'clean') { b.classList.toggle('on'); chat.classList.toggle('show-clean', b.classList.contains('on')); return; }
    app.querySelectorAll('#seg button:not([data-v="clean"])').forEach(x => x.classList.remove('on')); b.classList.add('on');
    chat.classList.toggle('op-only', b.dataset.v === 'op');
  });
  app.querySelector('#chat').addEventListener('click', e => { const m = e.target.closest('.msg.seek'); if (m && e.target.closest('.who')) seek(m); });
  app.querySelector('#rerun').onclick = async () => {
    const btn = app.querySelector('#rerun'); btn.disabled = true; btn.textContent = 'Оцениваю…';
    try { await CQA.api.evaluateOne(d.id); toast('Оценка выполнена'); CQA.pages.dialog(app, id); }
    catch (e) { toast('Не удалось оценить: ' + e.message, true); btn.disabled = false; btn.textContent = 'Оценить заново'; }
  };
  const toAppeal = app.querySelector('#toAppeal');
  if (toAppeal) toAppeal.onclick = () => {
    const c = app.querySelector('.crit [data-appeal]')?.closest('.crit');
    if (!c) { toast('Нет критериев, которые можно оспорить'); return; }
    c.classList.add('open'); c.querySelector('.appeal').classList.add('on');
    c.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'center' });
    setTimeout(() => c.querySelector('[data-comment]').focus(), 400);
  };

  app.querySelector('#crits').addEventListener('click', async e => {
    const crit = e.target.closest('.crit'); if (!crit) return;
    const c = d.criteria.find(x => x.id === crit.dataset.id);
    if (e.target.closest('.head')) { crit.classList.toggle('open'); return; }
    const go = e.target.closest('[data-go]');
    if (go) { goTo(+go.dataset.go); return; }
    if (e.target.closest('[data-appeal]')) { crit.querySelector('.appeal').classList.toggle('on'); return; }
    const opt = e.target.closest('[data-o]');
    if (opt) { const was = opt.classList.contains('sel'); crit.querySelectorAll('[data-o]').forEach(b => b.classList.remove('sel')); if (!was) opt.classList.add('sel'); return; }
    const send = e.target.closest('[data-send]');
    if (send) {
      const ta = crit.querySelector('[data-comment]'), comment = ta.value.trim();
      if (!comment) { ta.classList.add('err'); ta.focus(); toast('Напишите комментарий к апелляции', true); return; }
      const sel = crit.querySelector('[data-o].sel')?.dataset.o;
      const proposed = sel === undefined ? null : (c.type === 'scale' && sel !== 'N/A' ? +sel : sel);
      send.disabled = true; send.textContent = 'Отправляю…';
      try {
        const a = await CQA.api.createAppeal({ evaluation_id: d.evaluation_id, criterion_id: c.id, comment, proposed_answer: proposed });
        a.original_total = d.total; a.final_total = d.final_total; appeals.push(a);
      } catch (err) { toast('Не удалось отправить: ' + err.message, true); send.disabled = false; send.textContent = 'Отправить апелляцию'; return; }
      renderCrits();
      app.querySelector(`.crit[data-id="${c.id}"]`).classList.add('open');
      toast('Апелляция отправлена проверяющему');
    }
  });

  tilt(app); parallax(app.querySelector('.hero'));
  renderCrits(); refresh();
  setTimeout(() => setTotal(d.final_total ?? d.total ?? 0), 350);
  const n = cfg.params.get('n');                                   // ?n=12 — показать реплику (ссылка из D365)
  if (n) setTimeout(() => goTo(+n), 600);
};
