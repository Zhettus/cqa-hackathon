/* Общие визуальные штуки: 3D-объекты, анимации чисел, наклон карточек, параллакс. */
CQA.ui = (() => {
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const esc = s => String(s ?? '').replace(/[&<>"]/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[ch]));

  const art = {
    bubble: (s = 1) => `<div class="bubble" style="transform:scale(${s});transform-origin:bottom right"><i></i><i></i><i></i></div>`,
    shield: () => `<div class="shield"><svg viewBox="0 0 110 126"><path d="M55 8 12 24v34c0 30 19 50 43 60 24-10 43-30 43-60V24L55 8z" fill="#2F3FA8" transform="translate(4 6)"/><path d="M55 4 12 20v34c0 30 19 50 43 60 24-10 43-30 43-60V20L55 4z" fill="url(#sh)"/><path d="M55 4 12 20v34c0 8 1.5 15 4 21L55 14l41 12v-6L55 4z" fill="#fff" opacity=".22"/><path d="m36 60 13 13 25-27" stroke="#fff" stroke-width="9" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg></div>`,
    bars: () => `<div class="bars"><i></i><i></i><i></i><i></i></div>`,
    orb: (bad) => `<div class="okorb ${bad ? 'bad' : ''}"><svg width="30" height="30" viewBox="0 0 24 24"><path d="${bad ? 'M7 7l10 10M17 7 7 17' : 'm5 12.5 4.5 4.5L19 7.5'}" stroke="#fff" stroke-width="3" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg></div>`,
    langs: () => `<div class="langs"><span>RU</span><span>KZ</span><span>EN</span></div>`,
    sphere: () => `<div class="sphere"></div>`,
    cube: () => `<div class="cube"><svg width="34" height="34" viewBox="0 0 24 24"><path d="M5 7h14M5 12h9M5 17h6" stroke="#fff" stroke-width="2.6" stroke-linecap="round"/></svg></div>`
  };

  const chev = '<svg class="chev" viewBox="0 0 16 16"><path d="m4 6 4 4 4-4" stroke="currentColor" stroke-width="1.8" fill="none" stroke-linecap="round"/></svg>';

  function ringHTML(id, r = 72, cap = 'из 100') {
    const size = (r + 13) * 2;
    return `<div class="ring" style="width:${size}px;height:${size}px">
      <svg width="${size}" height="${size}"><circle class="track" cx="${size / 2}" cy="${size / 2}" r="${r}"/><circle class="val" id="${id}-ring" cx="${size / 2}" cy="${size / 2}" r="${r}"/></svg>
      <div class="in"><div class="num" id="${id}-num">0</div><small id="${id}-cap">${cap}</small></div></div>`;
  }
  // плавно меняет число в кольце; возвращает функцию для следующих изменений
  function ring(id, r = 72, dec = 2) {
    const circle = document.getElementById(id + '-ring'), num = document.getElementById(id + '-num');
    const C = 2 * Math.PI * r; let shown = 0;
    circle.style.strokeDasharray = C; circle.style.strokeDashoffset = C;
    return function set(to, from = shown) {
      circle.style.strokeDashoffset = C * (1 - Math.min(Math.max(to || 0, 0), 100) / 100);
      countUp(num, from, to ?? 0, dec);
      shown = to ?? 0;
    };
  }
  function countUp(el, from, to, dec = 0, dur = 1200) {
    if (reduce) { el.textContent = to.toFixed(dec); return; }
    const t0 = performance.now();
    (function tick(t) {
      const p = Math.min((t - t0) / dur, 1), k = 1 - Math.pow(1 - p, 3);
      el.textContent = (from + (to - from) * k).toFixed(dec);
      if (p < 1) requestAnimationFrame(tick);
    })(t0);
  }
  function tilt(root) {
    if (reduce) return;
    root.querySelectorAll('.tile').forEach(t => {
      t.addEventListener('pointermove', e => {
        const r = t.getBoundingClientRect(), x = (e.clientX - r.left) / r.width - .5, y = (e.clientY - r.top) / r.height - .5;
        t.style.transform = `perspective(800px) rotateX(${-y * 7}deg) rotateY(${x * 7}deg) translateY(-3px)`;
      });
      t.addEventListener('pointerleave', () => t.style.transform = '');
    });
  }
  function parallax(hero) {
    const scene = hero.querySelector('.scene'); if (!scene) return;
    const objs = [...scene.querySelectorAll('.obj')];
    objs.forEach(o => o.addEventListener('click', () => pop(o)));
    if (reduce) return;
    hero.addEventListener('pointermove', e => {
      const r = scene.getBoundingClientRect(), x = (e.clientX - r.left - r.width / 2) / r.width, y = (e.clientY - r.top - r.height / 2) / r.height;
      objs.forEach(o => { const d = +o.dataset.d; o.style.transform = `translate3d(${-x * d}px,${-y * d}px,0) rotateY(${x * d * .3}deg) rotateX(${-y * d * .3}deg)`; });
    });
    hero.addEventListener('pointerleave', () => objs.forEach(o => o.style.transform = ''));
  }
  function pop(el) { el.classList.remove('pop'); void el.offsetWidth; el.classList.add('pop'); }
  function toast(msg, bad) {
    const t = document.createElement('div'); t.className = 'toast' + (bad ? ' bad' : ''); t.textContent = msg;
    document.body.appendChild(t); requestAnimationFrame(() => t.classList.add('show'));
    setTimeout(() => { t.classList.remove('show'); setTimeout(() => t.remove(), 400); }, 3200);
  }

  // всплывающие подсказки для всех элементов с data-tip
  function tips() {
    const tip = document.createElement('div'); tip.className = 'tip'; document.body.appendChild(tip);
    document.addEventListener('pointerover', e => { const t = e.target.closest('[data-tip]'); if (!t) return; tip.innerHTML = t.dataset.tip; tip.classList.add('show'); });
    document.addEventListener('pointermove', e => { if (!tip.classList.contains('show')) return;
      const x = Math.min(e.clientX + 14, innerWidth - tip.offsetWidth - 8), y = e.clientY - tip.offsetHeight - 12;
      tip.style.transform = `translate(${x}px,${y < 8 ? e.clientY + 18 : y}px)`; });
    document.addEventListener('pointerout', e => { if (e.target.closest('[data-tip]') && !e.relatedTarget?.closest?.('[data-tip]')) tip.classList.remove('show'); });
  }

  return { tips, reduce, esc, art, chev, ringHTML, ring, countUp, tilt, parallax, pop, toast };
})();
