/* =========================================================
   Конфигурация фронтенда. Адрес API — в ОДНОМ месте.
   API_BASE = '' → относительные пути (тот же источник, что и страница: FastAPI отдаёт frontend/ на /).
   Для встраивания со стороннего источника (Dynamics 365) — указать адрес API и добавить его
   в CORS_ORIGINS бэкенда. Параметры страницы — через URL:
     ?view=dialogs|dialog|checklist|appeals|analytics  &conversation=<id>  &group=<group_id>  &n=<реплика>
   ========================================================= */
window.CQA = window.CQA || {};
(() => {
  const params = new URLSearchParams(location.search);
  CQA.config = {
    API_BASE: params.get('api') || '',          // '' — тот же источник
    GROUP_ID: params.get('group') || 'DEV',
    CHECKLIST_ID: 'chat-quality',
    POLL_MS: 1500,                              // как часто спрашивать прогресс пакета
    AUTHOR: params.get('user') || 'operator',   // кто подаёт апелляцию (в D365 — из контекста пользователя)
    REVIEWER: params.get('reviewer') || params.get('user') || 'qa',
    params
  };
  // Параметры URL → стартовый экран (хэш внутри приложения остаётся основным способом навигации)
  if (!location.hash) {
    const view = params.get('view'), conv = params.get('conversation') || params.get('conversation_id');
    if (conv) location.hash = '#dialog/' + encodeURIComponent(conv);
    else if (view && view !== 'dialog') location.hash = '#' + view;
  }
})();
