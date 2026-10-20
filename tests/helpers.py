"""Dados sintéticos e fontes falsas, usados SOMENTE nos testes automatizados."""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.market_data import COLUMNS, DataError, Provider

M5 = 300_000
M15 = 900_000
START = 1_700_000_100_000 - (1_700_000_100_000 % M15)  # alinhado a 15 min


def make_5m(n: int = 900, seed: int = 7, price: float = 60_000.0, start: int = START) -> pd.DataFrame:
    """Passeio aleatório com fases de alta e de baixa (para gerar sinais dos dois lados)."""
    rng = np.random.default_rng(seed)
    phase = np.sin(np.arange(n) / 70.0)
    rets = rng.normal(0.0009 * phase, 0.0012, n)
    close = price * np.exp(np.cumsum(rets))
    open_ = np.concatenate([[price], close[:-1]])
    wick = np.abs(rng.normal(0, 0.0008, n))
    high = np.maximum(open_, close) * (1 + wick)
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.0008, n)))
    volume = rng.lognormal(3.0, 0.4, n) * (1 + 2 * np.abs(rets) / 0.0012)
    open_time = start + np.arange(n, dtype=np.int64) * M5
    return pd.DataFrame({"open_time": open_time, "open": open_, "high": high, "low": low, "close": close,
                         "volume": volume, "close_time": open_time + M5 - 1})[COLUMNS]


def aggregate(df5: pd.DataFrame, step: int = M15) -> pd.DataFrame:
    """Monta candles de 15m a partir dos de 5m (como uma corretora faria)."""
    key = df5["open_time"] // step * step
    g = df5.groupby(key)
    out = pd.DataFrame({
        "open_time": g["open_time"].first().index.astype("int64"),
        "open": g["open"].first().to_numpy(),
        "high": g["high"].max().to_numpy(),
        "low": g["low"].min().to_numpy(),
        "close": g["close"].last().to_numpy(),
        "volume": g["volume"].sum().to_numpy(),
    })
    out["close_time"] = out["open_time"] + step - 1
    return out[COLUMNS].reset_index(drop=True)


class Clock:
    def __init__(self, now: int):
        self.now = now

    def __call__(self) -> int:
        return self.now


class FakeProvider(Provider):
    """Devolve os candles "conhecidos" até o horário do relógio (inclui o candle em andamento)."""

    def __init__(self, name: str, df5: pd.DataFrame, df15: pd.DataFrame, clock: Clock,
                 df1h: pd.DataFrame | None = None):
        self.name = name
        self.frames = {"5m": df5, "15m": df15, "1h": df1h if df1h is not None else aggregate(df5, 3_600_000)}
        self.clock = clock
        self.fail = False
        self.calls = 0

    def fetch_recent(self, symbol, interval, limit):
        self.calls += 1
        if self.fail:
            raise DataError(f"{self.name}: simulando queda da API")
        df = self.frames[interval]
        return df[df["open_time"] <= self.clock.now].tail(limit).reset_index(drop=True)

    def fetch_range(self, symbol, interval, start_ms, end_ms):
        df = self.frames[interval]
        return df[(df["open_time"] >= start_ms) & (df["open_time"] <= end_ms)].reset_index(drop=True)


class FakeTelegram:
    def __init__(self, fail_times: int = 0, enabled: bool = True):
        self.sent: list[str] = []
        self.fail_times = fail_times
        self._enabled = enabled
        self.token = "TESTTOKEN"

    @property
    def enabled(self):
        return self._enabled

    def send(self, text: str) -> None:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("Telegram fora do ar (simulado)")
        self.sent.append(text)

    def discover_chats(self):
        return [{"chat_id": 123, "nome": "Teste", "tipo": "private"}]
