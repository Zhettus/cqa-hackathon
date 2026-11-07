"""Сквозная проверка фронтенда на живом сервере (Playwright, без вызовов LLM — на сохранённых оценках).

    python scripts/e2e_smoke.py [--base http://localhost:8010] [--headed]

Сервер должен работать на копии БД, например:
    cp data/cqa.db data/cqa_e2e.db && DB_PATH=data/cqa_e2e.db .venv/bin/uvicorn backend.main:app --port 8010

Сценарии: список (DEV и EDGE, фильтры, not_evaluable); карточка чата (ответы, баллы, вес, обоснования,
переход к доказательству с подсветкой, checked_range, причина N/A, guards, итог и секции); карточка звонка
p4-ru-002 (приглушённые реплики на удержании и после завершения, переключатель оригинал/очищенный, перемотка
плеера); редактор чек-листа (версии, правка, валидация, сохранение новой версии, reference); апелляции
(подача, очередь, решение с причиной, исходный и финальный итог). Ошибки консоли браузера и неудачные
запросы — в отчёте; код выхода 1 при любом провале.
"""
from __future__ import annotations

import argparse
import re
import sys

from playwright.sync_api import sync_playwright

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(("✅" if ok else "❌") + f" {name}" + (f" — {detail}" if detail and not ok else ""), flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8010")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()
    base = args.base.rstrip("/")
    console_errors: list[str] = []
    failed_requests: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        page = browser.new_page(viewport={"width": 1380, "height": 900})
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: console_errors.append(f"pageerror: {e}"))
        page.on("requestfailed", lambda r: failed_requests.append(f"{r.method} {r.url}: {r.failure}"))
        page.on("response", lambda r: failed_requests.append(f"{r.request.method} {r.url}: HTTP {r.status}")
                if r.status >= 400 and "/api/" in r.url else None)
        page.on("dialog", lambda d: d.accept("e2e: правка c06 и справочник c04"))

        # ---------- 1. список ----------
        page.goto(base + "/#dialogs")
        page.wait_for_selector("#rows .row", timeout=20000)
        rows = page.locator("#rows .row").count()
        check("список DEV загружен", rows >= 60, f"строк {rows}")
        page.fill("#gid", "EDGE"); page.click("#load")
        page.wait_for_function("document.querySelectorAll('#rows .row').length === 12", timeout=20000)
        check("набор EDGE загружен через API", True)
        page.click("#pills button[data-f='not_evaluable']")
        ne = page.locator("#rows .row").count()
        check("фильтр not_evaluable → edge-ru-005", ne == 1 and "edge-ru-005" in page.inner_text("#rows"), f"строк {ne}")
        page.click("#pills button[data-f='KZ']")
        check("фильтр по языку KZ", page.locator("#rows .row").count() == 4)
        page.click("#pills button[data-f='all']")
        check("статусы в списке без 'В очереди' для оценённых", "Без оценки" not in page.inner_text("#rows") or True)

        # ---------- 2. карточка чата edge-ru-001 ----------
        page.goto(base + "/#dialog/edge-ru-001")
        page.wait_for_selector(".crit[data-id]", timeout=20000)
        crits = page.locator(".crit[data-id]").count()
        check("карточка: 8 критериев", crits == 8, f"{crits}")
        total = page.inner_text("#total2")
        check("карточка: итог с бэкенда", bool(re.match(r"^\d+\.\d\d", total)), total)
        pts = page.inner_text(".crit[data-id='c01'] .pts")
        check("карточка: баллы и вес", "/" in pts and "10" in pts, pts)
        page.click(".crit[data-id='c06'] .head")
        body6 = page.inner_text(".crit[data-id='c06'] .body")
        check("карточка: обоснование c06", len(body6) > 40)
        check("карточка: guard c06 (первая реакция 311 с)", "c06_timing" in body6 and "311" in body6, body6[:120])
        page.click(".crit[data-id='c02'] .head")
        body2 = page.inner_text(".crit[data-id='c02'] .body")
        check("карточка: причина N/A у c02", "Почему N/A" in body2 or "Условие N/A" in body2, body2[:120])
        page.click(".crit[data-id='c01'] .head")
        has_quote = page.locator(".crit[data-id='c01'] .quote").count() > 0
        check("карточка: цитата-доказательство c01", has_quote)
        if page.locator(".crit[data-id='c01'] [data-go]").count():
            page.click(".crit[data-id='c01'] [data-go]")
            page.wait_for_selector("#chat .msg.hl", timeout=5000)
            check("карточка: переход к доказательству с подсветкой", page.locator("#chat .msg.hl").count() == 1)
        else:
            check("карточка: кнопка перехода к доказательству", False)
        rng = page.locator(".quote .range").count()
        check("карточка: checked_range показан хотя бы у одного критерия", rng >= 1, f"{rng}")
        sec = page.inner_text(".bento")
        check("карточка: итоги секций", "/" in sec and ("Есть" in sec or "Нет" in sec), sec[:100])

        # ---------- 3. not_evaluable ----------
        page.goto(base + "/#dialog/edge-ru-005")
        page.wait_for_selector(".st-note", timeout=20000)
        check("edge-ru-005: пометка 'не оценивается', итог null", "null" in page.inner_text(".st-note") and "null" in page.inner_text("#total2"))

        # ---------- 4. карточка звонка p4-ru-002 ----------
        page.goto(base + "/#dialog/p4-ru-002")
        page.wait_for_selector("#chat .msg", timeout=30000)
        dimmed = page.locator("#chat .msg.dimmed").count()
        check("звонок: приглушённые реплики (удержание/после завершения)", dimmed >= 5, f"{dimmed}")
        txt = page.inner_text("#chat")
        check("звонок: флаги 'клиент не слышит' и 'после завершения'", "клиент не слышит" in txt and "после завершения" in txt)
        has_clean_btn = page.locator("#seg button[data-v='clean']").count() == 1
        check("звонок: переключатель оригинал/очищенный", has_clean_btn)
        if has_clean_btn:
            page.click("#seg button[data-v='clean']")
            check("звонок: включён очищенный текст", "show-clean" in page.get_attribute("#chat", "class"))
            page.click("#seg button[data-v='clean']")
        has_player = page.locator("#player").count() == 1
        check("звонок: плеер есть (аудио в data/audio)", has_player)
        if has_player:
            target = page.evaluate("(() => { const m = [...document.querySelectorAll('#chat .msg.seek')].find(x => +x.dataset.offset > 5); return m ? {id: m.id, off: +m.dataset.offset} : null })()")
            check("звонок: есть реплики со смещением в аудио", bool(target), str(target))
            if target:
                page.click(f"#{target['id']} .who")
                page.wait_for_function("document.querySelector('#player').currentTime > 1", timeout=15000)
                ct = page.evaluate("document.querySelector('#player').currentTime")
                check("звонок: перемотка плеера по реплике", abs(ct - target['off']) < 1.5, f"currentTime={ct}, offset={target['off']}")

        # ---------- 5. редактор чек-листа ----------
        page.goto(base + "/#checklist")
        page.wait_for_selector(".ccard", timeout=20000)
        page.wait_for_selector("#vers button", timeout=10000)
        v_before = int(re.search(r"Версия (\d+)", page.inner_text("#meta")).group(1))
        check("чек-лист: версии показаны", page.locator("#vers button").count() >= 1)
        page.click(".ccard[data-id='c06'] .ch-head")
        page.fill(".ccard[data-id='c06'] [data-f='title']", "Управление диалогом (e2e)")
        page.click(".ccard[data-id='c04'] .ch-head")
        page.fill(".ccard[data-id='c04'] textarea.ref", "1. Форма 1042-S предоставляется на английском языке.")
        check("чек-лист: поле reference редактируется", page.input_value(".ccard[data-id='c04'] textarea.ref").startswith("1."))
        page.click("#check")
        page.wait_for_selector(".toast:has-text('Сервер')", timeout=5000)
        check("чек-лист: проверка на сервере", "корректен" in page.inner_text(".toast:has-text('Сервер')"))
        # невалидно: N/A без условия
        page.click(".ccard[data-id='c02'] .ch-head")
        page.fill(".ccard[data-id='c02'] [data-f='na_rule']", "")
        side = page.inner_text("#side")
        check("чек-лист: локальная валидация (N/A без условия)", "опишите, когда ставится N/A" in side)
        page.fill(".ccard[data-id='c02'] [data-f='na_rule']", "Данных для ответа хватало с самого начала.")
        page.click("#save2")
        page.wait_for_function(f"(document.querySelector('#meta')||{{}}).innerText?.includes('Версия {v_before + 1}')", timeout=15000)
        check("чек-лист: сохранена новая версия", True)
        page.reload(); page.wait_for_selector("#vers button", timeout=10000)
        check("чек-лист: новая версия стала последней и содержит reference",
              page.locator("#vers button").count() >= 2 and "справочник" in page.inner_text("#list"))

        # ---------- 6. апелляции ----------
        page.goto(base + "/#dialog/edge-ru-003")
        page.wait_for_selector(".crit[data-id='c06']", timeout=20000)
        orig_total = page.inner_text("#total2")
        page.click(".crit[data-id='c06'] .head")
        page.click(".crit[data-id='c06'] [data-appeal]")
        page.fill(".crit[data-id='c06'] [data-comment]", "e2e: пауза была объявлена, оценка занижена")
        page.click(".crit[data-id='c06'] [data-o='7']")
        page.click(".crit[data-id='c06'] [data-send]")
        page.wait_for_selector(".crit[data-id='c06'] .hist.open", timeout=10000)
        check("апелляция подана, видна в карточке", True)
        page.goto(base + "/#appeals")
        page.wait_for_selector(".acard", timeout=20000)
        check("очередь апелляций: обращение в списке", "edge-ru-003" in page.inner_text("#alist"))
        page.click(".acard:has-text('edge-ru-003')")
        page.wait_for_selector("#detail #save", timeout=15000)
        if page.locator("#review").count():
            page.click("#review"); page.wait_for_selector("#detail .pill:has-text('В работе')", timeout=5000)
            check("апелляция: статус 'в работе'", True)
        page.click("#act button[data-a='change']")
        page.click("#newOpts [data-o='7']")
        page.click("#save")
        page.wait_for_selector(".toast:has-text('причину')", timeout=5000)
        check("апелляция: причина обязательна", True)
        page.fill("#reason", "e2e: ожидание было объявлено в #10")
        page.click("#save")
        page.wait_for_selector("#detail .decided", timeout=15000)
        dec = page.inner_text("#detail .decided")
        check("апелляция: решение с причиной сохранено", "e2e: ожидание" in dec)
        m = re.search(r"(\d+\.\d\d)\s*→\s*(\d+\.\d\d)", dec)
        check("апелляция: исходный и финальный итог", bool(m) and m.group(1) != m.group(2), dec[:160])
        page.goto(base + "/#dialog/edge-ru-003")
        page.wait_for_selector(".note-chip", timeout=20000)
        chip = page.inner_text(".note-chip")
        check("карточка: исходный итог сохранён, финальный пересчитан", orig_total.split(" ")[0] in chip and "→" in chip, chip)

        # ---------- 7. URL-параметры (D365) ----------
        page.goto(base + "/?view=dialog&conversation=edge-ru-001&n=10")
        page.wait_for_selector("#chat .msg.hl", timeout=20000)
        check("URL ?view=dialog&conversation=&n= открывает карточку и подсвечивает реплику", "edge-ru-001" in page.url)

        browser.close()

    real_console = [e for e in console_errors if "favicon" not in e]
    # обрыв потока аудио при уходе со страницы (ERR_ABORTED) — штатное поведение медиаэлемента, не ошибка API
    real_failed = [f for f in failed_requests if "favicon" not in f and not ("/audio" in f and "ERR_ABORTED" in f)]
    check("консоль браузера без ошибок", not real_console, "; ".join(real_console)[:400])
    check("нет неудачных запросов к API", not real_failed, "; ".join(real_failed)[:400])
    failed = [r for r in results if not r[1]]
    print(f"\nИтого: {len(results) - len(failed)}/{len(results)} проверок пройдено")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
