"""Monta o que a estratégia enxerga em cada candle de 5m (sem olhar o futuro).

O robô ao vivo e o backtest usam exatamente esta mesma função, então o
backtest testa o mesmo código que roda de verdade.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .indicators import add_indicators
from .market_data import interval_ms
from .strategy import MIN_CANDLES_15M, MIN_CANDLES_5M, Signal, Snapshot, evaluate


def _missing_cumsum(open_time: np.ndarray, step: int) -> np.ndarray:
    """cum[i] = total de candles faltando entre o candle 0 e o candle i."""
    missing = np.zeros(len(open_time), dtype=np.int64)
    if len(open_time) > 1:
        missing[1:] = np.diff(open_time) // step - 1
    return np.cumsum(missing)


class Analyzer:
    def __init__(self, df5: pd.DataFrame, df15: pd.DataFrame,
                 entry_interval: str = "5m", trend_interval: str = "15m"):
        self.ind5 = add_indicators(df5.reset_index(drop=True), 9, 21)
        self.ind15 = add_indicators(df15.reset_index(drop=True), 20, 50)
        a, b = self.ind5, self.ind15
        self.close_time5 = a["close_time"].to_numpy(dtype=np.int64)
        self.close_time15 = b["close_time"].to_numpy(dtype=np.int64)
        self._cols5 = {c: a[c].to_numpy(dtype=float) for c in
                       ("close", "ema_fast", "ema_slow", "rsi", "macd_hist", "vol_ratio", "atr")}
        self._cols15 = {c: b[c].to_numpy(dtype=float) for c in ("close", "ema_fast", "ema_slow")}
        self._gaps5 = _missing_cumsum(a["open_time"].to_numpy(dtype=np.int64), interval_ms(entry_interval))
        self._gaps15 = _missing_cumsum(b["open_time"].to_numpy(dtype=np.int64), interval_ms(trend_interval))

    def __len__(self) -> int:
        return len(self.ind5)

    def trend_index(self, time_ms: int) -> int:
        """Índice do último candle de 15m FECHADO até `time_ms` (-1 se nenhum)."""
        return int(np.searchsorted(self.close_time15, time_ms, side="right")) - 1

    @staticmethod
    def _window_gaps(cum: np.ndarray, end: int, size: int) -> int:
        if end < 0:
            return 0
        start = max(0, end - size + 1)
        return int(cum[end] - cum[start])

    def snapshot(self, i: int, now_ms: int | None = None) -> Snapshot:
        c5, c15 = self._cols5, self._cols15
        t = int(self.close_time5[i])
        j = self.trend_index(t)
        nan = float("nan")
        return Snapshot(
            time_ms=t,
            price=float(c5["close"][i]),
            n5=i + 1,
            n15=j + 1,
            gaps5=self._window_gaps(self._gaps5, i, MIN_CANDLES_5M),
            gaps15=self._window_gaps(self._gaps15, j, MIN_CANDLES_15M),
            stale_ms=max(0, (now_ms - t)) if now_ms is not None else 0,
            ema_fast=float(c5["ema_fast"][i]),
            ema_slow=float(c5["ema_slow"][i]),
            rsi=float(c5["rsi"][i]),
            macd_hist=float(c5["macd_hist"][i]),
            macd_hist_prev=float(c5["macd_hist"][i - 1]) if i >= 1 else nan,
            vol_ratio=float(c5["vol_ratio"][i]),
            atr=float(c5["atr"][i]),
            close15=float(c15["close"][j]) if j >= 0 else nan,
            ema20_15=float(c15["ema_fast"][j]) if j >= 0 else nan,
            ema50_15=float(c15["ema_slow"][j]) if j >= 0 else nan,
            time15_ms=int(self.close_time15[j]) if j >= 0 else 0,
        )

    def signal(self, i: int, now_ms: int | None = None) -> Signal:
        return evaluate(self.snapshot(i, now_ms))
