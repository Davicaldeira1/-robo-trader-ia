import pytest

from app.config import TradingParams
from app.strategy import BUY, SELL, Signal
from app.trader import Candle, PaperTrader, TraderState

M5 = 300_000
BASE = 1_704_888_000_000  # 2024-01-10 12:00 UTC (09:00 em São Paulo)


def C(t, o, h, l, c):  # noqa: E741
    return Candle(t, o, h, l, c, 10.0, t + M5 - 1)


def buy(c: Candle, atr: float) -> Signal:
    return Signal(BUY, "teste de compra", [], c.close_time, c.close, atr, True)


def opened_trader(atr=2000.0, params=None, t=BASE, price=60_000.0):
    tr = PaperTrader(params or TradingParams(), TraderState.new(1000.0))
    c0 = C(t, price, price + 10, price - 10, price)
    tr.process_candle(c0, lambda: buy(c0, atr))
    c1 = C(t + M5, price, price + 50, price, price + 20)
    events = tr.process_candle(c1)
    opened = [e for e in events if e["type"] == "opened"]
    return tr, (opened[0]["trade"] if opened else None), t + 2 * M5


def test_entry_at_next_open_with_slippage_and_1pct_risk():
    tr, trade, _ = opened_trader(atr=2000)
    fill = 60_000 * 1.0005
    assert trade["entry_price"] == pytest.approx(fill)
    assert trade["entry_time"] == BASE + M5  # abertura do candle seguinte ao sinal
    assert trade["stop"] == pytest.approx(fill - 1.5 * 2000)
    assert trade["take"] == pytest.approx(fill + 2.0 * 1.5 * 2000)
    assert trade["risk_amount"] == pytest.approx(10.0)  # 1% de US$ 1.000
    assert trade["entry_fee"] == pytest.approx(trade["qty"] * fill * 0.001)


def test_stop_loss_loses_exactly_the_planned_risk_including_costs():
    tr, trade, t = opened_trader(atr=2000)
    ev = tr.process_candle(C(t, 60_000, 60_100, trade["stop"] - 100, 59_000))
    closed = [e for e in ev if e["type"] == "closed"][0]["trade"]
    assert closed["exit_reason"] == "Stop-loss"
    assert closed["pnl"] == pytest.approx(-10.0, abs=1e-6)
    assert tr.s.cash == pytest.approx(1000.0 - 10.0, abs=1e-6)
    assert tr.s.fees_paid == pytest.approx(closed["entry_fee"] + closed["exit_fee"])


def test_take_profit_gives_about_2r_after_costs():
    tr, trade, t = opened_trader(atr=2000)
    ev = tr.process_candle(C(t, 60_000, trade["take"] + 50, 59_900, trade["take"]))
    closed = [e for e in ev if e["type"] == "closed"][0]["trade"]
    assert closed["exit_reason"].startswith("Alvo")
    assert closed["pnl"] > 0
    assert 1.5 < closed["r_multiple"] < 2.0  # 2R bruto, menos taxas e slippage


def test_stop_and_target_in_same_candle_assumes_worst_case():
    tr, trade, t = opened_trader(atr=2000)
    ev = tr.process_candle(C(t, 60_000, trade["take"] + 10, trade["stop"] - 10, 60_000))
    closed = [e for e in ev if e["type"] == "closed"][0]["trade"]
    assert closed["exit_reason"] == "Stop-loss"


def test_gap_below_stop_fills_at_open_not_at_stop():
    tr, trade, t = opened_trader(atr=2000)
    gap_open = trade["stop"] - 1000
    ev = tr.process_candle(C(t, gap_open, gap_open + 10, gap_open - 10, gap_open))
    closed = [e for e in ev if e["type"] == "closed"][0]["trade"]
    assert closed["exit_price"] == pytest.approx(gap_open * (1 - 0.0005))
    assert closed["pnl"] < -10.0  # perde mais que o planejado, como na vida real


def test_position_never_exceeds_available_cash():
    tr, trade, _ = opened_trader(atr=20)  # stop muito curto pediria posição enorme
    assert trade["qty"] * trade["entry_price"] * 1.001 <= 1000.0 + 1e-9
    assert trade["risk_amount"] < 10.0
    assert tr.s.cash >= -1e-9


def test_sell_signal_closes_position_at_next_open():
    tr, trade, t = opened_trader(atr=2000)
    c = C(t, 60_100, 60_200, 60_000, 60_150)
    tr.process_candle(c, lambda: Signal(SELL, "venda", [], c.close_time, c.close, 50, True))
    ev = tr.process_candle(C(t + M5, 60_150, 60_300, 60_100, 60_200))
    closed = [e for e in ev if e["type"] == "closed"][0]["trade"]
    assert closed["exit_reason"] == "Sinal de VENDA"
    assert closed["exit_price"] == pytest.approx(60_150 * (1 - 0.0005))


def test_three_full_losses_stop_trading_for_the_day_and_resume_next_day():
    tr = PaperTrader(TradingParams(), TraderState.new(1000.0))
    t = BASE
    for _ in range(3):
        c0 = C(t, 60_000, 60_010, 59_990, 60_000)
        tr.process_candle(c0, lambda c0=c0: buy(c0, 2000))
        tr.process_candle(C(t + M5, 60_000, 60_010, 59_990, 60_000))
        ev = tr.process_candle(C(t + 2 * M5, 60_000, 60_010, 50_000, 55_000))
        assert any(e["type"] == "closed" for e in ev)
        t += 3 * M5
    assert tr.s.day_start_equity - tr.equity() == pytest.approx(29.70, abs=0.01)
    # 4ª compra no mesmo dia: não há espaço para arriscar sem passar de 3% → robô para no dia
    c0 = C(t, 60_000, 60_010, 59_990, 60_000)
    tr.process_candle(c0, lambda: buy(c0, 2000))
    ev = tr.process_candle(C(t + M5, 60_000, 60_010, 59_990, 60_000))
    assert any(e["type"] == "halted" for e in ev)
    assert tr.s.position is None and tr.halted
    # novos sinais de compra no mesmo dia são ignorados
    c2 = C(t + 2 * M5, 60_000, 60_010, 59_990, 60_000)
    tr.process_candle(c2, lambda: buy(c2, 2000))
    assert tr.s.pending is None
    # dia seguinte (horário de São Paulo): liberado de novo
    next_day = BASE + 24 * 3600 * 1000
    ev = tr.process_candle(C(next_day, 60_000, 60_010, 59_990, 60_000))
    assert any(e["type"] == "resumed" for e in ev)
    assert not tr.halted


def test_gap_loss_above_daily_limit_halts_the_day():
    # stop curto + posição limitada pelo saldo (~US$ 1.000); o preço abre 5% abaixo (gap)
    tr, trade, t = opened_trader(atr=40)
    assert trade is not None and tr.s.position is not None
    gap_open = 57_000.0
    ev = tr.process_candle(C(t, gap_open, gap_open + 10, gap_open - 10, gap_open))
    kinds = [e["type"] for e in ev]
    assert kinds == ["closed", "halted"]
    assert tr.s.day_start_equity - tr.equity() > 30.0
    assert tr.halted
    c = C(t + M5, 57_000, 57_010, 56_990, 57_000)
    tr.process_candle(c, lambda: buy(c, 2000))
    assert tr.s.pending is None  # nenhuma compra nova no mesmo dia


def test_pending_order_expires_if_candles_were_skipped():
    tr = PaperTrader(TradingParams(), TraderState.new(1000.0))
    c0 = C(BASE, 60_000, 60_010, 59_990, 60_000)
    tr.process_candle(c0, lambda: buy(c0, 2000))
    ev = tr.process_candle(C(BASE + 3 * M5, 60_000, 60_010, 59_990, 60_000))
    assert any(e["type"] == "cancelled" for e in ev)
    assert tr.s.position is None


def test_paused_blocks_new_buys_but_stops_still_work():
    tr, trade, t = opened_trader(atr=2000)
    tr.s.paused = True
    ev = tr.process_candle(C(t, 60_000, 60_010, trade["stop"] - 1, 59_000))
    assert any(e["type"] == "closed" for e in ev)
    c = C(t + M5, 60_000, 60_010, 59_990, 60_000)
    tr.process_candle(c, lambda: buy(c, 2000))
    assert tr.s.pending is None


def test_state_roundtrip_keeps_position_and_pending():
    tr, trade, t = opened_trader(atr=2000)
    c = C(t, 60_000, 60_010, 59_990, 60_000)
    tr.process_candle(c, lambda: Signal(SELL, "venda", [], c.close_time, c.close, 50, True))
    restored = TraderState.from_dict(tr.s.to_dict())
    assert restored == tr.s
    assert restored.position.trade_id == trade["trade_id"]
    assert restored.pending.side == "SELL"
