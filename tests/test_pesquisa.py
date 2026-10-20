"""Estudo comparativo: períodos separados, sem olhar o futuro e sem reaproveitar dados de teste."""
import json

import pandas as pd
import pytest

from app import pesquisa
from app.config import TradingParams
from app.market_data import MarketData
from app.metrics import json_safe
from app.pesquisa import (CANDIDATES, EXCLUDED, PERIODS, apply_cost_filter, build_market, check_periods,
                          fetch_frames, format_markdown, period_bounds, random_benchmark, run_study,
                          select_params, trend_signals)
from app.strategy import BUY, SELL, WAIT, Signal
from tests.helpers import Clock, FakeProvider, aggregate, make_5m

H1 = 3_600_000
# Períodos curtos só para os testes (os oficiais estão em PERIODS)
TEST_PERIODS = {"desenvolvimento": ("2024-01-11", "2024-01-22"),
                "validacao": ("2024-01-23", "2024-01-30"),
                "reserva": ("2024-01-31", "2024-02-06")}


def _frames(seed=51, shock_from=None, factor=1.0):
    start = period_bounds(("2024-01-01", "2024-01-01"))[0]
    df5 = make_5m(37 * 288, seed=seed, start=start)
    if shock_from is not None:
        df5.loc[df5["open_time"] >= shock_from, ["open", "high", "low", "close"]] *= factor
    return {"5m": df5, "15m": aggregate(df5), "1h": aggregate(df5, H1)}


@pytest.fixture(scope="module")
def frames():
    return _frames()


def test_official_periods_are_ordered_and_never_touch_already_seen_data():
    check_periods(PERIODS)
    last_end = period_bounds(list(PERIODS.values())[-1])[1]
    assert last_end < period_bounds(EXCLUDED)[0]
    assert EXCLUDED == ("2026-09-18", "2026-10-20")  # janela do backtest anterior
    with pytest.raises(ValueError):
        check_periods({"a": ("2025-01-01", "2025-02-01"), "b": ("2025-01-15", "2025-03-01")})
    with pytest.raises(ValueError):
        check_periods({"a": ("2026-09-01", "2026-09-30")})  # invade o período já visto


def test_cost_filter_blocks_only_buys_whose_target_does_not_pay_costs():
    params = TradingParams()  # custo ida e volta 0,30% -> alvo mínimo 0,60%
    small = Signal(BUY, "x", [], 1, 60_000.0, 50.0, True)  # alvo = 2 x 1,5 x 50 = 150 = 0,25%
    big = Signal(BUY, "x", [], 2, 60_000.0, 200.0, True)  # alvo = 600 = 1,0%
    sell = Signal(SELL, "x", [], 3, 60_000.0, 50.0, True)
    out = apply_cost_filter([small, big, sell], params)
    assert [s.action for s in out] == [WAIT, BUY, SELL]


def test_trend_strategy_is_causal_and_buys_only_on_fresh_cross(frames):
    df = frames["1h"]
    ind, full = trend_signals(df, "1h")
    for cut in (150, 400, len(df) - 1):
        _, part = trend_signals(df.iloc[:cut], "1h")
        assert [json_safe(s.to_dict()) for s in part] == [json_safe(s.to_dict()) for s in full[:cut]]
    fast, slow = ind["ema_fast"].to_numpy(), ind["ema_slow"].to_numpy()
    buys = [i for i, s in enumerate(full) if s.action == BUY]
    assert buys
    for i in buys:
        assert fast[i] > slow[i] and fast[i - 1] <= slow[i - 1]
    assert all(s.action == WAIT and not s.data_ok for s in full[:99])  # < 100 candles: sem sinal


def test_parameter_choice_ignores_validation_and_reserve_data(frames):
    """Mudar preços depois do desenvolvimento não pode mudar a escolha dos parâmetros."""
    dev = period_bounds(TEST_PERIODS["desenvolvimento"])
    shocked = _frames(shock_from=dev[1] + 1, factor=1.25)
    for cand in CANDIDATES:
        p1, t1, _ = select_params(cand, build_market(cand, frames), TradingParams(), dev)
        p2, t2, _ = select_params(cand, build_market(cand, shocked), TradingParams(), dev)
        assert p1 == p2 and t1 == t2, cand.key


def test_random_benchmark_is_reproducible(frames):
    cand = CANDIDATES[3]
    market = build_market(cand, frames)
    val = period_bounds(TEST_PERIODS["validacao"])
    a = random_benchmark(cand, market, TradingParams(atr_stop_mult=2.0), val, 0.0, n=5, seed=1)
    b = random_benchmark(cand, market, TradingParams(atr_stop_mult=2.0), val, 0.0, n=5, seed=1)
    assert a == b
    assert a["simulacoes"] in (0, 5)


def test_study_runs_and_reserve_is_only_used_after_passing_validation(frames, monkeypatch):
    monkeypatch.setattr(pesquisa, "MIN_TRADES", 1)  # força alguma estratégia a chegar na reserva
    rep = run_study(frames, TradingParams(), TEST_PERIODS, n_random=3)
    assert [e["chave"] for e in rep["estrategias"]] == ["A", "B", "C", "D"]
    for e in rep["estrategias"]:
        passed = e["validacao"]["operacoes"] >= 1 and e["validacao"]["retorno_pct"] > 0
        assert ("reserva" in e) == passed, e["chave"]
        assert e["veredito"]
    assert set(rep["robo_atual"]) == {"desenvolvimento", "validacao"}  # reserva só para aprovadas
    assert rep["robo_atual"]["validacao"]["operacoes"] >= 0
    md = format_markdown(rep)
    assert "Validação" in md and "Conclusão automática" in md and "Robô atual" in md
    json.dumps(rep)


def test_fetch_frames_downloads_three_timeframes_until_period_end():
    data = _frames()
    clock = Clock(10**13)
    provider = FakeProvider("historico", data["5m"], data["15m"], clock)
    provider.frames["1h"] = data["1h"]
    name, frames = fetch_frames(MarketData([], providers=[provider]), TEST_PERIODS)
    assert name == "historico" and set(frames) == {"5m", "15m", "1h"}
    end = period_bounds(TEST_PERIODS["reserva"])[1]
    for df in frames.values():
        assert isinstance(df, pd.DataFrame) and df["close_time"].max() <= end
