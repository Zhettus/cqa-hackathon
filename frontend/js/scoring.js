/* Подсчёт по правилам чек-листа.
   Критерий: {id, section, title, description, type, weight, steps?, step_labels?,
              na_allowed, na_channels?, na_rule, answer, evidence[], why}
   type: 'yes_no' | 'scale' | 'flag'
   answer: 'Да' | 'Нет' | 'N/A' | число шкалы | null (ещё не оценён) */
CQA.scoring = (() => {
  // половина вверх до 2 знаков (50.625 → 50.63), EPSILON убирает ошибку float (1.005 → 1.01)
  const r2 = v => Math.round((v + Number.EPSILON) * 100) / 100;
  const max = c => c.type === 'scale' ? Math.max(...c.steps) : 1;
  const evaluated = c => c.answer !== null && c.answer !== undefined;
  const applicable = c => c.type !== 'flag' && evaluated(c) && c.answer !== 'N/A';
  const points = c => c.type === 'scale' ? c.answer / max(c) * c.weight : (c.answer === 'Да' ? c.weight : 0);

  // N/A разрешён критерием и (если задано) только для определённых каналов
  const naAllowed = (c, channel) => !!c.na_allowed && (!c.na_channels || !c.na_channels.length || !channel || c.na_channels.includes(channel));

  function total(criteria) {
    const ap = criteria.filter(applicable);
    const W = ap.reduce((s, c) => s + c.weight, 0);
    const E = ap.reduce((s, c) => s + points(c), 0);
    return { total: W ? r2(E / W * 100) : null, W, E: r2(E) };
  }
  function section(criteria, i) {
    const ap = criteria.filter(c => c.section === i && applicable(c));
    return { W: ap.reduce((s, c) => s + c.weight, 0), E: r2(ap.reduce((s, c) => s + points(c), 0)) };
  }
  const weightSum = criteria => criteria.filter(c => c.type !== 'flag').reduce((s, c) => s + (+c.weight || 0), 0);
  const gross = criteria => criteria.some(c => c.type === 'flag' && c.answer === 'Да');
  function state(c) {
    if (!evaluated(c)) return 'na';
    if (c.type === 'flag') return c.answer === 'Да' ? 'zero' : 'full';
    if (c.answer === 'N/A') return 'na';
    const p = points(c);
    return p === c.weight ? 'full' : p === 0 ? 'zero' : 'part';
  }
  const fmt = (c, a) => a === null || a === undefined ? 'Не оценён' : (c.type === 'scale' && a !== 'N/A') ? `${a} из ${max(c)}` : String(a);
  const label = c => fmt(c, c.answer);
  function options(c, channel) {
    const base = c.type === 'scale' ? [...c.steps].sort((a, b) => b - a) : ['Да', 'Нет'];
    return c.type !== 'flag' && naAllowed(c, channel) ? [...base, 'N/A'] : base;
  }
  const typeName = c => c.type === 'scale' ? `Шкала ${Math.min(...c.steps)}–${max(c)}` : c.type === 'flag' ? 'Флаг' : 'Да / Нет';

  return { r2, max, evaluated, applicable, points, naAllowed, total, section, weightSum, gross, state, fmt, label, options, typeName };
})();
