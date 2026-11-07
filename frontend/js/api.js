/* =========================================================
   ЕДИНСТВЕННАЯ точка связи фронта с бэком (docs/api_contract.md).
   Страницы вызывают только CQA.api.*, ничего не знают про fetch и формат бэка.
   Все данные — из API; заготовленных результатов нет.
   ========================================================= */
CQA.api = (() => {
  const cfg = CQA.config;
  let checklistCache = null;

  /* ---------- HTTP ---------- */
  async function http(method, path, body) {
    const r = await fetch(cfg.API_BASE + path, {
      method, headers: body !== undefined ? { 'Content-Type': 'application/json' } : {},
      body: body !== undefined ? JSON.stringify(body) : undefined
    });
    if (!r.ok) {
      let msg = `${method} ${path} → ${r.status}`;
      try {
        const j = await r.json();
        if (j.error) {
          msg = j.error.message || msg;
          const errs = j.error.details && j.error.details.errors;
          if (Array.isArray(errs) && errs.length) msg += ': ' + errs.map(e => `${e.path}: ${e.message}`).join('; ');
        }
      } catch (_) { /* не JSON */ }
      const err = new Error(msg); err.status = r.status; throw err;
    }
    return r.status === 204 ? null : r.json();
  }
  const q = encodeURIComponent;

  /* ---------- значения ответов: бэк yes|no|na|число ↔ интерфейс Да|Нет|N/A|число ---------- */
  const toUi = a => a === 'yes' ? 'Да' : a === 'no' ? 'Нет' : a === 'na' ? 'N/A' : (a === null || a === undefined ? null : a);
  const toApi = a => a === 'Да' ? 'yes' : a === 'Нет' ? 'no' : a === 'N/A' ? 'na' : a;
  const TYPE_UI = { binary: 'yes_no', scale: 'scale', flag: 'flag' };
  const TYPE_API = { yes_no: 'binary', scale: 'scale', flag: 'flag' };

  function fmtTime(ts) {
    if (!ts) return '';
    const m = /T(\d{2}:\d{2}:\d{2})/.exec(ts);          // звонки: ISO 8601 → 10:01:30
    return m ? m[1] : ts;                                // чаты: MM:SS как есть
  }

  /* ---------- чек-лист: контракт → формат экранов и обратно ---------- */
  function normalizeChecklist(raw) {
    const sectionIds = (raw.sections || []).map(s => s.id);
    return {
      id: raw.id, name: raw.name, version: raw.version, is_latest: raw.is_latest, created_at: raw.created_at,
      author: raw.author, comment: raw.comment, settings: raw.settings || {},
      sections: (raw.sections || []).map(s => s.title), sectionIds,
      criteria: (raw.criteria || []).map(c => {
        const steps = c.type === 'scale' ? (c.scale || []).map(s => +s.value).sort((a, b) => a - b) : undefined;
        const step_labels = {}; (c.scale || []).forEach(s => { step_labels[s.value] = s.description || ''; });
        return {
          id: c.id, section: Math.max(0, sectionIds.indexOf(c.section)), section_id: c.section,
          title: c.title || '', description: c.description || '', type: TYPE_UI[c.type] || 'yes_no',
          weight: +(c.weight ?? 0), steps, step_labels,
          na_allowed: !!c.na_allowed, na_rule: c.na_condition || '', na_channels: [],
          reference: c.reference || '', guards: c.guards, hints: c.hints
        };
      })
    };
  }
  function denormalizeChecklist(cl) {
    const ids = cl.sectionIds || cl.sections.map((_, i) => 's' + i);
    return {
      id: cl.id || cfg.CHECKLIST_ID, name: cl.name || 'Чек-лист качества чатов', settings: cl.settings || {},
      sections: cl.sections.map((title, i) => ({ id: ids[i] || 's' + i, title })),
      criteria: cl.criteria.map(c => {
        const o = { id: c.id, section: ids[c.section] || ids[0], title: c.title, type: TYPE_API[c.type] || 'binary',
                    weight: c.type === 'flag' ? 0 : +c.weight, na_allowed: c.type === 'flag' ? false : !!c.na_allowed,
                    description: c.description || '' };
        if (o.na_allowed) o.na_condition = c.na_rule || '';
        if (o.type === 'scale') o.scale = [...new Set((c.steps || []).map(Number))].sort((a, b) => b - a)
          .map(v => ({ value: v, description: (c.step_labels || {})[v] || '' }));
        if (c.reference && c.reference.trim()) o.reference = c.reference;
        if (c.guards) o.guards = c.guards;
        if (c.hints) o.hints = c.hints;
        return o;
      })
    };
  }

  /* ---------- оценка + диалог → объект карточки ---------- */
  function normalizeEvaluationCriteria(ev, checklist) {
    const byId = Object.fromEntries((ev.criteria || []).map(c => [c.criterion_id, c]));
    return checklist.criteria.map(def => {
      const r = byId[def.id];
      if (!r) return { ...def, answer: null, why: '', evidence: [], quotes: [], range: null, na_reason: '', guards: [], points: null, applicable: false };
      const evd = r.evidence || {};
      const quotes = (evd.quotes || []).map(x => ({ n: x.n, text: x.text, model_quote: x.model_quote }));
      return { ...def, weight: +(r.weight ?? def.weight), answer: toUi(r.answer), why: r.explanation || '',
        evidence: quotes.map(x => x.n), quotes, range: evd.range || null, na_reason: evd.na_condition || '',
        evidence_kind: evd.kind, guards: r.guards || [], hints: r.hints || [], confidence: r.confidence,
        votes: r.votes || [], model_answer: toUi(r.model_answer), violation_type: r.violation_type,
        points: r.points, applicable: !!r.applicable };
    });
  }
  function normalizeDialog(conv, ev, checklist, appeals, final) {
    const transcript = (conv.messages || []).map(m => ({
      i: m.n, t: fmtTime(m.timestamp), s: m.speaker, x: m.text, clean: m.text_clean || null,
      flags: m.flags || [], segment: m.segment, counts: m.counts_for_operator, event: m.event, offset: m.audio_offset_s
    }));
    const base = {
      id: conv.conversation_id, group_id: conv.group_id, channel: conv.channel, language: conv.language,
      audio_url: conv.audio ? conv.audio.url : null, facts: conv.facts || {}, normalization: conv.normalization || null,
      transcript, sections: checklist.sections, appeals: appeals || []
    };
    if (!ev) return { ...base, status: 'pending', evaluation_id: null, criteria: [], total: null, final_total: null, gross: false, flags: [] };
    const criteria = ev.status === 'completed' ? normalizeEvaluationCriteria(ev, checklist) : [];
    return { ...base, status: ev.status === 'completed' ? 'evaluated' : ev.status, status_reason: ev.status_reason || null,
      evaluation_id: ev.id, checklist_version: ev.checklist && ev.checklist.version, created_at: ev.created_at,
      model: ev.model, prompt_version: ev.prompt_version, votes: ev.votes, cost: ev.cost,
      criteria, total: ev.total, final_total: final ? final.total : (ev.final_total ?? ev.total), final: final || null,
      sectionScores: ev.sections || [], flags: (ev.flags || []).filter(f => f.answer === 'yes').map(f => f.criterion_id),
      gross: (ev.flags || []).some(f => f.answer === 'yes') };
  }
  function normalizeAppeal(a) {
    const resolved = a.status === 'confirmed' || a.status === 'changed';
    return {
      id: a.id, evaluation_id: a.evaluation_id, dialog_id: a.conversation_id, criterion_id: a.criterion_id,
      comment: a.comment || '', author: a.author || '', created_at: a.created_at || null,
      status: resolved ? 'resolved' : 'open', status_raw: a.status, events: a.events || [],
      original: { answer: toUi(a.original && a.original.answer), points: a.original ? a.original.points : null },
      decision: a.decision ? { action: a.decision.decision, answer: toUi(a.decision.new_answer ?? (a.original && a.original.answer)),
                               reason: a.decision.reason || '', reviewer: a.decision.reviewer || '', decided_at: a.decision.decided_at } : null,
      original_total: null, final_total: null                   // заполняются страницами из оценки и final
    };
  }

  /* ---------- API ---------- */
  const api = {
    toUi, toApi, fmtTime,
    getMeta: () => http('GET', '/api/meta'),

    // чек-листы с версиями
    async getChecklist(version) {
      const path = version ? `/api/checklists/${q(cfg.CHECKLIST_ID)}/versions/${version}` : `/api/checklists/${q(cfg.CHECKLIST_ID)}`;
      const cl = normalizeChecklist(await http('GET', path));
      if (!version) checklistCache = cl;
      return cl;
    },
    checklist: async () => checklistCache || api.getChecklist(),
    getChecklistVersions: () => http('GET', `/api/checklists/${q(cfg.CHECKLIST_ID)}/versions`),
    getChecklistDiff: (a, b) => http('GET', `/api/checklists/${q(cfg.CHECKLIST_ID)}/versions/${a}/diff/${b}`),
    validateChecklist: cl => http('POST', '/api/checklists/validate', denormalizeChecklist(cl)),
    async saveChecklist(cl, { base_version, author, comment } = {}) {
      const body = { ...denormalizeChecklist(cl), base_version, author: author || cfg.REVIEWER, comment: comment || '' };
      return (checklistCache = normalizeChecklist(await http('POST', `/api/checklists/${q(cfg.CHECKLIST_ID)}/versions`, body)));
    },

    // наборы и диалоги
    importDataset: (group, prefer = 'remote') => http('POST', '/api/datasets/import', { group_id: group, prefer }),
    getDatasets: () => http('GET', '/api/datasets'),
    async getDialogs(group) {
      const r = await http('GET', `/api/conversations?group_id=${q(group)}&limit=500`);
      return (r.items || []).map(d => {
        const ev = d.latest_evaluation;
        return { id: d.conversation_id, channel: d.channel, language: d.language, messages: d.messages,
          operator_messages: d.operator_messages, has_audio: d.has_audio,
          evaluation_id: ev ? ev.id : null, checklist_version: ev ? ev.checklist.version : null,
          total: ev ? ev.total : null, final_total: ev ? ev.final_total : null,
          status: !ev ? 'pending' : ev.status === 'completed' ? 'evaluated' : ev.status,   // evaluated | not_evaluable | failed | pending
          status_reason: ev ? ev.status_reason : null, gross: !!(ev && ev.flags && ev.flags.length),
          flags: ev ? ev.flags || [] : [], has_appeal: !!(ev && ev.open_appeals) };
      });
    },

    // оценка: один диалог синхронно, набор — пакетом с прогрессом
    evaluateOne: (conversation_id, version) => http('POST', '/api/evaluations',
      { conversation_id, checklist_id: cfg.CHECKLIST_ID, checklist_version: version || null }),
    startBatch: (group, { version, only_not_evaluated = false, concurrency = 4 } = {}) => http('POST', '/api/batches',
      { group_id: group, checklist_id: cfg.CHECKLIST_ID, checklist_version: version || null, only_not_evaluated, concurrency }),
    async getBatch(id) {
      const b = await http('GET', `/api/batches/${q(id)}`);
      const p = b.progress || {};
      return { ...b, done: p.done || 0, total: p.total || 0, finished: ['completed', 'cancelled', 'failed'].includes(b.status) };
    },

    // карточка: диалог + последняя (или указанная) оценка + апелляции + финальный итог
    async getDialog(id, evaluationId) {
      const cl = await api.checklist();
      const conv = await http('GET', `/api/conversations/${q(id)}`);
      let evId = evaluationId;
      if (!evId) {
        const list = await http('GET', `/api/evaluations?conversation_id=${q(id)}&limit=1`);
        evId = list.items && list.items.length ? list.items[0].id : null;
      }
      if (!evId) return normalizeDialog(conv, null, cl, [], null);
      const full = await http('GET', `/api/evaluations/${q(evId)}`);
      const appeals = (full.appeals || []).map(normalizeAppeal);
      const d = normalizeDialog(conv, full.evaluation, cl, appeals, full.final);
      appeals.forEach(a => { a.original_total = d.total; a.final_total = d.final_total; });
      return d;
    },

    // результаты набора (для аналитики): строки с ответами по критериям
    async getAllResults(group) {
      const cl = await api.checklist();
      const r = await http('GET', `/api/results?group_id=${q(group)}&checklist_id=${q(cfg.CHECKLIST_ID)}`);
      return (r.items || []).map(row => ({
        id: row.conversation_id, channel: row.channel, language: row.language, status: row.status, evaluation_id: row.evaluation_id,
        total: row.total, final_total: row.final_total, gross: (row.flags || []).length > 0, flags: row.flags || [],
        criteria: cl.criteria.map(def => ({ ...def, answer: toUi(row.answers ? row.answers[def.id] : null),
                                           final_answer: toUi(row.final_answers ? row.final_answers[def.id] : null) }))
      }));
    },

    // апелляции
    async getAppeals(filters = {}) {
      const qs = Object.entries(filters).filter(([, v]) => v).map(([k, v]) => `${k}=${q(v)}`).join('&');
      const r = await http('GET', '/api/appeals' + (qs ? '?' + qs : ''));
      return (r.items || []).map(normalizeAppeal);
    },
    async createAppeal({ evaluation_id, criterion_id, comment, proposed_answer }) {
      const text = proposed_answer !== null && proposed_answer !== undefined
        ? `${comment}\n\nПредлагаемая оценка: ${proposed_answer}` : comment;
      return normalizeAppeal(await http('POST', `/api/evaluations/${q(evaluation_id)}/appeals`,
        { criterion_id, comment: text, author: cfg.AUTHOR }));
    },
    reviewAppeal: id => http('POST', `/api/appeals/${q(id)}/review`, { reviewer: cfg.REVIEWER }).then(normalizeAppeal),
    async decideAppeal(id, { action, answer, reason, dialog_id }) {
      const res = await http('POST', `/api/appeals/${q(id)}/decision`,
        { decision: action, new_answer: action === 'change' ? toApi(answer) : null, reason, reviewer: cfg.REVIEWER });
      const appeal = normalizeAppeal(res.appeal);
      const dialog = await api.getDialog(dialog_id || appeal.dialog_id, appeal.evaluation_id);
      appeal.original_total = dialog.total; appeal.final_total = dialog.final_total;
      return { appeal, dialog, final: res.final };
    }
  };
  return api;
})();
