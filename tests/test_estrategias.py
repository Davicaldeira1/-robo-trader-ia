"""Estratégia de tendência 1h no robô ao vivo: configuração, equivalência com o backtest e painel."""
import time

import pytest
from fastapi.testclient import TestClient

from app import pesquisa
from app.backtest import format_markdown, precompute_signals, run_full_backtest, simulate
from app.config import STRATEGIES, Settings, TradingParams, load_settings
from app.engine import Engine
from app.estrategias import TrendAnalysis, trend_signals
from app.market_data import MarketData
from app.storage import Store
from app.strategy import WAIT
from app.web import create_app
from tests.helpers import Clock, FakeProvider, aggregate, make_5m

H1 = 3_600_000


def _data(hours=700, seed=51, start=None):
    start = start if start is not None else 1_704_067_200_000  # 2024-01-01 00:00 UTC
    df5 = make_5m(hours * 12, seed=seed, start=start)
    return df5, aggregate(df5), aggregate(df5, H1)


def test_default_strategy_is_trend_1h_with_frozen_parameters():
    s = load_settings({})
    assert s.strategy == "tendencia_1h" and s.intervals == ("1h",) and s.trend_interval is None
    assert (s.trading.atr_stop_mult, s.trading.take_profit_r) == (3.0, 100.0)
    old = load_settings({"STRATEGY": "regras_5m"})
    assert old.intervals == ("5m", "15m")
    assert (old.trading.atr_stop_mult, old.trading.take_profit_r) == (1.5, 2.0)
    assert load_settings({"ATR_STOP_MULT": "2"}).trading.atr_stop_mult == 2.0  # ainda pode ser ajustado
    assert load_settings({}).trading.risk_per_trade == 0.01  # risco e trava diária não mudam
    with pytest.raises(ValueError):
        load_settings({"STRATEGY": "dinheiro_real"})
    with pytest.raises(ValueError):
        Settings(strategy="inexistente")
    assert set(STRATEGIES) == {"tendencia_1h", "regras_5m"}


def test_study_and_live_robot_share_the_same_strategy_code():
    assert pesquisa.trend_signals is trend_signals


def test_stale_data_never_generates_a_signal():
    _, _, df1h = _data()
    a = TrendAnalysis(df1h)
    last = len(a) - 1
    fresh = a.signal(last, int(a.close_time5[last]) + 60_000)
    late = a.signal(last, int(a.close_time5[last]) + 3 * H1)
    assert fresh.data_ok
    assert late.action == WAIT and not late.data_ok and "atrasados" in late.reasons[0]


def _engine(tmp_path, clock, frames, db="robo.db"):
    df5, df15, df1h = frames
    settings = Settings(data_dir=tmp_path, candles_limit=5000,  # padrão: tendência 1h
                        trading=TradingParams(atr_stop_mult=3.0, take_profit_r=100.0))
    provider = FakeProvider("teste", df5, df15, clock, df1h)
    return Engine(settings, Store(tmp_path / db), MarketData([], providers=[provider]), clock=clock)


def test_live_trend_robot_matches_backtest_trade_for_trade(tmp_path):
    frames = _data()
    df1h = frames[2]
    n = len(df1h)
    clock = Clock(0)
    eng = _engine(tmp_path, clock, frames)
    for i in range(120, n - 1):
        clock.now = int(df1h["close_time"].iloc[i]) + 10_000
        eng.tick()
    live = [(t["entry_time"], round(t["entry_price"], 6), t["exit_time"], round(t["pnl"], 6), t["exit_reason"])
            for t in sorted(eng.store.closed_trades(), key=lambda t: t["entry_time"])]

    a = TrendAnalysis(df1h.iloc[:n - 1])
    res = simulate(a, precompute_signals(a), eng.params, int(df1h["close_time"].iloc[120]),
                   int(df1h["close_time"].iloc[n - 2]))
    bt = [(t["entry_time"], round(t["entry_price"], 6), t["exit_time"], round(t["pnl"], 6), t["exit_reason"])
          for t in res.trades if t["exit_reason"] != "Fim do período de teste"]
    assert len(live) >= 3, "os dados de teste deveriam gerar operações"
    assert live == bt
    st = eng.status()
    assert st["strategy"]["key"] == "tendencia_1h" and st["strategy"]["intervals"] == ["1h"]
    assert all(t["exit_reason"] != "Alvo atingido (take-profit)" for t in eng.store.closed_trades())


def test_panel_follows_the_trend_strategy(tmp_path):
    frames = _data(300)
    df1h = frames[2]
    clock = Clock(int(df1h["close_time"].iloc[250]) + 10_000)
    eng = _engine(tmp_path, clock, frames)
    eng.tick()
    settings = Settings(data_dir=tmp_path, dashboard_password="", engine_enabled=False)
    client = TestClient(create_app(settings, eng, start_background=False))
    st = client.get("/api/status").json()
    assert st["strategy"]["name"] == "Tendência 1h (EMA20/50)"
    chart = client.get("/api/chart").json()
    assert chart["labels"] == ["EMA20", "EMA50"] and chart["candles"]
    assert (chart["candles"][1]["time"] - chart["candles"][0]["time"]) == 3600
    assert client.get("/api/chart?tf=5m").status_code == 400
    assert st["signal"]["data_ok"] and st["signal"]["buy_checks"]  # condições para o painel


def test_panel_backtest_tests_the_strategy_that_is_running():
    now = int(time.time() * 1000)
    start = (now - 700 * H1) // H1 * H1
    df5, df15, df1h = _data(700, start=start)
    market = MarketData([], providers=[FakeProvider("historico", df5, df15, Clock(now), df1h)])
    rep = run_full_backtest(load_settings({}), days=20, market=market)
    assert rep["estrategia"] == "Tendência 1h (EMA20/50)"
    assert set(rep["candles"]) == {"1h"}
    assert len(rep["dentro_da_amostra"]) == 4
    md = format_markdown(rep)
    assert "Tendência 1h" in md and "sem alvo fixo" in md
    assert "candles de 1h" in md and "candles de 5m" not in md
