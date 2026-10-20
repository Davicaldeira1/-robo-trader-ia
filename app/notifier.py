"""Alertas pelo Telegram.

As mensagens primeiro entram numa fila no banco de dados (na mesma
transação da operação simulada) e depois são enviadas. Se o Telegram
estiver fora do ar, a mensagem é reenviada mais tarde; ela não se perde
nem é enviada duas vezes.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from .storage import Store, now_ms

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 8
MAX_AGE_MS = 6 * 3600 * 1000  # mensagens com mais de 6 h são descartadas


def money(x: float | None) -> str:
    if x is None:
        return "—"
    sign = "-" if x < 0 else ""
    return f"{sign}US$ {abs(x):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_time(ms: int, tz: str = "America/Sao_Paulo") -> str:
    return datetime.fromtimestamp(ms / 1000, tz=ZoneInfo(tz)).strftime("%d/%m %H:%M")


class TelegramClient:
    def __init__(self, token: str, chat_id: str, client: httpx.Client | None = None):
        self.token = token
        self.chat_id = chat_id
        self.client = client or httpx.Client(timeout=15)

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def _url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"

    def send(self, text: str) -> None:
        resp = self.client.post(self._url("sendMessage"), json={
            "chat_id": self.chat_id, "text": text, "disable_web_page_preview": True})
        if resp.status_code != 200:
            raise RuntimeError(f"Telegram respondeu HTTP {resp.status_code}: {resp.text[:200]}")

    def discover_chats(self) -> list[dict]:
        """Lista as conversas que mandaram mensagem para o bot (para achar o chat_id)."""
        if not self.token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN não configurado.")
        resp = self.client.get(self._url("getUpdates"))
        if resp.status_code != 200:
            raise RuntimeError(f"Telegram respondeu HTTP {resp.status_code}: {resp.text[:200]}")
        chats = {}
        for update in resp.json().get("result", []):
            msg = update.get("message") or update.get("channel_post") or {}
            chat = msg.get("chat")
            if chat:
                name = chat.get("title") or " ".join(
                    x for x in (chat.get("first_name"), chat.get("last_name")) if x) or chat.get("username")
                chats[chat["id"]] = {"chat_id": chat["id"], "nome": name, "tipo": chat.get("type")}
        return list(chats.values())


class Notifier:
    """Esvazia a fila de mensagens em segundo plano."""

    def __init__(self, store: Store, telegram: TelegramClient):
        self.store = store
        self.telegram = telegram

    def flush(self) -> int:
        sent = 0
        for msg in self.store.pending_messages():
            if not self.telegram.enabled:
                self.store.mark_message(msg["id"], "sem_telegram")
                continue
            if now_ms() - msg["created_ms"] > MAX_AGE_MS:
                self.store.mark_message(msg["id"], "expirada")
                continue
            try:
                self.telegram.send(msg["text"])
            except Exception as exc:  # noqa: BLE001
                status = "falhou" if msg["attempts"] + 1 >= MAX_ATTEMPTS else "pending"
                error = str(exc)
                if self.telegram.token:
                    error = error.replace(self.telegram.token, "***")  # o token nunca vai para o banco/log
                self.store.mark_message(msg["id"], status, error[:500])
                log.warning("Falha ao enviar Telegram (tentativa %d): %s", msg["attempts"] + 1, error)
                break  # tenta de novo no próximo ciclo
            else:
                self.store.mark_message(msg["id"], "enviada")
                sent += 1
        return sent

    def run_forever(self, stop: threading.Event, interval: float = 5.0) -> None:
        while not stop.is_set():
            try:
                self.flush()
            except Exception:  # noqa: BLE001
                log.exception("Erro no envio de alertas")
            stop.wait(interval)
