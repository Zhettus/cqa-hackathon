"""Пакетная оценка: фоновая задача в процессе API. Прогресс — в таблицах batches / batch_items,
сами оценки — обычные записи evaluations (с batch_id). Отмена: уже готовые оценки сохраняются,
очередь помечается cancelled.

Ошибки провайдера: аутентификация и биллинг останавливают пакет (status failed с понятным сообщением,
оставшиеся диалоги — provider_error, оценённые сохранены); лимит запросов (429) — пакет ждёт и повторяет
диалог с растущей паузой (RATE_LIMIT_BACKOFF_S × 2^n), после исчерпания попыток — provider_error."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Callable

from backend import db
from backend.evaluator import Evaluator
from backend.llm import LLMError, LLMProvider, retry_rate_limited

log = logging.getLogger(__name__)

STOP_CODES = ("auth", "billing")
PROVIDER_MESSAGES = {
    "auth": "Провайдер отклонил ключ (аутентификация). Пакет остановлен, оценённые диалоги сохранены. "
            "Проверьте LLM_API_KEY в .env и перезапустите сервер.",
    "billing": "У провайдера закончился баланс/квота (биллинг). Пакет остановлен, оценённые диалоги сохранены. "
               "Пополните баланс или переключите LLM_PROVIDER/LLM_BASE_URL в .env.",
}


class BatchRunner:
    def __init__(self, db_path: str | Path, provider_factory: Callable[[], LLMProvider]):
        self.db_path = db_path
        self.provider_factory = provider_factory
        self.tasks: dict[str, asyncio.Task] = {}

    def start(self, batch_id: str) -> asyncio.Task:
        task = asyncio.create_task(self.run(batch_id), name=f"batch:{batch_id}")
        self.tasks[batch_id] = task
        task.add_done_callback(lambda t: self.tasks.pop(batch_id, None))
        return task

    async def run(self, batch_id: str) -> None:
        conn = db.connect(self.db_path)
        try:
            batch = db.get_batch(conn, batch_id)
            if batch["status"] != "queued":
                return
            db.set_batch_status(conn, batch_id, "running")
            evaluator = Evaluator(conn, self.provider_factory())
            sem = asyncio.Semaphore(max(1, batch["concurrency"]))
            cid_, ver = batch["checklist"]["id"], batch["checklist"]["version"]
            stop: dict[str, LLMError | None] = {"error": None}

            async def one(conversation_id: str) -> None:
                async with sem:
                    if stop["error"] is not None:
                        db.set_batch_item(conn, batch_id, conversation_id, "provider_error",
                                          error=f"не оценён: {stop['error'].code}")
                        return
                    if db.batch_cancel_requested(conn, batch_id):
                        db.set_batch_item(conn, batch_id, conversation_id, "cancelled")
                        return
                    db.set_batch_item(conn, batch_id, conversation_id, "running")
                    try:
                        ev = await retry_rate_limited(
                            lambda: evaluator.evaluate(conversation_id, cid_, ver, batch_id=batch_id),
                            on_wait=lambda s, n: db.set_batch_item(conn, batch_id, conversation_id, "running",
                                                                   error=f"лимит запросов, ожидание {s:.0f} с (попытка {n})"))
                    except LLMError as e:
                        db.set_batch_item(conn, batch_id, conversation_id, "provider_error", error=str(e))
                        if e.code in STOP_CODES:
                            stop["error"] = e                  # остальные диалоги не начинаем
                        return
                    except Exception as e:                     # страховка: пакет не должен падать из-за одного диалога
                        db.set_batch_item(conn, batch_id, conversation_id, "failed", error=f"{type(e).__name__}: {e}")
                        return
                    db.set_batch_item(conn, batch_id, conversation_id, ev["status"], evaluation_id=ev["id"],
                                      total=ev.get("total"),
                                      error=ev.get("status_reason") if ev["status"] == "failed" else None)

            queued = [i["conversation_id"] for i in batch["items"] if i["status"] == "queued"]
            await asyncio.gather(*(one(c) for c in queued))
            if stop["error"] is not None:
                e = stop["error"]
                db.set_batch_status(conn, batch_id, "failed", error=f"{PROVIDER_MESSAGES.get(e.code, e.code)} ({e})")
            elif db.batch_cancel_requested(conn, batch_id):
                db.set_batch_status(conn, batch_id, "cancelled")
            else:
                db.set_batch_status(conn, batch_id, "completed")
        except Exception as e:  # noqa: BLE001 — любой сбой раннера фиксируется в статусе пакета
            log.exception("пакет %s: сбой раннера", batch_id)
            try:
                db.set_batch_status(conn, batch_id, "failed", error=f"{type(e).__name__}: {e}")
            except Exception:  # noqa: BLE001
                pass
        finally:
            conn.close()
