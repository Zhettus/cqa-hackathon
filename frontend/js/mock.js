/* Тестовые данные: в том же формате, что вернёт бэк.
   Когда бэк готов — этот файл не нужен (USE_MOCK = false). */
CQA.mock = (() => {
  const checklist = {
    version: 1,
    sections: ['Начало и понимание запроса', 'Безопасность и решение', 'Ведение и завершение', 'Грубые нарушения'],
    criteria: [
      { id: '01', section: 0, title: 'Установление контакта', type: 'yes_no', weight: 10,
        description: 'Да — оператор поздоровался на языке клиента и представился в первом ответе.',
        na_allowed: true, na_channels: ['voice call'], na_rule: 'Приветствие оператора не попало в запись звонка.' },
      { id: '02', section: 0, title: 'Выявление потребности', type: 'yes_no', weight: 15,
        description: 'Да — оператор учёл известные детали и уточнил недостающее до ответа.',
        na_allowed: true, na_rule: 'Данных для ответа хватало с самого начала.' },
      { id: '03', section: 1, title: 'Идентификация', type: 'yes_no', weight: 20,
        description: 'Да — проверка личности подтверждена до раскрытия данных или защищённого действия.',
        na_allowed: true, na_rule: 'Запрос не требовал идентификации.' },
      { id: '04', section: 1, title: 'Корректная и полная консультация', type: 'scale', weight: 20, steps: [0, 5, 10, 15, 20],
        step_labels: { 20: 'Все существенные вопросы закрыты или дан корректный следующий шаг', 15: 'Основной вопрос решён, пропущена второстепенная деталь',
                       10: 'Часть запроса закрыта, существенный вопрос остался открытым', 5: 'Есть общая подсказка, но клиенту непонятно, как действовать',
                       0: 'Ответа по существу нет или ключевая рекомендация ошибочна' },
        description: 'Оцените ответ или следующий шаг по шкале.',
        na_allowed: true, na_rule: 'Чат закончился до того, как клиент сформулировал запрос.' },
      { id: '05', section: 2, title: 'Эмпатия и клиентоориентированность', type: 'yes_no', weight: 15,
        description: 'Да — оператор вежлив и отреагировал на конкретную проблему клиента.', na_allowed: false, na_rule: '' },
      { id: '06', section: 2, title: 'Управление диалогом', type: 'scale', weight: 15, steps: [0, 4, 7, 10],
        step_labels: { 10: 'Ответы ясные и связные, об ожидании предупредил', 7: 'Один заметный сбой, но клиент может продолжить',
                       4: 'Клиенту приходится переспрашивать', 0: 'Ход диалога непонятен' },
        description: 'Ясность, порядок ответов и предупреждение об ожидании.', na_allowed: false, na_rule: '' },
      { id: '07', section: 2, title: 'Завершение', type: 'yes_no', weight: 5,
        description: 'Да — оператор подвёл итог или предложил помощь и попрощался.',
        na_allowed: true, na_rule: 'Чат оборвался до возможности его завершить.' },
      { id: '08', section: 3, title: 'Грубые ошибки', type: 'flag', weight: 0,
        description: 'Да — оскорбление клиента, предложение платной услуги в обход компании или раскрытие конфиденциальной информации.',
        na_allowed: false, na_rule: '' }
    ]
  };

  const T = {
    RU: ['Здравствуйте, хочу открыть брокерский счёт. Какие комиссии и как пополнить?', 'Здравствуйте! Меня зовут {op}, сейчас помогу.',
         'Открытие счёта бесплатное. Комиссия за сделки зависит от тарифа, пополнить можно банковским переводом.', 'А можно начать с небольшой суммы?',
         'Да, начать можно с любой суммы. Если появятся вопросы по тарифу — я на связи.', 'Спасибо за обращение, хорошего дня!',
         'Бот: соединяю с оператором', 'Оператор {op} подключился к чату'],
    KZ: ['Сәлеметсіз бе, шотқа ақшаны қалай салуға болады?', 'Сәлеметсіз бе! Менің атым {op}, қазір көмектесемін.',
         'Банк аударымы арқылы немесе картамен толтыруға болады.', 'Комиссия бар ма?',
         'Толтыру үшін комиссия жоқ. Басқа сұрақтарыңыз болса, жазыңыз.', 'Хабарласқаныңызға рақмет, сау болыңыз!',
         'Бот: операторға қосып жатырмын', '{op} операторы чатқа қосылды'],
    EN: ['Hi, how can I withdraw money from my brokerage account?', 'Hello! My name is {op}, happy to help.',
         'You can request a withdrawal in the app under Account → Withdraw; it usually takes one business day.', 'Is there a fee?',
         'Withdrawals to a local bank are free. Let me know if anything else comes up.', 'Thanks for reaching out, have a great day!',
         'Bot: connecting you to an operator', 'Operator {op} joined the chat']
  };
  const ops = ['Айгерим', 'Дана', 'Ержан', 'Алина', 'Тимур', 'Madina'];

  function seed(str) { let h = 2166136261; for (const ch of str) h = Math.imul(h ^ ch.charCodeAt(0), 16777619); return () => ((h = Math.imul(h ^ (h >>> 15), 2246822507) >>> 0) / 4294967296); }

  function makeTranscript(lang, op, isCall) {
    const t = T[lang], f = s => s.replace('{op}', op);
    const times = isCall
      ? ['2026-11-01T10:00:02+05:00', '2026-11-01T10:00:05+05:00', '2026-11-01T10:00:38+05:00', '2026-11-01T10:00:41+05:00', '2026-11-01T10:01:30+05:00', '2026-11-01T10:01:52+05:00', '2026-11-01T10:02:10+05:00', '2026-11-01T10:02:45+05:00']
      : ['00:02', '00:05', '00:38', '00:41', '01:30', '01:52', '02:10', '02:45'];
    return [
      { speaker: 'client', text: t[0] }, { speaker: 'bot', text: t[6] }, { speaker: 'system', text: f(t[7]) },
      { speaker: 'operator', text: f(t[1]) }, { speaker: 'operator', text: t[2] }, { speaker: 'client', text: t[3] },
      { speaker: 'operator', text: t[4] }, { speaker: 'operator', text: t[5] }
    ].map((m, k) => ({ i: k + 1, timestamp: times[k], ...m }));
  }

  const langs = ['RU', 'RU', 'KZ', 'EN', 'RU', 'KZ', 'EN', 'RU', 'RU', 'KZ', 'EN', 'RU', 'RU', 'EN', 'KZ', 'RU'];
  const records = langs.map((lang, k) => {
    const n = String(k + 1).padStart(3, '0'), isCall = k === 4 || k === 11, op = ops[k % ops.length];
    return { conversation_id: `p1-${lang.toLowerCase()}-${n}`, channel: isCall ? 'voice call' : 'agent chat', language: lang,
             operator: op, transcript: makeTranscript(lang, op, isCall), audio_url: isCall ? 'https://example.com/audio.mp3' : null };
  });

  const evidenceFor = { '01': [4], '02': [4], '03': [5], '04': [5], '05': [7], '06': [5], '07': [8] };
  const fixed001 = { '01': 'Да', '02': 'N/A', '03': 'N/A', '04': 15, '05': 'Да', '06': 7, '07': 'Да', '08': 'Нет' };

  function explain(def, a) {
    if (a === 'N/A') return def.na_rule || 'Критерий неприменим к этому диалогу.';
    if (def.type === 'scale') return def.step_labels?.[a] || `Ответ оценён на ${a} по шкале критерия.`;
    if (def.type === 'flag') return a === 'Да' ? 'Выявлено нарушение: ' + def.description.replace(/^Да — /, '') : 'Нарушений по этому критерию не выявлено.';
    const d = def.description.replace(/^Да — /, '');
    return a === 'Да' ? 'Выполнено: ' + d : 'Не выполнено: ' + d;
  }

  // оценка одного критерия «как будто ML»
  function judge(rec, def, version) {
    if (rec.conversation_id === 'p1-ru-001' && fixed001[def.id] !== undefined && version === 1) return fixed001[def.id];
    const rnd = seed(rec.conversation_id + def.id + def.title + version);
    const na = CQA.scoring.naAllowed(def, rec.channel);
    if (def.type === 'flag') return rnd() < .12 ? 'Да' : 'Нет';
    if (na && rnd() < .2) return 'N/A';
    if (def.type === 'scale') { const s = [...def.steps].sort((a, b) => a - b); return s[Math.max(0, s.length - 1 - Math.floor(rnd() * rnd() * s.length))]; }
    return rnd() < .8 ? 'Да' : 'Нет';
  }

  // onlyIds — переоценить только эти критерии (остальные берутся из prev)
  function evaluate(rec, cl, onlyIds, prev) {
    const criteria = cl.criteria.map(def => {
      const old = prev && prev.criteria.find(c => c.id === def.id);
      if (old && onlyIds && !onlyIds.includes(def.id)) return old;
      const a = judge(rec, def, cl.version);
      return { id: def.id, answer: a,
               explanation: a === 'N/A' ? null : explain(def, a), na_reason: a === 'N/A' ? explain(def, a) : null,
               evidence: a === 'N/A' || def.type === 'flag' ? [] : (evidenceFor[def.id] || [7]) };
    });
    return { id: rec.conversation_id, channel: rec.channel, language: rec.language, operator: rec.operator,
             audio_url: rec.audio_url, transcript: rec.transcript, checklist_version: cl.version, criteria };
  }

  return { checklist, records, evaluate };
})();
