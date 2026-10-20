"""Estratégias disponíveis para o robô (sempre em modo simulado).

- tendencia_1h (padrão): segue a tendência no gráfico de 1 hora.
    COMPRA  quando a EMA20 cruza para cima da EMA50 (cruzamento novo).
    VENDA   enquanto a EMA20 estiver abaixo da EMA50 (encerra a posição).
    Stop de 3x ATR(14) e sem alvo fixo (parâmetros congelados no estudo
    comparativo, período de desenvolvimento 2025-04 a 2025-12).
- regras_5m: as regras originais (entrada 5m + tendência 15m), ver strategy.py.

O robô ao vivo, o backtest do painel e o estudo comparativo usam estas
mesmas funções, então o que roda é exatamente o que foi testado.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .analysis import Analyzer
from .config import Settings
from .indicators import add_indicators
from .market_data import interval_ms
from .strategy import BUY, SELL, WAIT, Check, Signal

MIN_CANDLES_TREND = 100


def _fmt(x: float) -> str:
    return "—" if np.isnan(x) else f"{x:,.2f}"


def trend_signals(df: pd.DataFrame, interval: str) -> tuple[pd.DataFrame, list[Signal]]:
    """Cruzamento de EMA20/EMA50 no próprio tempo gráfico, candle a candle e sem olhar o futuro."""
    ind = add_indicators(df.reset_index(drop=True), 20, 50)
    fast, slow = ind["ema_fast"].to_numpy(), ind["ema_slow"].to_numpy()
    close, atr_ = ind["close"].to_numpy(), ind["atr"].to_numpy()
    times = ind["close_time"].to_numpy(dtype=np.int64)
    missing = np.zeros(len(ind), dtype=np.int64)
    missing[1:] = np.diff(ind["open_time"].to_numpy(dtype=np.int64)) // interval_ms(interval) - 1
    cum = np.cumsum(missing)
    signals = []
    for i in range(len(ind)):
        t, price, a = int(times[i]), float(close[i]), float(atr_[i])
        prev_fast = fast[i - 1] if i else np.nan
        prev_slow = slow[i - 1] if i else np.nan
        problems = []
        if i + 1 < MIN_CANDLES_TREND:
            problems.append(f"Só há {i + 1} candles de {interval} (mínimo {MIN_CANDLES_TREND}).")
        if cum[i] - cum[max(0, i - MIN_CANDLES_TREND + 1)] > 0:
            problems.append("Faltam candles na série recente.")
        if any(np.isnan(v) for v in (fast[i], slow[i], a, prev_fast, prev_slow)) or not a > 0:
            problems.append("Indicadores ainda não podem ser calculados com os dados disponíveis.")
        if problems:
            signals.append(Signal(WAIT, "Dados insuficientes — nenhum sinal gerado.", problems, t, price, a, False))
            continue
        above, was_below = fast[i] > slow[i], prev_fast <= prev_slow
        buy_checks = [Check(f"EMA20 acima da EMA50 ({interval})", bool(above), f"{_fmt(fast[i])} vs {_fmt(slow[i])}"),
                      Check("Cruzamento novo (no candle anterior a EMA20 estava abaixo)", bool(was_below),
                            f"{_fmt(prev_fast)} vs {_fmt(prev_slow)}")]
        sell_checks = [Check(f"EMA20 abaixo da EMA50 ({interval})", bool(fast[i] < slow[i]),
                             f"{_fmt(fast[i])} vs {_fmt(slow[i])}")]
        if above and was_below:
            signals.append(Signal(BUY, f"EMA20 cruzou acima da EMA50 ({interval}): início de tendência de alta.",
                                  [c.label for c in buy_checks], t, price, a, True, buy_checks, sell_checks))
        elif fast[i] < slow[i]:
            signals.append(Signal(SELL, f"EMA20 abaixo da EMA50 ({interval}): tendência de baixa.",
                                  [sell_checks[0].label], t, price, a, True, buy_checks, sell_checks))
        else:
            why = ("Tendência de alta já em andamento: o robô só compra no cruzamento."
                   if above else "Médias empatadas: sem tendência definida.")
            signals.append(Signal(WAIT, why, [why], t, price, a, True, buy_checks, sell_checks))
    return ind, signals


class TrendAnalysis:
    """Mesma interface do Analyzer (ind5, close_time5, signal), para o robô e o backtest."""

    def __init__(self, df: pd.DataFrame, interval: str = "1h"):
        self.interval = interval
        self.ind5, self._signals = trend_signals(df, interval)
        self.close_time5 = self.ind5["close_time"].to_numpy(dtype=np.int64)
        self.charts = {interval: (self.ind5, ["EMA20", "EMA50"])}

    def __len__(self) -> int:
        return len(self.ind5)

    def signal(self, i: int, now_ms: int | None = None) -> Signal:
        s = self._signals[i]
        if now_ms is not None and s.data_ok and now_ms - s.time_ms > 2 * interval_ms(self.interval):
            late = (now_ms - s.time_ms) // 60000
            return Signal(WAIT, "Dados insuficientes — nenhum sinal gerado.", [f"Dados atrasados em {late} min."],
                          s.time_ms, s.price, s.atr, False)
        return s


class RulesAnalysis(Analyzer):
    """Regras originais (entrada + tendência) com os gráficos para o painel."""

    def __init__(self, df_entry: pd.DataFrame, df_trend: pd.DataFrame, entry: str, trend: str):
        super().__init__(df_entry, df_trend, entry, trend)
        self.charts = {entry: (self.ind5, ["EMA9", "EMA21"]), trend: (self.ind15, ["EMA20", "EMA50"])}


def build_analysis(settings: Settings, frames: dict[str, pd.DataFrame]):
    """Monta a análise da estratégia configurada a partir de candles FECHADOS."""
    if settings.trend_interval is None:
        return TrendAnalysis(frames[settings.entry_interval], settings.entry_interval)
    return RulesAnalysis(frames[settings.entry_interval], frames[settings.trend_interval],
                         settings.entry_interval, settings.trend_interval)
