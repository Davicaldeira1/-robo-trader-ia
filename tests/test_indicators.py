import math

import numpy as np
import pytest

from app.indicators import add_indicators, atr, ema, macd, rsi, volume_ratio
from tests.helpers import make_5m

# Exemplo clássico do RSI de Wilder (StockCharts), valores de referência publicados.
WILDER_CLOSES = [44.3389, 44.0902, 44.1497, 43.6124, 44.3278, 44.8264, 45.0955, 45.4245, 45.8433,
                 46.0826, 45.8931, 46.0328, 45.6140, 46.2820, 46.2820, 46.0028, 46.0328, 46.4116,
                 46.2222, 45.6439, 46.2122, 46.2521, 45.7137, 46.4515, 45.7835, 45.3548, 44.0288,
                 44.1783, 44.2181, 44.5672, 43.4205, 42.6628, 43.1314]
WILDER_RSI = {14: 70.53, 15: 66.32, 16: 66.55, 17: 69.41, 18: 66.36, 19: 57.97, 20: 62.93,
              21: 63.26, 22: 56.06, 23: 62.38, 24: 54.71, 25: 50.42, 26: 39.99, 27: 41.46,
              28: 41.87, 29: 45.46, 30: 37.30, 31: 33.08, 32: 37.77}


def test_ema_starts_with_sma_and_follows_formula():
    out = ema(list(range(1, 11)), 3)
    assert np.isnan(out[:2]).all()
    assert out[2] == pytest.approx(2.0)  # média de 1, 2, 3
    assert out[3] == pytest.approx(4 * 0.5 + 2.0 * 0.5)
    assert out[9] == pytest.approx(9.0)  # série linear: EMA fica 1 abaixo do preço


def test_ema_without_enough_data_is_nan():
    assert np.isnan(ema([1.0, 2.0], 5)).all()


def test_rsi_matches_wilder_reference():
    out = rsi(WILDER_CLOSES, 14)
    assert np.isnan(out[:14]).all()
    for i, expected in WILDER_RSI.items():
        assert out[i] == pytest.approx(expected, abs=0.05), i


def test_rsi_extremes():
    assert rsi(np.arange(1, 40, dtype=float), 14)[-1] == pytest.approx(100.0)
    assert rsi(np.arange(40, 1, -1, dtype=float), 14)[-1] == pytest.approx(0.0)
    assert rsi(np.full(30, 5.0), 14)[-1] == pytest.approx(50.0)


def test_macd_is_difference_of_emas_and_signal_is_its_ema():
    close = make_5m(200)["close"].to_numpy()
    line, sig, hist = macd(close)
    np.testing.assert_allclose(line, ema(close, 12) - ema(close, 26), equal_nan=True)
    first_valid = np.flatnonzero(~np.isnan(line))[0]
    assert first_valid == 25
    assert np.isnan(sig[: first_valid + 8]).all() and not math.isnan(sig[first_valid + 8])
    np.testing.assert_allclose(hist, line - sig, equal_nan=True)


def test_atr_constant_range():
    n = 40
    close = np.full(n, 100.0)
    out = atr(close + 2, close - 2, close, 14)
    assert np.isnan(out[:14]).all()
    assert out[14:] == pytest.approx(4.0)


def test_volume_ratio_uses_only_previous_candles():
    vol = np.array([10.0] * 20 + [30.0, 10.0])
    out = volume_ratio(vol, 20)
    assert np.isnan(out[:20]).all()
    assert out[20] == pytest.approx(3.0)  # 30 / média dos 20 anteriores (10)
    assert out[21] == pytest.approx(10.0 / 11.0)


def test_indicators_are_causal():
    """Acrescentar candles no futuro não pode mudar valores do passado."""
    df = make_5m(400)
    full = add_indicators(df, 9, 21)
    cut = add_indicators(df.iloc[:250], 9, 21)
    cols = ["ema_fast", "ema_slow", "rsi", "macd", "macd_signal", "macd_hist", "atr", "vol_ratio"]
    np.testing.assert_allclose(full[cols].iloc[:250].to_numpy(), cut[cols].to_numpy(), equal_nan=True)
