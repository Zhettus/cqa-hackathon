/* =========================================================
   ЕДИНСТВЕННАЯ точка связи фронта с бэком.
   Страницы вызывают только CQA.api.*, ничего не знают про fetch.
   Если на бэке назовут поля иначе — правим только normalize* ниже.
   ========================================================= */
CQA.api = (() => {
  const cfg = CQA.config;
  let checklistCache = null;

  /* ---------- приведение ответов бэка к формату фронта ---------- */
  function fmtTime(ts) {
    if (!ts) return '';
    const m = /T(\d{2}:\d{2}:\d{2})/.exec(ts);          // звонки: ISO 8601 → 10:01:30
    return m ? m[1] : ts;                                // чаты: MM:SS как есть
  }
  function normalizeCriterion(c) {
    const legacyNa = c.type === 'yes_no_na';             // на случай старого формата
    const type = legacyNa ? 'yes_no' : (c.type || 'yes_no');
    return {
      id: String(c.id), section: +(c.section ?? 0), title: c.title || '', description: c.description || '',
      type, weight: +(c.weight ?? 0),
      steps: type === 'scale' ? (c.steps || [0, 5, 10]).map(Number) : undefined,
      step_labels: c.step_labels || {},
      na_allowed: legacyNa || !!c.na_allowed, na_channels: c.na_channels || [], na_rule: c.na_rule || ''
    };
  }
  function normalizeChecklist(raw) {
    return { version: raw.version ?? 1, sections: raw.sections || ['Критерии'], criteria: (raw.criteria || []).map(normalizeCriterion) };
  }
  function normalizeDialog(raw, checklist) {
    const results = Object.fromEntries((raw.criteria || []).map(c => [String(c.id), c]));
    const criteria = checklist.criteria.map(def => {
      const r = results[def.id] || {};
      const answer = r.answer === undefined ? null : (def.type === 'scale' && r.answer !== 'N/A' && r.answer !== null ? +r.answer : r.answer);
      return { ...def, answer,
        evidence: Array.isArray(r.evidence) ? r.evidence.map(Number) : (r.evidence ? [+r.evidence] : []),
        why: r.explanation || r.na_reason || (answer === null ? 'Критерий ещё не оценён по этому диалогу — нажмите «Применить к набору» в чек-листе.' : '') };
    });
    const calc = CQA.scoring.total(criteria);
    return {
      id: raw.id || raw.conversation_id, channel: raw.channel, language: raw.language, operator: raw.operator || '',
      audio_url: raw.audio_url || null, checklist_version: raw.checklist_version,
      transcript: (raw.transcript || []).map((m, k) => ({ i: m.i ?? k + 1, t: fmtTime(m.timestamp), s: m.speaker, x: m.text })),
      criteria, sections: checklist.sections,
      total: calc.total,                                  // считаем по чек-листу (бэк может прислать свой total — сверим)
      backend_total: raw.total,
      gross: raw.gross_violation ?? CQA.scoring.gross(criteria)
    };
  }
  function normalizeAppeal(a) {
    return {
      id: a.id, dialog_id: a.dialog_id, criterion_id: String(a.criterion_id), comment: a.comment || '',
      proposed: a.proposed_answer ?? null, status: a.status || 'open', created_at: a.created_at || null,
      original: a.original || {}, original_total: a.original_total ?? a.original?.total ?? null,
      decision: a.decision || null, final_total: a.final_total ?? null
    };
  }

  /* ---------- настоящий бэк (FastAPI) ---------- */
  async function http(method, path, body) {
    const r = await fetch(cfg.API_BASE + path, {
      method, headers: body ? { 'Content-Type': 'application/json' } : {}, body: body ? JSON.stringify(body) : undefined
    });
    if (!r.ok) throw new Error(`${method} ${path} → ${r.status}`);
    return r.json();
  }
  const q = encodeURIComponent;
  const real = {
    async getChecklist() { return (checklistCache = normalizeChecklist(await http('GET', '/api/checklist'))); },
    async saveChecklist(cl) { return (checklistCache = normalizeChecklist(await http('PUT', '/api/checklist', cl))); },
    getDialogs: g => http('GET', `/api/dialogs?group_id=${q(g)}`),
    evaluate: (g, criteria) => http('POST', '/api/evaluate', criteria ? { group_id: g, criteria } : { group_id: g }),
    getProgress: g => http('GET', `/api/progress?group_id=${q(g)}`),
    async getDialog(id) { return normalizeDialog(await http('GET', `/api/dialogs/${q(id)}`), checklistCache || await real.getChecklist()); },
    async getAllResults(g) {
      const cl = checklistCache || await real.getChecklist();
      try { return (await http('GET', `/api/results?group_id=${q(g)}`)).map(r => normalizeDialog(r, cl)); }
      catch (e) {                                         // нет /api/results — собираем по одному диалогу
        const list = (await real.getDialogs(g)).filter(d => d.status === 'evaluated');
        const out = [];
        for (let i = 0; i < list.length; i += 8)
          out.push(...await Promise.all(list.slice(i, i + 8).map(d => real.getDialog(d.id).catch(() => null))));
        return out.filter(Boolean);
      }
    },
    async getAppeals() { return (await http('GET', '/api/appeals')).map(normalizeAppeal); },
    async createAppeal(body) { return normalizeAppeal(await http('POST', '/api/appeals', body)); },
    async decideAppeal(id, body) {
      const res = await http('POST', `/api/appeals/${q(id)}/decision`, body);
      return { appeal: normalizeAppeal(res.appeal), dialog: normalizeDialog(res.result, checklistCache || await real.getChecklist()) };
    }
  };

  /* ---------- тестовый режим ---------- */
  const M = CQA.mock, store = {}, appeals = [], progress = { done: 0, total: 0 };
  const cl = () => normalizeChecklist(M.checklist);
  M.records.forEach(r => store[r.conversation_id] = M.evaluate(r, cl()));
  progress.done = progress.total = M.records.length;
  const wait = ms => new Promise(r => setTimeout(r, ms));
  const clone = o => JSON.parse(JSON.stringify(o));

  const mock = {
    async getChecklist() { await wait(80); return (checklistCache = cl()); },
    async saveChecklist(next) {
      await wait(200);
      M.checklist = { ...clone(next), version: (M.checklist.version || 1) + 1 };
      return (checklistCache = cl());
    },
    async getDialogs() {
      await wait(100);
      return M.records.map(r => {
        const res = store[r.conversation_id], d = res && normalizeDialog(res, cl());
        return { id: r.conversation_id, channel: r.channel, language: r.language, operator: r.operator,
                 total: d ? d.total : null, gross: d ? d.gross : false, status: d ? 'evaluated' : 'pending',
                 has_appeal: appeals.some(a => a.dialog_id === r.conversation_id && a.status === 'open') };
      });
    },
    async evaluate(g, onlyIds) {
      const partial = Array.isArray(onlyIds) && onlyIds.length;
      if (!partial) M.records.forEach(r => delete store[r.conversation_id]);
      progress.done = 0; progress.total = M.records.length;
      let k = 0;
      const tick = setInterval(() => {
        const r = M.records[k++];
        store[r.conversation_id] = M.evaluate(r, cl(), partial ? onlyIds : null, store[r.conversation_id]);
        progress.done = k;
        if (k >= M.records.length) clearInterval(tick);
      }, partial ? 140 : 260);
      return { started: true };
    },
    async getProgress() { return { ...progress }; },
    async getDialog(id) {
      await wait(120);
      if (!store[id]) throw new Error('Диалог ещё не оценён');
      return normalizeDialog(clone(store[id]), cl());
    },
    async getAllResults() { await wait(150); return Object.values(store).map(r => normalizeDialog(clone(r), cl())); },
    // только для демо: «эталон», который отличается от ответов системы в ~12% случаев
    demoGroundTruth() {
      const rnd = (() => { let h = 7; return () => ((h = Math.imul(h ^ (h >>> 13), 1274126177) >>> 0) / 4294967296); })();
      return Object.values(store).map(r => ({ conversation_id: r.id, criteria: Object.fromEntries(r.criteria.map(c => {
        const def = cl().criteria.find(d => d.id === c.id); if (!def) return [c.id, c.answer];
        if (rnd() > .12) return [c.id, c.answer];
        const opts = CQA.scoring.options(def, r.channel).filter(o => o != c.answer);
        return [c.id, opts[Math.floor(rnd() * opts.length)] ?? c.answer];
      })) }));
    },
    async getAppeals() { await wait(100); return appeals.map(a => normalizeAppeal(clone(a))); },
    async createAppeal({ dialog_id, criterion_id, comment, proposed_answer }) {
      await wait(200);
      const d = normalizeDialog(clone(store[dialog_id]), cl()), c = d.criteria.find(x => x.id === criterion_id);
      const a = { id: appeals.length + 1, dialog_id, criterion_id, comment, proposed_answer, status: 'open',
                  created_at: new Date().toISOString(),
                  original: { answer: c.answer, points: CQA.scoring.applicable(c) ? CQA.scoring.r2(CQA.scoring.points(c)) : null, explanation: c.why },
                  original_total: d.total, decision: null, final_total: null };
      appeals.push(a);
      return normalizeAppeal(clone(a));
    },
    async decideAppeal(id, { action, answer, reason }) {
      await wait(250);
      const a = appeals.find(x => x.id == id);
      const res = store[a.dialog_id], c = res.criteria.find(x => x.id === a.criterion_id);
      if (action === 'change') { c.answer = answer; c.explanation = 'Изменено проверяющим: ' + reason; c.na_reason = null; }
      const d = normalizeDialog(clone(res), cl());
      Object.assign(a, { status: 'resolved', decision: { action, answer: action === 'change' ? answer : a.original.answer, reason, decided_at: new Date().toISOString() }, final_total: d.total });
      return { appeal: normalizeAppeal(clone(a)), dialog: d };
    }
  };

  return cfg.USE_MOCK ? mock : real;
})();
