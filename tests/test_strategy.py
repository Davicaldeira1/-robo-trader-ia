from dataclasses import replace

import pytest

from app.strategy import BUY, SELL, WAIT, Snapshot, evaluate

BULL = Snapshot(time_ms=1_000_000, price=60_000, n5=300, n15=300, gaps5=0, gaps15=0, stale_ms=0,
                ema_fast=59_950, ema_slow=59_900, rsi=60, macd_hist=5, macd_hist_prev=3, vol_ratio=1.4,
                atr=80, close15=60_000, ema20_15=59_800, ema50_15=59_500, time15_ms=1_000_000)


def test_all_conditions_true_gives_buy():
    sig = evaluate(BULL)
    assert sig.action == BUY and sig.data_ok
    assert all(c.ok for c in sig.buy_checks) and len(sig.buy_checks) == 6


@pytest.mark.parametrize("change", [
    {"ema20_15": 59_400},             # tendência 15m não é de alta
    {"close15": 59_400},              # preço abaixo da EMA50 do 15m
    {"ema_fast": 59_800},             # EMA9 < EMA21
    {"macd_hist": -1},                # MACD negativo
    {"macd_hist": 2},                 # MACD positivo mas caindo (anterior = 3)
    {"rsi": 75},                      # sobrecomprado
    {"rsi": 45},                      # sem força
    {"vol_ratio": 0.7},               # volume fraco
])
def test_any_missing_buy_condition_blocks_buy(change):
    sig = evaluate(replace(BULL, **change))
    assert sig.action != BUY
    assert sig.data_ok


def test_bearish_momentum_gives_sell():
    sig = evaluate(replace(BULL, ema_fast=59_800, macd_hist=-4, macd_hist_prev=-2))
    assert sig.action == SELL
    assert "Momento de baixa" in sig.reasons[0]


def test_bearish_15m_trend_gives_sell():
    sig = evaluate(replace(BULL, ema20_15=59_300, close15=59_200))
    assert sig.action == SELL


def test_mixed_conditions_wait():
    sig = evaluate(replace(BULL, rsi=75))
    assert sig.action == WAIT
    assert any("RSI" in r for r in sig.reasons)


@pytest.mark.parametrize("change,text", [
    ({"n5": 50}, "candles de 5m"),
    ({"n15": 10}, "candles de 15m"),
    ({"gaps5": 2}, "Faltam candles"),
    ({"stale_ms": 30 * 60_000}, "atrasados"),
    ({"rsi": float("nan")}, "Indicadores"),
    ({"time15_ms": 2_000_000}, "posterior"),
    ({"atr": 0.0}, "ATR"),
])
def test_missing_or_bad_data_never_generates_signal(change, text):
    sig = evaluate(replace(BULL, **change))
    assert sig.action == WAIT
    assert not sig.data_ok
    assert sig.buy_checks == [] and sig.sell_checks == []
    assert any(text in r for r in sig.reasons)
