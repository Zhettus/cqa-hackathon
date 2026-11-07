/* Экран: редактор чек-листа (#checklist) */
CQA.pages = CQA.pages || {};
CQA.state = CQA.state || { pendingApply: null };   // версия сохранена, но набор ею ещё не переоценён

CQA.pages.checklist = async function (app) {
  const { esc, art, chev, ringHTML, ring, tilt, parallax, toast, reduce } = CQA.ui;
  const S = CQA.scoring, cfg = CQA.config;
  app.innerHTML = `<div class="wrap"><div class="loading">Загружаю чек-лист…</div></div>`;

  let saved;
  try { saved = await CQA.api.getChecklist(); }
  catch (e) { app.innerHTML = `<div class="wrap"><div class="empty"><h2>Не удалось загрузить чек-лист</h2><p>${esc(e.message)}</p></div></div>`; return; }
  const clone = o => JSON.parse(JSON.stringify(o));
  let draft = clone(saved), openId = null, applying = false, firstPaint = true;
  const COLORS = ['#7B8CF5', '#5FA8F0', '#3CC3C9', '#1FD17A', '#16A35A', '#8BD3B0', '#A9B4FF', '#4FD9C2', '#F5C25A', '#E89B12'];

  app.innerHTML = `
  <section class="hero"><div class="wrap">
    <div>
      <div class="crumb">Настройка оценки</div>
      <h1>Чек-лист</h1>
      <p class="lead">Критерии, веса, шкалы и правила N/A. Изменения применяются к оценке без правки кода.</p>
      <div class="meta" id="meta"></div>
      <div><button class="btn btn-g" id="save">Сохранить конфигурацию</button><button class="btn btn-o" id="add">+ Добавить критерий</button></div>
    </div>
    <div class="scene">
      <div class="obj score" data-d="8"><div class="float"><div class="glass">${ringHTML('ws', 72, 'сумма весов')}<div class="cap" id="wsCap">Должно быть 100</div></div></div></div>
      <div class="obj" style="left:2%;top:10%;z-index:4" data-d="28"><div class="float">${art.cube()}</div></div>
      <div class="obj" style="right:2%;top:4%;z-index:2" data-d="20"><div class="float">${art.sphere()}</div></div>
      <div class="obj" style="left:6%;bottom:8%;z-index:4" data-d="24"><div class="float">${art.shield()}</div></div>
      <div class="obj" style="right:6%;bottom:10%;z-index:4" data-d="16"><div class="float">${art.bars()}</div></div>
    </div>
  </div></section>

  <div class="wrap">
    <main class="cl-grid">
      <section class="panel"><h2>Критерии</h2><div id="list"></div>
        <button class="add-row" id="add2">+ Добавить критерий</button></section>
      <aside class="panel side-panel" id="side"></aside>
    </main>
  </div>`;

  const setRing = ring('ws', 72, 0);
  let ringShown = 0;

  /* ---------- модель ---------- */
  const nextId = () => 'c' + String(Math.max(0, ...draft.criteria.map(c => +String(c.id).replace(/\D/g, '') || 0)) + 1).padStart(2, '0');
  const find = id => draft.criteria.find(c => c.id === id);
  function diff() {
    const was = Object.fromEntries(saved.criteria.map(c => [c.id, JSON.stringify(c)]));
    const now = Object.fromEntries(draft.criteria.map(c => [c.id, JSON.stringify(c)]));
    return {
      added: draft.criteria.filter(c => !was[c.id]).map(c => c.id),
      changed: draft.criteria.filter(c => was[c.id] && was[c.id] !== now[c.id]).map(c => c.id),
      removed: saved.criteria.filter(c => !now[c.id]).map(c => c.id),
      order: JSON.stringify(saved.criteria.map(c => c.id)) !== JSON.stringify(draft.criteria.filter(c => was[c.id]).map(c => c.id))
    };
  }
  function errors() {
    const e = [];
    const sum = S.weightSum(draft.criteria);
    draft.criteria.forEach(c => {
      if (!c.title.trim()) e.push(`Критерий ${c.id}: нет названия`);
      if (c.type !== 'flag' && !(c.weight > 0)) e.push(`Критерий ${c.id}: вес должен быть больше 0`);
      if (c.type === 'scale') {
        const st = c.steps.filter(v => v !== '' && !isNaN(v));
        if (new Set(st).size < 2) e.push(`Критерий ${c.id}: в шкале нужно минимум 2 разные ступени`);
        if (st.some(v => v < 0)) e.push(`Критерий ${c.id}: ступени шкалы не могут быть отрицательными`);
      }
      if (c.na_allowed && !c.na_rule.trim()) e.push(`Критерий ${c.id}: опишите, когда ставится N/A`);
      if (c.reference && c.reference.length > 20000) e.push(`Критерий ${c.id}: справочник длиннее 20000 символов`);
    });
    return e;
  }

  /* ---------- отрисовка ---------- */
  const tag = c => `<span class="ctag">${esc(S.typeName(c))}</span>${c.type !== 'flag' ? `<span class="ctag w">вес ${c.weight}</span>` : '<span class="ctag">без баллов</span>'}${c.na_allowed ? '<span class="ctag na">N/A</span>' : ''}${c.reference ? '<span class="ctag">справочник</span>' : ''}${c.guards && c.guards.length ? '<span class="ctag">guards</span>' : ''}`;
  function cardStatus(id) {
    const d = diff();
    return d.added.includes(id) ? '<span class="pill full">новый</span>' : d.changed.includes(id) ? '<span class="pill part">изменён</span>' : '';
  }

  function editor(c) {
    const steps = c.type === 'scale' ? [...c.steps].map((v, k) => ({ v, k })) : [];
    return `<div class="ed">
      <div class="row2">
        <label class="f"><span>Название</span><input data-f="title" value="${esc(c.title)}" placeholder="Например: Соблюдение скрипта"></label>
        <label class="f sm"><span>Секция</span><select data-f="section">${draft.sections.map((s, i) => `<option value="${i}" ${i === c.section ? 'selected' : ''}>${esc(s)}</option>`).join('')}</select></label>
      </div>
      <label class="f"><span>Формулировка для оценки</span><textarea data-f="description" placeholder="Да — оператор …">${esc(c.description)}</textarea></label>
      <div class="row2">
        <div class="f"><span>Тип ответа</span><div class="seg" data-type>
          ${[['yes_no', 'Да / Нет'], ['scale', 'Шкала'], ['flag', 'Флаг']].map(([v, l]) => `<button data-t="${v}" class="${c.type === v ? 'on' : ''}">${l}</button>`).join('')}</div></div>
        ${c.type !== 'flag' ? `<div class="f sm"><span>Вес</span><div class="num-in"><button data-w="-5">−</button><input data-f="weight" type="number" min="0" step="1" value="${c.weight}"><button data-w="5">+</button></div></div>` : '<div class="f sm"><span>Вес</span><div class="hint">Флаг не даёт баллов, отмечается отдельно</div></div>'}
      </div>
      ${c.type === 'scale' ? `<div class="f"><span>Шкала (верхняя ступень даёт полный вес)</span>
        <div class="steps">${steps.sort((a, b) => b.v - a.v).map(s => `<div class="step" data-k="${s.k}">
          <input class="sv" data-step="${s.k}" type="number" min="0" value="${s.v}">
          <input class="sl" data-label="${s.k}" value="${esc(c.step_labels?.[s.v] || '')}" placeholder="Что значит эта ступень">
          <span class="sp">${c.weight && S.max(c) ? S.r2(s.v / S.max(c) * c.weight) : 0} б.</span>
          <button class="x" data-delstep="${s.k}" title="Удалить ступень">✕</button></div>`).join('')}
          <button class="add-step" data-addstep>+ ступень</button></div></div>` : ''}
      ${c.type !== 'flag' ? `<div class="f"><label class="sw"><input type="checkbox" data-f="na_allowed" ${c.na_allowed ? 'checked' : ''}><i></i>Разрешить N/A</label>
        ${c.na_allowed ? `<div class="na-box">
          <input data-f="na_rule" value="${esc(c.na_rule)}" placeholder="Когда критерий неприменим (условие N/A; например: только для звонков — …)"></div>` : ''}</div>` : ''}
      <label class="f"><span>Справочник компании (reference) <small>— факты, с которыми модель сверяет ответ оператора; необязательно</small></span><textarea class="ref" data-f="reference" placeholder="1. Факт…&#10;2. Факт…">${esc(c.reference || '')}</textarea></label>
      <div class="ed-acts">
        <button class="btn-l" data-move="-1">↑</button><button class="btn-l" data-move="1">↓</button>
        <span class="sp-flex"></span>
        <button class="btn-l" data-replace title="Новый критерий займёт место этого с тем же весом">Заменить на новый</button>
        <button class="btn-l danger" data-del>Удалить</button>
      </div></div>`;
  }

  function renderList() {
    let html = '', last = -1;
    const order = [...draft.criteria].map((c, k) => ({ c, k })).sort((a, b) => a.c.section - b.c.section || a.k - b.k);
    order.forEach(({ c }, n) => {
      if (c.section !== last) { last = c.section; html += `<div class="sec">${esc((draft.sections[c.section] || '').toUpperCase())}</div>`; }
      html += `<div class="ccard ${firstPaint ? '' : 'still'} ${openId === c.id ? 'open' : ''}" data-id="${c.id}" style="--k:${n}">
        <div class="ch-head"><span class="n">${esc(c.id)}</span><span class="t">${esc(c.title) || '<i>Без названия</i>'}</span>
          <span class="tags">${tag(c)}</span><span class="st">${cardStatus(c.id)}</span>${chev}</div>
        <div class="body"><div>${openId === c.id ? editor(c) : ''}</div></div></div>`;
    });
    app.querySelector('#list').innerHTML = html; firstPaint = false;
  }
  function renderHead(id) {
    const card = app.querySelector(`.ccard[data-id="${id}"]`), c = find(id); if (!card || !c) return;
    card.querySelector('.ch-head .t').innerHTML = esc(c.title) || '<i>Без названия</i>';
    card.querySelector('.ch-head .tags').innerHTML = tag(c);
    card.querySelector('.ch-head .st').innerHTML = cardStatus(id);
    card.querySelectorAll('.step').forEach(row => {
      const v = c.steps?.[+row.dataset.k]; row.querySelector('.sp').textContent = (c.weight && S.max(c) ? S.r2(v / S.max(c) * c.weight) : 0) + ' б.';
    });
  }

  function renderSide() {
    const sum = S.r2(S.weightSum(draft.criteria)), errs = errors(), d = diff();
    const dirty = d.added.length || d.changed.length || d.removed.length || d.order;
    const pend = CQA.state.pendingApply;
    if (sum !== ringShown) { setRing(sum, ringShown); ringShown = sum; }
    const glass = app.querySelector('.glass'); glass.classList.toggle('warn', sum !== 100);
    app.querySelector('#wsCap').textContent = sum === 100 ? 'Всё сходится' : sum > 100 ? `Лишние ${S.r2(sum - 100)}` : `Не хватает ${S.r2(100 - sum)}`;
    app.querySelector('#meta').innerHTML = `<span>Версия ${saved.version}${saved.is_latest === false ? ' (не последняя, только просмотр)' : ''}</span><span>${draft.criteria.length} критериев</span><span>${draft.sections.length} секции</span>${sum !== 100 ? `<span class="warn-chip">Сумма весов ${sum}, не 100 — допустимо, веса N/A перераспределяются</span>` : ''}${dirty ? '<span class="warn-chip">Есть несохранённые изменения</span>' : ''}`;
    renderVersions();

    const weighted = draft.criteria.filter(c => c.type !== 'flag');
    const name = id => esc(find(id)?.title || saved.criteria.find(c => c.id === id)?.title || id);
    app.querySelector('#side').innerHTML = `
      <h2>Сводка</h2>
      <div class="wsum ${sum === 100 ? 'ok' : 'bad'}"><b>${sum}</b> / 100 <span>${sum === 100 ? '✓ веса сходятся' : 'сумма весов не 100 — итог считается по применимым весам'}</span></div>
      <div class="stack">${weighted.map((c, i) => `<i style="flex:${c.weight || 0.0001};background:${COLORS[i % COLORS.length]}" title="${esc(c.id)} ${esc(c.title)}: ${c.weight}"></i>`).join('')}</div>
      <div class="legend">${weighted.map((c, i) => `<span><i style="background:${COLORS[i % COLORS.length]}"></i>${esc(c.id)} · ${c.weight}</span>`).join('')}</div>

      <div class="side-sec"><small>ИЗМЕНЕНИЯ</small>
        ${dirty ? `<ul class="chg-list">
          ${d.added.map(id => `<li><span class="pill full">+</span>${esc(id)} ${name(id)}</li>`).join('')}
          ${d.changed.map(id => `<li><span class="pill part">~</span>${esc(id)} ${name(id)}</li>`).join('')}
          ${d.removed.map(id => `<li><span class="pill zero">−</span>${esc(id)} ${name(id)}</li>`).join('')}
          ${d.order && !d.added.length && !d.removed.length ? '<li><span class="pill na">↕</span>изменён порядок</li>' : ''}</ul>` : '<p class="muted">Нет несохранённых изменений</p>'}
      </div>
      ${errs.length ? `<div class="side-sec"><small>НУЖНО ИСПРАВИТЬ</small><ul class="err-list">${errs.map(e => `<li>${esc(e)}</li>`).join('')}</ul></div>` : ''}

      <button class="btn btn-g wide" id="save2" ${!dirty || errs.length || saved.is_latest === false ? 'disabled' : ''}>Сохранить как новую версию</button>
      <button class="btn-l wide" id="check">Проверить на сервере</button>
      ${dirty ? '<button class="btn-l wide" id="reset">Отменить изменения</button>' : ''}

      <div class="side-sec apply ${pend ? 'hot' : ''}"><small>ПРИМЕНИТЬ К НАБОРУ</small>
        <p>${pend ? `Версия <b>v${pend.version}</b> сохранена (изменены: <b>${pend.ids.map(esc).join(', ')}</b>). Переоценить набор <b>${esc(cfg.GROUP_ID)}</b> новой версией — каждая оценка хранит версию чек-листа, прежние оценки сохраняются.` : 'После сохранения новой версии переоцените набор ею.'}</p>
        <div class="progress ${applying ? 'show' : ''}" id="aprog"><div class="pt"><span id="aptext">Оценено 0 из 0</span><span id="appct">0%</span></div><div class="pb"><i id="apbar"></i></div></div>
        <button class="btn ${pend ? 'btn-g' : 'btn-o'} wide" id="apply" ${!pend || applying ? 'disabled' : ''}>${applying ? 'Переоцениваю…' : 'Переоценить набор новой версией'}</button>
        <button class="btn-l wide" id="applyAll" ${applying ? 'disabled' : ''}>Переоценить весь набор текущей версией</button>
      </div>`;
  }
  const renderAll = () => { renderList(); renderSide(); };

  /* ---------- действия ---------- */
  function toggle(id) {
    openId = openId === id ? null : id; renderList();
    if (openId) setTimeout(() => app.querySelector(`.ccard[data-id="${id}"]`)?.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'nearest' }), 50);
  }
  function addCriterion(at, base) {
    const c = { id: nextId(), section: base ? base.section : Math.min(2, draft.sections.length - 1), title: base ? '' : 'Новый критерий',
      description: '', type: 'yes_no', weight: base ? base.weight : 0, steps: undefined, step_labels: {}, na_allowed: false, na_channels: [], na_rule: '' };
    if (at === undefined) draft.criteria.push(c); else draft.criteria.splice(at, 0, c);
    openId = c.id; renderAll();
    setTimeout(() => { const card = app.querySelector(`.ccard[data-id="${c.id}"]`); card?.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'center' }); card?.classList.add('born'); card?.querySelector('[data-f="title"]')?.focus(); }, 60);
    return c;
  }

  async function save() {
    const errs = errors(); if (errs.length) { toast(errs[0], true); return; }
    const d = diff(), btns = app.querySelectorAll('#save,#save2');
    btns.forEach(b => { b.disabled = true; b.textContent = 'Сохраняю…'; });
    const payload = { ...draft, criteria: draft.criteria.map(c => {
      const o = { ...c, weight: +c.weight, section: +c.section };
      if (c.type === 'scale') { o.steps = [...new Set(c.steps.map(Number))].sort((a, b) => a - b); } else { delete o.steps; o.step_labels = {}; }
      if (c.type === 'flag') { o.weight = 0; o.na_allowed = false; }
      return o; }) };
    const comment = prompt('Комментарий к новой версии (что изменено):', [...d.added.map(i => '+' + i), ...d.changed.map(i => '~' + i), ...d.removed.map(i => '−' + i)].join(', ')) ?? '';
    try { saved = await CQA.api.saveChecklist(payload, { base_version: saved.version, comment }); }
    catch (e) { toast('Не удалось сохранить: ' + e.message, true); btns.forEach(b => { b.disabled = false; b.textContent = 'Сохранить конфигурацию'; }); return; }
    draft = clone(saved);
    CQA.state.pendingApply = { version: saved.version, ids: [...d.added, ...d.changed, ...d.removed] };
    app.querySelector('#save').disabled = false; app.querySelector('#save').textContent = 'Сохранить конфигурацию';
    renderAll();
    toast(`Чек-лист сохранён · версия ${saved.version}`);
    app.querySelector('.apply')?.classList.add('flashbox');
  }

  async function apply(all) {
    if (applying) return;
    applying = true; renderSide();
    const version = all ? saved.version : (CQA.state.pendingApply && CQA.state.pendingApply.version) || saved.version;
    let b;
    try { b = await CQA.api.startBatch(cfg.GROUP_ID, { version }); }
    catch (e) { toast('Не удалось запустить: ' + e.message, true); applying = false; renderSide(); return; }
    if (!b.total) { toast('В наборе нет диалогов'); applying = false; renderSide(); return; }
    while (true) {
      let p; try { p = await CQA.api.getBatch(b.batch_id); } catch (e) { toast('Связь потеряна: ' + e.message, true); break; }
      const pct = p.total ? Math.round(p.done / p.total * 100) : 0;
      const t = app.querySelector('#aptext'); if (!t) return;          // ушли со страницы
      t.textContent = `Оценено ${p.done} из ${p.total}`; app.querySelector('#appct').textContent = pct + '%'; app.querySelector('#apbar').style.width = pct + '%';
      if (p.finished) {
        if (p.status === 'completed') { toast(`Готово: ${p.total} диалогов переоценены версией v${version}`); CQA.state.pendingApply = null; }
        else toast(`Пакет ${p.status}: ${p.error || ''}`, true);
        break;
      }
      await new Promise(r => setTimeout(r, Math.min(cfg.POLL_MS, 1000)));
    }
    applying = false; renderSide();
  }

  /* ---------- версии: список и просмотр старой версии (без правки) ---------- */
  let versions = [];
  async function renderVersions() {
    const meta = app.querySelector('#meta'); if (!meta) return;
    try { versions = versions.length ? versions : await CQA.api.getChecklistVersions(); } catch (_) { return; }
    let box = app.querySelector('#vers');
    if (!box) { box = document.createElement('div'); box.id = 'vers'; box.className = 'vers'; meta.after(box); }
    box.innerHTML = 'Версии: ' + versions.map(v => `<button data-v="${v.version}" class="${v.version === saved.version ? 'on' : ''}" title="${esc(v.comment || '')} · ${esc(v.author || '')} · ${esc(v.created_at || '')}">v${v.version}</button>`).join('');
  }
  async function checkOnServer() {
    try {
      const r = await CQA.api.validateChecklist({ ...draft });
      if (r.valid) toast('Сервер: чек-лист корректен');
      else toast('Сервер: ' + r.errors.map(e => `${e.path}: ${e.message}`).join('; '), true);
    } catch (e) { toast('Не удалось проверить: ' + e.message, true); }
  }

  /* ---------- события ---------- */
  app.querySelector('#add').onclick = app.querySelector('#add2').onclick = () => addCriterion();
  app.querySelector('#save').onclick = save;
  app.querySelector('#side').addEventListener('click', e => {
    if (e.target.closest('#save2')) save();
    if (e.target.closest('#reset')) { draft = clone(saved); openId = null; renderAll(); toast('Изменения отменены'); }
    if (e.target.closest('#apply')) apply(false);
    if (e.target.closest('#applyAll')) apply(true);
    if (e.target.closest('#check')) checkOnServer();
  });
  app.addEventListener('click', async e => {
    const b = e.target.closest('#vers button'); if (!b) return;
    try { saved = await CQA.api.getChecklist(+b.dataset.v); draft = clone(saved); openId = null; versions = []; renderAll(); toast(`Версия v${saved.version}${saved.is_latest ? '' : ' — только просмотр'}`); }
    catch (err) { toast('Не удалось загрузить версию: ' + err.message, true); }
  });

  const list = app.querySelector('#list');
  list.addEventListener('click', e => {
    const card = e.target.closest('.ccard'); if (!card) return;
    const id = card.dataset.id, c = find(id);
    if (e.target.closest('.ch-head')) { toggle(id); return; }
    const t = e.target.closest('[data-t]');
    if (t) {
      c.type = t.dataset.t;
      if (c.type === 'scale' && !c.steps) { c.steps = [0, 5, 10]; c.step_labels = {}; }
      if (c.type === 'flag') { c.weight = 0; c.na_allowed = false; }
      renderAll(); return;
    }
    const w = e.target.closest('[data-w]');
    if (w) { c.weight = Math.max(0, (+c.weight || 0) + +w.dataset.w); card.querySelector('[data-f="weight"]').value = c.weight; renderHead(id); renderSide(); return; }
    if (e.target.closest('[data-addstep]')) { const m = Math.max(...c.steps); c.steps.push(m + 5); renderAll(); return; }
    const ds = e.target.closest('[data-delstep]');
    if (ds) { if (c.steps.length <= 2) { toast('В шкале должно остаться минимум 2 ступени', true); return; } const v = c.steps[+ds.dataset.delstep]; c.steps.splice(+ds.dataset.delstep, 1); delete c.step_labels[v]; renderAll(); return; }
    const mv = e.target.closest('[data-move]');
    if (mv) {
      const same = draft.criteria.filter(x => x.section === c.section), k = same.indexOf(c), j = k + +mv.dataset.move;
      if (j < 0 || j >= same.length) return;
      const a = draft.criteria.indexOf(c), b = draft.criteria.indexOf(same[j]);
      [draft.criteria[a], draft.criteria[b]] = [draft.criteria[b], draft.criteria[a]]; renderAll(); return;
    }
    if (e.target.closest('[data-replace]')) {
      const at = draft.criteria.indexOf(c); draft.criteria.splice(at, 1);
      addCriterion(at, c); toast(`Критерий ${c.id} заменён новым с весом ${c.weight}. Заполните название и формулировку.`); return;
    }
    if (e.target.closest('[data-del]')) {
      draft.criteria = draft.criteria.filter(x => x !== c); openId = null; renderAll();
      toast(`Критерий ${c.id} удалён. ${c.weight ? `Освободилось ${c.weight} веса — распределите его.` : ''}`); return;
    }
  });
  list.addEventListener('input', e => {
    const card = e.target.closest('.ccard'); if (!card) return;
    const id = card.dataset.id, c = find(id), el = e.target;
    el.classList.remove('err');
    if (el.dataset.f === 'title' || el.dataset.f === 'description' || el.dataset.f === 'na_rule' || el.dataset.f === 'reference') c[el.dataset.f] = el.value;
    if (el.dataset.f === 'weight') c.weight = el.value === '' ? 0 : +el.value;
    if (el.dataset.step !== undefined) {
      const k = +el.dataset.step, old = c.steps[k], v = el.value === '' ? 0 : +el.value;
      c.steps[k] = v; if (c.step_labels[old] !== undefined) { c.step_labels[v] = c.step_labels[old]; if (old !== v) delete c.step_labels[old]; }
    }
    if (el.dataset.label !== undefined) c.step_labels[c.steps[+el.dataset.label]] = el.value;
    renderHead(id); renderSide();
  });
  list.addEventListener('change', e => {
    const card = e.target.closest('.ccard'); if (!card) return;
    const c = find(card.dataset.id), el = e.target;
    if (el.dataset.f === 'section') { c.section = +el.value; renderAll(); }
    if (el.dataset.f === 'na_allowed') { c.na_allowed = el.checked; if (!c.na_allowed) c.na_channels = []; renderAll(); }
  });

  tilt(app); parallax(app.querySelector('.hero'));
  renderAll();
};
