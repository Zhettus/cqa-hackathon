/* Экран: список диалогов + загрузка набора + пакетная оценка (#dialogs). Все данные — из API. */
CQA.pages = CQA.pages || {};
CQA.pages.dialogs = async function (app) {
  const { esc, art, ringHTML, ring, countUp, tilt, parallax, toast } = CQA.ui;
  const cfg = CQA.config;
  let list = [], filter = 'all', query = '', prev = {}, polling = false, alive = true;

  app.innerHTML = `
  <section class="hero"><div class="wrap">
    <div>
      <div class="crumb">Контроль качества обслуживания</div>
      <h1 class="h1-long">Оценка качества диалогов</h1>
      <p class="lead">Автоматическая проверка работы операторов по чек-листу — с объяснением и репликой-доказательством для каждого балла.</p>
      <div class="toolbar">
        <label class="gid"><span>group_id</span><input id="gid" value="${esc(cfg.GROUP_ID)}" spellcheck="false"></label>
        <button class="btn btn-o" id="load">Загрузить набор</button>
        <button class="btn btn-g" id="run">Оценить все</button>
        <button class="btn btn-o" id="runNew" title="Оценить только диалоги без оценки текущей версией чек-листа">Оценить неоценённые</button>
        <button class="btn btn-o" id="export" title="results.json в формате организаторов: автоматическая оценка набора последней версией чек-листа">Экспорт для сдачи</button>
      </div>
      <div class="progress" id="progress"><div class="pt"><span id="ptext">Оценено 0 из 0</span><span id="ppct">0%</span></div><div class="pb"><i id="pbar"></i></div></div>
      <div id="batchNote" class="ro-note"></div>
    </div>
    <div class="scene">
      <div class="obj score" data-d="8"><div class="float"><div class="glass">${ringHTML('avg')}<div class="cap">Средний балл по набору</div></div></div></div>
      <div class="obj" style="left:2%;top:10%;z-index:4" data-d="28"><div class="float">${art.bubble()}</div></div>
      <div class="obj" style="right:2%;top:4%;z-index:2" data-d="20"><div class="float">${art.sphere()}</div></div>
      <div class="obj" style="left:6%;bottom:6%;z-index:4" data-d="24"><div class="float">${art.langs()}</div></div>
      <div class="obj" style="right:6%;bottom:10%;z-index:4" data-d="16"><div class="float">${art.cube()}</div></div>
    </div>
  </div></section>

  <div class="wrap">
    <div class="bento">
      <div class="tile" data-f="all"><div class="v" id="k0">0</div><div class="l">диалогов в наборе</div><div class="art">${art.bubble(.7)}</div></div>
      <div class="tile" data-f="evaluated"><div class="v" id="k1">0</div><div class="l">уже оценено</div><div class="mb"><i id="k1b"></i></div><div class="art">${art.bars()}</div></div>
      <div class="tile" data-f="gross"><div class="v" id="k2">0</div><div class="l">с грубыми нарушениями</div><div class="art">${art.orb(true)}</div></div>
      <div class="tile" data-f="appeal"><div class="v" id="k3">0</div><div class="l">с апелляцией</div><div class="art">${art.shield()}</div></div>
    </div>

    <section class="panel list-panel">
      <div class="filters">
        <div class="pills" id="pills">
          ${[['all', 'Все'], ['RU', 'RU'], ['KZ', 'KZ'], ['EN', 'EN'], ['agent chat', 'Чат'], ['voice call', 'Звонок'],
             ['gross', 'Есть нарушение'], ['appeal', 'Есть апелляция'], ['not_evaluable', 'Не оценивается'], ['pending', 'Без оценки']]
            .map(([v, l]) => `<button data-f="${v}" class="${v === 'all' ? 'on' : ''}">${l}</button>`).join('')}
        </div>
        <label class="search"><svg width="16" height="16" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7" stroke="currentColor" stroke-width="2" fill="none"/><path d="m20 20-4-4" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg><input id="q" placeholder="Поиск по ID"></label>
      </div>
      <div class="rows-head"><span>ДИАЛОГ</span><span>КАНАЛ</span><span>ЯЗЫК</span><span>БАЛЛ</span><span>СТАТУС</span><span></span></div>
      <div id="rows"><div class="loading">Загружаю набор…</div></div>
    </section>
  </div>`;

  const setAvg = ring('avg');
  let avgShown = 0;
  const icon = ch => ch === 'voice call'
    ? '<svg width="16" height="16" viewBox="0 0 24 24"><path d="M5 4h4l2 5-2.5 1.5a11 11 0 0 0 5 5L15 13l5 2v4a2 2 0 0 1-2 2A16 16 0 0 1 3 6a2 2 0 0 1 2-2" stroke="currentColor" stroke-width="1.8" fill="none" stroke-linejoin="round"/></svg>Звонок'
    : '<svg width="16" height="16" viewBox="0 0 24 24"><path d="M4 5h16v11H9l-5 4z" stroke="currentColor" stroke-width="1.8" fill="none" stroke-linejoin="round"/></svg>Чат';

  function status(d) {
    if (d.status === 'pending') return '<span class="pill na">Без оценки</span>';
    if (d.status === 'not_evaluable') return '<span class="pill na" title="Нет реплик оператора">Не оценивается</span>';
    if (d.status === 'failed') return `<span class="pill err" title="${esc(d.status_reason || '')}">Ошибка оценки</span>`;
    if (d.has_appeal) return '<span class="pill part">На пересмотре</span>';
    if (d.gross) return '<span class="pill zero">Грубая ошибка</span>';
    return '<span class="pill full">Без нарушений</span>';
  }
  const match = d => {
    if (query && !d.id.toLowerCase().includes(query)) return false;
    if (filter === 'all') return true;
    if (filter === 'gross') return d.gross;
    if (filter === 'appeal') return d.has_appeal;
    if (filter === 'evaluated') return d.status === 'evaluated';
    if (filter === 'not_evaluable' || filter === 'pending') return d.status === filter;
    return d.language === filter || d.channel === filter;
  };

  function renderRows() {
    const box = app.querySelector('#rows'); if (!box) return;
    const shown = list.filter(match);
    if (!list.length) { box.innerHTML = '<div class="empty-row">Набор пуст — нажмите «Загрузить набор»</div>'; return; }
    if (!shown.length) { box.innerHTML = '<div class="empty-row">Нет диалогов под этот фильтр</div>'; return; }
    box.innerHTML = shown.map((d, k) => {
      const just = prev[d.id] === 'pending' && d.status === 'evaluated';
      const sc = d.final_total ?? d.total;
      const changed = d.final_total !== null && d.final_total !== undefined && d.total !== null && d.final_total !== d.total;
      return `<a class="row ${just ? 'just' : ''} ${d.status === 'pending' ? 'pending' : ''}" href="#dialog/${encodeURIComponent(d.id)}" style="--k:${k}">
        <span class="rid"><b>${esc(d.id)}</b><small>${d.messages} реплик · оператор ${d.operator_messages}${d.has_audio ? ' · аудио' : ''}</small></span>
        <span class="ch">${icon(d.channel)}</span>
        <span><span class="lng">${esc(d.language)}</span></span>
        <span class="sc">${d.status === 'pending' ? '<small>—</small>' : sc === null || sc === undefined ? '<small>null</small>' :
          `<b>${sc.toFixed(2)}</b>${changed ? `<small title="исходная оценка"> (${d.total.toFixed(2)})</small>` : ''}<span class="mini"><i style="width:${sc}%;background:${sc >= 80 ? 'var(--grad)' : sc >= 60 ? 'linear-gradient(90deg,#F5C25A,#E89B12)' : 'linear-gradient(90deg,#F59A9D,#E5484D)'}"></i></span>`}</span>
        <span>${status(d)}</span>
        <span class="go">${CQA.ui.chev}</span></a>`;
    }).join('');
  }

  const kpi = [0, 0, 0, 0];
  function renderStats() {
    const ev = list.filter(d => d.status === 'evaluated');
    const vals = [list.length, ev.length, list.filter(d => d.gross).length, list.filter(d => d.has_appeal).length];
    vals.forEach((v, i) => { const el = app.querySelector('#k' + i); if (el && v !== kpi[i]) { countUp(el, kpi[i], v, 0, 700); kpi[i] = v; } });
    const b = app.querySelector('#k1b'); if (b) b.style.width = (list.length ? ev.length / list.length * 100 : 0) + '%';
    const totals = ev.map(d => d.final_total ?? d.total).filter(v => v !== null && v !== undefined);
    const avg = totals.length ? Math.round(totals.reduce((s, v) => s + v, 0) / totals.length * 100) / 100 : 0;
    if (avg !== avgShown) { setAvg(avg, avgShown); avgShown = avg; }
  }

  async function load() {
    try { list = await CQA.api.getDialogs(cfg.GROUP_ID); }
    catch (e) { app.querySelector('#rows').innerHTML = `<div class="empty-row">Не удалось загрузить набор: ${esc(e.message)}</div>`; return; }
    renderRows(); renderStats();
    list.forEach(d => prev[d.id] = d.status);
  }

  async function importAndLoad() {
    const btn = app.querySelector('#load');
    cfg.GROUP_ID = app.querySelector('#gid').value.trim() || 'DEV'; prev = {};
    btn.disabled = true; btn.textContent = 'Загружаю…';
    try {
      const r = await CQA.api.importDataset(cfg.GROUP_ID, 'remote');
      toast(`Набор ${cfg.GROUP_ID}: ${r.imported} диалогов (${r.source === 'remote' ? 'API организаторов' : r.source === 'local' ? 'локальный файл' : r.source})`);
    } catch (e) { toast('Не удалось загрузить набор: ' + e.message, true); }
    btn.disabled = false; btn.textContent = 'Загрузить набор';
    await load();
  }

  async function run(onlyNew) {
    if (polling) return;
    const btn = app.querySelector(onlyNew ? '#runNew' : '#run'), pr = app.querySelector('#progress'), note = app.querySelector('#batchNote');
    btn.disabled = true; btn.textContent = 'Оцениваю…';
    let started;
    try { started = await CQA.api.startBatch(cfg.GROUP_ID, { only_not_evaluated: !!onlyNew }); }
    catch (e) { toast('Не удалось запустить оценку: ' + e.message, true); btn.disabled = false; btn.textContent = onlyNew ? 'Оценить неоценённые' : 'Оценить все'; return; }
    if (!started.total) { toast('Все диалоги уже оценены текущей версией чек-листа'); btn.disabled = false; btn.textContent = onlyNew ? 'Оценить неоценённые' : 'Оценить все'; return; }
    note.textContent = `Пакет ${started.batch_id}`;
    pr.classList.add('show'); polling = true;
    while (alive) {
      let b;
      try { b = await CQA.api.getBatch(started.batch_id); list = await CQA.api.getDialogs(cfg.GROUP_ID); }
      catch (e) { toast('Связь с сервером потеряна: ' + e.message, true); break; }
      const pct = b.total ? Math.round(b.done / b.total * 100) : 0;
      app.querySelector('#ptext').textContent = `Оценено ${b.done} из ${b.total}` + (b.progress.failed ? ` · ошибок ${b.progress.failed}` : '') + (b.progress.provider_error ? ` · провайдер недоступен ${b.progress.provider_error}` : '');
      app.querySelector('#ppct').textContent = pct + '%';
      app.querySelector('#pbar').style.width = pct + '%';
      renderRows(); renderStats();
      list.forEach(d => prev[d.id] = d.status);
      if (b.finished) {
        if (b.status === 'completed') toast(`Готово: ${b.total} диалогов, $${(b.cost && b.cost.usd || 0).toFixed(2)}`);
        else toast(`Пакет ${b.status}: ${b.error || ''}`, true);
        note.textContent = b.error ? `Пакет ${started.batch_id}: ${b.error}` : `Пакет ${started.batch_id} завершён`;
        break;
      }
      await new Promise(r => setTimeout(r, cfg.POLL_MS));
    }
    polling = false; btn.disabled = false; btn.textContent = onlyNew ? 'Оценить неоценённые' : 'Оценить все';
    setTimeout(() => pr.classList.remove('show'), 1500);
  }

  async function exportResults() {
    const btn = app.querySelector('#export');
    btn.disabled = true; btn.textContent = 'Экспортирую…';
    try {
      const doc = await CQA.api.exportResults(cfg.GROUP_ID);
      const blob = new Blob([JSON.stringify(doc, null, 2) + '\n'], { type: 'application/json' });
      const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = 'results.json';
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 2000);
      const ok = doc.results.filter(r => r.status === 'ok').length;
      toast(`Экспорт ${cfg.GROUP_ID}: ${ok} ok, ${doc.results.length - ok} error; модель ${doc.model.name} (${doc.model.mode})`);
    } catch (e) { toast('Экспорт не выполнен: ' + e.message, true); }
    btn.disabled = false; btn.textContent = 'Экспорт для сдачи';
  }

  /* --- события --- */
  const setFilter = f => {
    filter = f;
    app.querySelectorAll('#pills button').forEach(b => b.classList.toggle('on', b.dataset.f === f));
    app.querySelectorAll('.tile').forEach(t => t.classList.toggle('on', t.dataset.f === f && f !== 'all'));
    renderRows();
  };
  app.querySelector('#pills').addEventListener('click', e => { const b = e.target.closest('button'); if (b) setFilter(b.dataset.f); });
  app.querySelectorAll('.tile').forEach(t => t.addEventListener('click', () => setFilter(filter === t.dataset.f ? 'all' : t.dataset.f)));
  app.querySelector('#q').addEventListener('input', e => { query = e.target.value.trim().toLowerCase(); renderRows(); });
  app.querySelector('#load').onclick = importAndLoad;
  app.querySelector('#run').onclick = () => run(false);
  app.querySelector('#runNew').onclick = () => run(true);
  app.querySelector('#export').onclick = exportResults;
  window.addEventListener('hashchange', () => { alive = false; }, { once: true });

  tilt(app); parallax(app.querySelector('.hero'));
  await load();
};
