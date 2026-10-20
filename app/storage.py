"""Armazenamento persistente em SQLite (um arquivo no disco do servidor).

Cada candle processado é gravado numa única transação (estado, operações,
sinais, saldo, eventos e mensagens a enviar). Se o servidor cair no meio,
nada fica pela metade e, ao voltar, o robô continua de onde parou — sem
repetir operações (as chaves únicas do banco também impedem duplicatas).
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trades (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    signal_time INTEGER NOT NULL UNIQUE,
    entry_time INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    qty REAL NOT NULL,
    stop REAL NOT NULL,
    take REAL NOT NULL,
    entry_fee REAL NOT NULL,
    risk_amount REAL NOT NULL,
    reason TEXT,
    exit_time INTEGER,
    exit_price REAL,
    exit_fee REAL,
    exit_reason TEXT,
    pnl REAL,
    pnl_pct REAL,
    r_multiple REAL
);
CREATE TABLE IF NOT EXISTS signals (
    time_ms INTEGER PRIMARY KEY,
    action TEXT NOT NULL,
    price REAL,
    summary TEXT,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS equity (
    time_ms INTEGER PRIMARY KEY,
    equity REAL NOT NULL,
    price REAL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    time_ms INTEGER NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_ms INTEGER NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS backtests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_ms INTEGER NOT NULL,
    report TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox(status);
"""

TRADE_COLUMNS = ("id", "status", "signal_time", "entry_time", "entry_price", "qty", "stop", "take",
                 "entry_fee", "risk_amount", "reason", "exit_time", "exit_price", "exit_fee",
                 "exit_reason", "pnl", "pnl_pct", "r_multiple")


def now_ms() -> int:
    return int(time.time() * 1000)


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._lock, self.read() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @contextmanager
    def transaction(self):
        """Tudo dentro do bloco é gravado junto, ou nada é gravado."""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    @contextmanager
    def read(self):
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    # ---------------------------------------------------------- estado
    @staticmethod
    def set_kv(conn, key: str, value) -> None:
        conn.execute("INSERT INTO kv(key, value) VALUES(?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))

    def get_kv(self, key: str, default=None):
        with self.read() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    # -------------------------------------------------------- operações
    @staticmethod
    def save_trade(conn, trade: dict, status: str) -> None:
        row = {c: trade.get(c) for c in TRADE_COLUMNS}
        row["id"] = trade["trade_id"]
        row["status"] = status
        cols = ", ".join(TRADE_COLUMNS)
        marks = ", ".join(f":{c}" for c in TRADE_COLUMNS)
        updates = ", ".join(f"{c}=excluded.{c}" for c in TRADE_COLUMNS if c != "id")
        conn.execute(f"INSERT INTO trades({cols}) VALUES({marks}) "
                     f"ON CONFLICT(id) DO UPDATE SET {updates}", row)

    def trades(self, limit: int = 200) -> list[dict]:
        with self.read() as conn:
            rows = conn.execute("SELECT * FROM trades ORDER BY entry_time DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def closed_trades(self) -> list[dict]:
        with self.read() as conn:
            rows = conn.execute("SELECT * FROM trades WHERE status='closed' ORDER BY exit_time").fetchall()
        return [dict(r) for r in rows]

    # ----------------------------------------------------------- sinais
    @staticmethod
    def save_signal(conn, signal: dict) -> bool:
        cur = conn.execute("INSERT OR IGNORE INTO signals(time_ms, action, price, summary, data) "
                           "VALUES(?, ?, ?, ?, ?)",
                           (signal["time_ms"], signal["action"], signal["price"], signal["summary"],
                            json.dumps(signal)))
        return cur.rowcount == 1

    def signals(self, limit: int = 50) -> list[dict]:
        with self.read() as conn:
            rows = conn.execute("SELECT data FROM signals ORDER BY time_ms DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(r["data"]) for r in rows]

    # ------------------------------------------------------------ saldo
    @staticmethod
    def save_equity(conn, time_ms: int, equity: float, price: float) -> None:
        conn.execute("INSERT OR REPLACE INTO equity(time_ms, equity, price) VALUES(?, ?, ?)",
                     (time_ms, equity, price))

    def equity_series(self, since_ms: int = 0) -> list[dict]:
        with self.read() as conn:
            rows = conn.execute("SELECT time_ms, equity, price FROM equity WHERE time_ms>=? ORDER BY time_ms",
                                (since_ms,)).fetchall()
        return [dict(r) for r in rows]

    # ----------------------------------------------------------- eventos
    @staticmethod
    def add_event(conn, level: str, message: str, time_ms: int | None = None) -> None:
        conn.execute("INSERT INTO events(time_ms, level, message) VALUES(?, ?, ?)",
                     (time_ms or now_ms(), level, message[:2000]))

    def log_event(self, level: str, message: str) -> None:
        with self.transaction() as conn:
            self.add_event(conn, level, message)

    def events(self, limit: int = 100) -> list[dict]:
        with self.read() as conn:
            rows = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def error_count(self, since_ms: int) -> int:
        with self.read() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM events WHERE level='erro' AND time_ms>=?",
                               (since_ms,)).fetchone()
        return int(row["n"])

    # -------------------------------------------- mensagens do Telegram
    @staticmethod
    def enqueue(conn, text: str) -> None:
        conn.execute("INSERT INTO outbox(created_ms, text) VALUES(?, ?)", (now_ms(), text))

    def pending_messages(self, limit: int = 20) -> list[dict]:
        with self.read() as conn:
            rows = conn.execute("SELECT * FROM outbox WHERE status='pending' ORDER BY id LIMIT ?",
                                (limit,)).fetchall()
        return [dict(r) for r in rows]

    def mark_message(self, msg_id: int, status: str, error: str | None = None) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE outbox SET status=?, attempts=attempts+1, last_error=? WHERE id=?",
                         (status, error, msg_id))

    def messages(self, limit: int = 50) -> list[dict]:
        with self.read() as conn:
            rows = conn.execute("SELECT * FROM outbox ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------- backtest
    def save_backtest(self, report: dict) -> None:
        with self.transaction() as conn:
            conn.execute("INSERT INTO backtests(created_ms, report) VALUES(?, ?)",
                         (now_ms(), json.dumps(report)))

    def latest_backtest(self) -> dict | None:
        with self.read() as conn:
            row = conn.execute("SELECT report FROM backtests ORDER BY id DESC LIMIT 1").fetchone()
        return json.loads(row["report"]) if row else None
