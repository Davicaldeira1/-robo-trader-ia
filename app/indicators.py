"""Indicadores técnicos: EMA, RSI, MACD, ATR e volume relativo.

Todos são causais: o valor na posição i usa apenas dados até i (nunca
dados futuros). Enquanto não há dados suficientes o valor é NaN, e a
estratégia trata NaN como "dados insuficientes".
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _seeded_ewm(v: np.ndarray, seed_idx: int, seed: float, alpha: float) -> np.ndarray:
    """Recursão y[i] = alpha*v[i] + (1-alpha)*y[i-1], começando em y[seed_idx] = seed."""
    out = np.full(len(v), np.nan)
    tail = v[seed_idx:].copy()
    tail[0] = seed
    out[seed_idx:] = pd.Series(tail).ewm(alpha=alpha, adjust=False).mean().to_numpy()
    return out


def ema(values, period: int) -> np.ndarray:
    """Média móvel exponencial, iniciada pela média simples dos primeiros `period` valores."""
    v = np.asarray(values, dtype=float)
    valid = np.flatnonzero(~np.isnan(v))
    if len(valid) == 0 or len(v) - valid[0] < period:
        return np.full(len(v), np.nan)
    start = valid[0]
    seed_end = start + period
    return _seeded_ewm(v, seed_end - 1, v[start:seed_end].mean(), 2.0 / (period + 1))


def rsi(close, period: int = 14) -> np.ndarray:
    """RSI de Wilder (0 a 100)."""
    c = np.asarray(close, dtype=float)
    out = np.full(len(c), np.nan)
    if len(c) <= period:
        return out
    delta = np.concatenate(([np.nan], np.diff(c)))
    gains = np.clip(delta, 0, None)
    losses = np.clip(-delta, 0, None)
    alpha = 1.0 / period
    avg_gain = _seeded_ewm(gains, period, gains[1:period + 1].mean(), alpha)
    avg_loss = _seeded_ewm(losses, period, losses[1:period + 1].mean(), alpha)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs_val = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    out[period:] = np.where(avg_loss[period:] == 0,
                            np.where(avg_gain[period:] == 0, 50.0, 100.0), rs_val[period:])
    return out


def macd(close, fast: int = 12, slow: int = 26, signal: int = 9):
    """Retorna (linha MACD, linha de sinal, histograma)."""
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line, signal)
    return line, sig, line - sig


def atr(high, low, close, period: int = 14) -> np.ndarray:
    """Average True Range de Wilder (volatilidade média por candle)."""
    h, l, c = (np.asarray(x, dtype=float) for x in (high, low, close))
    n = len(c)
    if n <= period:
        return np.full(n, np.nan)
    tr = np.empty(n)
    tr[0] = h[0] - l[0]
    prev = c[:-1]
    tr[1:] = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - prev), np.abs(l[1:] - prev)))
    return _seeded_ewm(tr, period, tr[1:period + 1].mean(), 1.0 / period)


def volume_ratio(volume, period: int = 20) -> np.ndarray:
    """Volume do candle dividido pela média dos `period` candles ANTERIORES."""
    v = np.asarray(volume, dtype=float)
    out = np.full(len(v), np.nan)
    if len(v) <= period:
        return out
    csum = np.concatenate(([0.0], np.cumsum(v)))
    prev_mean = (csum[period:-1] - csum[:-period - 1]) / period
    with np.errstate(divide="ignore", invalid="ignore"):
        out[period:] = np.where(prev_mean > 0, v[period:] / prev_mean, np.nan)
    return out


def add_indicators(df: pd.DataFrame, ema_fast: int, ema_slow: int) -> pd.DataFrame:
    """Copia o DataFrame de candles e adiciona as colunas de indicadores."""
    base = df.reset_index(drop=True)
    close = base["close"].to_numpy(dtype=float)
    line, sig, hist = macd(close)
    cols = {
        "ema_fast": ema(close, ema_fast),
        "ema_slow": ema(close, ema_slow),
        "rsi": rsi(close, 14),
        "macd": line,
        "macd_signal": sig,
        "macd_hist": hist,
        "atr": atr(base["high"], base["low"], close, 14),
        "vol_ratio": volume_ratio(base["volume"], 20),
    }
    return pd.concat([base, pd.DataFrame(cols)], axis=1)
