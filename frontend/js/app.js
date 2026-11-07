/* Роутер: экраны переключаются по адресу после # */
CQA.pages = CQA.pages || {};
CQA.pages.soon = (title, text) => async app => {
  app.innerHTML = `<div class="wrap"><div class="empty"><h2>${title}</h2><p>${text}</p><a class="btn btn-o" href="#dialogs">← К диалогам</a></div></div>`;
};
CQA.pages.checklist = CQA.pages.checklist || CQA.pages.soon('Чек-лист', 'Экран редактора чек-листа в работе.');
CQA.pages.appeals = CQA.pages.appeals || CQA.pages.soon('Апелляции', 'Экран проверяющего в работе.');

(function () {
  const app = document.getElementById('app');
  function route() {
    const [page, arg] = (location.hash.slice(1) || 'dialogs').split('/');
    const tab = page === 'dialog' ? 'dialogs' : page;
    document.querySelectorAll('.links a').forEach(a => a.classList.toggle('active', a.getAttribute('href') === '#' + tab));
    const render = CQA.pages[page] || CQA.pages.dialogs;
    window.scrollTo(0, 0);
    render(app, arg && decodeURIComponent(arg));
  }
  window.addEventListener('hashchange', route);
  document.querySelectorAll('.lang button').forEach(b => b.addEventListener('click', () => CQA.i18n.set(b.textContent.trim())));
  CQA.i18n.start();
  CQA.ui.tips();
  if (CQA.config.USE_MOCK) document.getElementById('mockBadge').hidden = false;
  route();
})();
