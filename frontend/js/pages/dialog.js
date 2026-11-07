/* Экран: карточка диалога (#dialog/<id>). Здесь оператор/пользователь ПОДАЁТ апелляцию. */
CQA.pages = CQA.pages || {};
CQA.pages.dialog = async function (app, id) {
  const { esc, art, chev, ringHTML, ring, tilt, parallax, toast, reduce } = CQA.ui;
  const S = CQA.scoring;

  app.innerHTML = `<div class="wrap"><div class="loading">Загружаю диалог…</div></div>`;
  let d, appeals;
  try { [d, appeals] = await Promise.all([CQA.api.getDialog(id), CQA.api.getAppeals()]); appeals = appeals.filter(a => a.dialog_id === id); }
  catch (e) { app.innerHTML = `<div class="wrap"><div class="empty"><h2>Не удалось открыть ${esc(id)}</h2><p>${esc(e.message)}</p><a class="btn btn-o" href="#dialogs">← К списку</a></div></div>`; return; }

  const sections = d.sections, isCall = d.channel === 'voice call';
  const replies = d.transcript.filter(m => m.s === 'client' || m.s === 'operator').length;
  const resolved = appeals.filter(a => a.status === 'resolved' && a.decision?.action === 'change');
  const firstTotal = resolved.length ? resolved[0].original_total : null;
  let filter = null, first = true;

  app.innerHTML = `
  <section class="hero"><div class="wrap">
    <div>
      <div class="crumb"><a href="#dialogs">Диалоги</a> → ${esc(d.id)}</div>
      <h1>${esc(d.id)}</h1>
      <p class="lead">${d.operator ? `Оператор <b>${esc(d.operator)}</b> · ` : ''}проверено по <b>${d.criteria.length} критериям</b> чек-листа.</p>
      <div class="meta"><span>${isCall ? '📞 Звонок' : '💬 Чат'}</span><span>${esc(d.language)}</span><span>${replies} реплик</span>${d.transcript.length ? `<span>${esc(d.transcript.at(-1).t)}</span>` : ''}${d.checklist_version ? `<span>чек-лист v${d.checklist_version}</span>` : ''}</div>
      ${firstTotal !== null ? `<div class="note-chip">Итог изменён после апелляции · исходный ${firstTotal.toFixed(2)}</div>` : ''}
      ${d.audio_url ? `<audio class="player" controls src="${esc(d.audio_url)}"></audio>` : ''}
      <div><button class="btn btn-g" id="rerun">Обновить</button><button class="btn btn-o" id="toAppeal">Подать апелляцию</button></div>
    </div>
    <div class="scene">
      <div class="obj score" data-d="8"><div class="float"><div class="glass">${ringHTML('dl')}<div class="cap">Итоговый балл</div></div></div></div>
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
        <h2>Транскрипт <div class="seg" id="seg"><button class="on" data-v="all">Все реплики</button><button data-v="op">Только оператор</button></div></h2>
        <div class="chat" id="chat">${d.transcript.map((m, k) => (m.s === 'bot' || m.s === 'system')
          ? `<div class="sys stag" style="animation-delay:${300 + k * 30}ms" id="m${m.i}">${esc(m.x)} · ${esc(m.t)}</div>`
          : `<div class="msg stag ${m.s === 'operator' ? 'op' : ''}" style="animation-delay:${300 + k * 30}ms" id="m${m.i}">
               <div class="who">${m.s === 'operator' ? 'Оператор' : 'Клиент'} · #${m.i} · ${esc(m.t)}</div><div class="b">${esc(m.x)}</div></div>`).join('')}</div>
      </section>
      <section class="panel">
        <h2>Оценка по критериям <button class="reset" id="reset">Показать все ✕</button></h2>
        <div id="crits"></div>
        <div class="foot"><span id="naNote"></span><span>Итог <b id="total2">—</b></span></div>
      </section>
    </main>
  </div>`;

  const setTotal = ring('dl');

  function appealBlock(c) {
    const list = appeals.filter(a => a.criterion_id === c.id);
    if (!list.length) return '';
    return list.map(a => a.status === 'open'
      ? `<div class="hist open"><b>Апелляция №${a.id} на рассмотрении</b><span>«${esc(a.comment)}»</span>
           <a href="#appeals/${a.id}">Открыть у проверяющего →</a></div>`
      : `<div class="hist done">
           <div class="steps3">
             <div><small>Исходная оценка</small><b>${esc(S.fmt(c, a.original.answer))}</b></div>
             <div><small>Решение</small><b>${a.decision.action === 'confirm' ? 'Подтверждена' : 'Изменена на ' + esc(S.fmt(c, a.decision.answer))}</b></div>
             <div><small>Итог диалога</small><b>${a.original_total?.toFixed(2) ?? '—'} → ${a.final_total?.toFixed(2) ?? '—'}</b></div>
           </div>
           <span>Причина: ${esc(a.decision.reason)}</span></div>`).join('');
  }

  function renderCrits() {
    const box = app.querySelector('#crits');
    const open = [...box.querySelectorAll('.crit.open')].map(e => e.dataset.id);
    let html = '', last = -1;
    d.criteria.forEach((c, k) => {
      if (c.section !== last) { last = c.section; html += `<div class="sec" data-sec="${c.section}">${esc((sections[c.section] || '').toUpperCase())}</div>`; }
      const st = S.state(c), ev = c.evidence[0], q = ev && d.transcript.find(m => m.i === ev);
      const ap = appeals.filter(a => a.criterion_id === c.id);
      const changed = ap.find(a => a.status === 'resolved' && a.decision.action === 'change');
      const hasOpen = ap.some(a => a.status === 'open');
      html += `<div class="crit ${first ? 'stag' : ''} ${open.includes(c.id) ? 'open' : ''}" data-id="${c.id}" data-sec="${c.section}" ${st === 'na' ? 'data-na' : ''} style="animation-delay:${380 + k * 40}ms">
        <div class="head"><span class="n">${esc(c.id)}</span><span class="t">${esc(c.title)}${hasOpen ? ' <span class="dot-amber" title="Апелляция на рассмотрении"></span>' : ''}</span>
          <span class="pw">${changed ? `<span class="was">было: ${esc(S.fmt(c, changed.original.answer))}</span>` : ''}<span class="pill ${st}">${esc(S.label(c))}</span></span>
          <span class="pts">${c.type === 'flag' ? '<small>без баллов</small>' : S.applicable(c) ? `${S.r2(S.points(c))} <small>/ ${c.weight}</small>` : '<small>—</small>'}</span>${chev}</div>
        <div class="body"><div><div class="in">
          ${c.answer === 'N/A' ? '<span class="tag-na">Почему N/A</span> ' : ''}${esc(c.why)}
          ${q ? `<div class="quote">«${esc(q.x)}»<small>сообщение #${q.i} · ${esc(q.t)}</small></div>` : ''}
          ${!q && S.evaluated(c) && c.answer !== 'N/A' && c.type !== 'flag' ? `<div class="quote dim-q">Проверенный участок: весь диалог (#1–#${d.transcript.length})</div>` : ''}
          ${appealBlock(c)}
          <div class="acts">${q ? `<button class="btn-l" data-go="${q.i}">Показать в диалоге</button>` : ''}${c.type !== 'flag' && S.evaluated(c) && !hasOpen ? `<button class="btn-l" data-appeal>Оспорить</button>` : ''}</div>
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
    sections.forEach((_, i) => {
      const v = app.querySelector('#tv' + i); if (!v) return;
      if (i === sections.length - 1 && d.criteria.some(c => c.section === i && c.type === 'flag')) {
        v.textContent = d.gross ? 'Есть' : 'Нет'; app.querySelector('#ta' + i).innerHTML = art.orb(d.gross); return;
      }
      const s = S.section(d.criteria, i);
      v.innerHTML = s.W ? `${s.E}<small> / ${s.W}</small>` : 'N/A';
      setTimeout(() => { const b = app.querySelector('#tb' + i); if (b) b.style.width = (s.W ? s.E / s.W * 100 : 0) + '%'; }, 300 + i * 120);
    });
  }
  function refresh() {
    const calc = S.total(d.criteria);
    app.querySelector('#naNote').textContent = calc.W && calc.W < 100 ? `Вес N/A перераспределён: ${calc.W} → 100` : calc.W ? 'Все критерии применимы' : 'Нет применимых критериев — итог не рассчитывается';
    app.querySelector('#total2').textContent = d.total === null ? '—' : d.total.toFixed(2);
    renderBento();
  }
  function setFilter(f) {
    filter = f;
    app.querySelectorAll('.tile').forEach(t => t.classList.toggle('on', +t.dataset.sec === f));
    app.querySelectorAll('.crit,.sec').forEach(el => el.classList.toggle('dim', f !== null && +el.dataset.sec !== f));
    app.querySelector('#reset').classList.toggle('show', f !== null);
  }

  /* --- события --- */
  app.querySelectorAll('.tile').forEach(t => t.addEventListener('click', () => setFilter(filter === +t.dataset.sec ? null : +t.dataset.sec)));
  app.querySelector('#reset').onclick = () => setFilter(null);
  app.querySelector('#seg').addEventListener('click', e => {
    const b = e.target.closest('button'); if (!b) return;
    app.querySelectorAll('#seg button').forEach(x => x.classList.remove('on')); b.classList.add('on');
    app.querySelector('#chat').classList.toggle('op-only', b.dataset.v === 'op');
  });
  app.querySelector('#rerun').onclick = () => CQA.pages.dialog(app, id);
  app.querySelector('#toAppeal').onclick = () => {
    const c = app.querySelector('.crit:not([data-na]) [data-appeal]')?.closest('.crit');
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
    if (go) {
      app.querySelectorAll('.msg').forEach(m => m.classList.remove('hl', 'flash'));
      const m = app.querySelector('#m' + go.dataset.go); if (!m) return;
      m.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'center' });
      m.classList.add('hl'); void m.offsetWidth; m.classList.add('flash'); return;
    }
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
        const a = await CQA.api.createAppeal({ dialog_id: d.id, criterion_id: c.id, comment, proposed_answer: proposed });
        appeals.push(a);
      } catch (err) { toast('Не удалось отправить: ' + err.message, true); send.disabled = false; send.textContent = 'Отправить апелляцию'; return; }
      renderCrits();
      app.querySelector(`.crit[data-id="${c.id}"]`).classList.add('open');
      toast('Апелляция отправлена проверяющему');
    }
  });

  tilt(app); parallax(app.querySelector('.hero'));
  renderCrits(); refresh();
  setTimeout(() => setTotal(d.total), 350);
};
