"""Backtest: sem vazamento de informação futura, com custos, e validação fora da amostra."""
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from app.analysis import Analyzer
from app.backtest import (GRID, format_markdown, main, precompute_signals, run_full_backtest, simulate,
                          walk_forward)
from app.config import Settings, TradingParams
from app.market_data import MarketData
from app.metrics import json_safe
from tests.helpers import M5, M15, Clock, FakeProvider, aggregate, make_5m


def _data(n=1000, seed=51, **kw):
    df5 = make_5m(n, seed=seed, **kw)
    return df5, aggregate(df5)


def test_signals_never_use_future_data():
    """Calcular com o histórico inteiro ou só até o candle dá o MESMO sinal."""
    df5, df15 = _data()
    full = [json_safe(s.to_dict()) for s in precompute_signals(Analyzer(df5, df15))]  # NaN -> None
    for cut in (250, 451, 777, 999):
        t = df5["close_time"].iloc[cut - 1]
        part = precompute_signals(Analyzer(df5.iloc[:cut], df15[df15["close_time"] <= t]))
        assert [json_safe(s.to_dict()) for s in part] == full[:cut], cut


def test_15m_trend_uses_only_closed_candles():
    df5, df15 = _data()
    a = Analyzer(df5, df15)
    closes15 = df15["close_time"].to_numpy()
    for i in range(len(df5)):
        snap = a.snapshot(i)
        assert snap.time15_ms <= snap.time_ms
        expected = closes15[closes15 <= snap.time_ms]
        assert snap.time15_ms == (expected[-1] if len(expected) else 0)
    # Exemplo: às 10:05 o candle de 15m das 10:00 ainda está aberto → usa o das 09:45.
    snap = a.snapshot(1)
    assert snap.time15_ms == 0 and snap.n15 == 0
    i = 4  # 5º candle de 5m fecha 1 candle de 15m e mais 2 de 5m do próximo
    assert a.snapshot(i).time15_ms == df15["close_time"].iloc[0]


def test_changing_the_future_does_not_change_past_trades():
    df5, df15 = _data()
    a = Analyzer(df5, df15)
    res = simulate(a, precompute_signals(a), TradingParams(), int(df5["close_time"].iloc[0]),
                   int(df5["close_time"].iloc[-1]))
    cut = 800
    shocked = df5.copy()
    shocked.loc[cut:, ["open", "high", "low", "close"]] *= 0.5  # crash só no "futuro"
    shocked15 = aggregate(shocked)
    b = Analyzer(shocked, shocked15)
    res2 = simulate(b, precompute_signals(b), TradingParams(), int(df5["close_time"].iloc[0]),
                    int(df5["close_time"].iloc[-1]))
    t_cut = int(df5["open_time"].iloc[cut])
    before = [t for t in res.trades if t["exit_time"] < t_cut]
    before2 = [t for t in res2.trades if t["exit_time"] < t_cut]
    assert before and before == before2


def test_entries_happen_at_next_candle_open_with_slippage():
    df5, df15 = _data()
    a = Analyzer(df5, df15)
    sigs = precompute_signals(a)
    res = simulate(a, sigs, TradingParams(), int(df5["close_time"].iloc[0]), int(df5["close_time"].iloc[-1]))
    assert res.trades
    opens = dict(zip(df5["open_time"], df5["open"]))
    for t in res.trades:
        assert t["entry_time"] == t["signal_time"] + 1  # abertura do candle seguinte ao sinal
        assert t["entry_price"] == opens[t["entry_time"]] * 1.0005


def test_fees_and_slippage_are_charged():
    df5, df15 = _data()
    a = Analyzer(df5, df15)
    sigs = precompute_signals(a)
    span = (int(df5["close_time"].iloc[0]), int(df5["close_time"].iloc[-1]))
    with_costs = simulate(a, sigs, TradingParams(), *span)
    free = simulate(a, sigs, TradingParams(fee_rate=0, slippage_rate=0), *span)
    assert with_costs.metrics["trades"] == free.metrics["trades"] > 0
    assert with_costs.metrics["fees_paid"] > 0 and free.metrics["fees_paid"] == 0
    assert with_costs.metrics["final_equity"] < free.metrics["final_equity"]


def test_walk_forward_tests_on_unseen_period():
    df5, df15 = _data(1500)
    rep = walk_forward(df5, df15, TradingParams())
    assert rep["parte1_escolhido"]["fim"] < rep["parte2_fora_da_amostra"]["inicio"]
    assert len(rep["dentro_da_amostra"]) == len(GRID)
    chosen = (rep["escolhido"]["atr_stop_mult"], rep["escolhido"]["take_profit_r"])
    assert chosen in GRID or chosen == (1.5, 2.0)
    assert rep["parte2_parametros_padrao"]["parametros"] == {"atr_stop_mult": 1.5, "take_profit_r": 2.0}
    assert any("não garantem" in n for n in rep["avisos"])
    md = format_markdown(rep)
    assert "FORA da amostra" in md and "Taxa de acerto" in md
    json.dumps(rep)  # relatório precisa ser JSON válido (sem NaN/infinito)


def test_parameter_choice_ignores_out_of_sample_data():
    """Mudar os dados da parte 2 não pode mudar a escolha feita na parte 1."""
    df5, df15 = _data(1500)
    rep = walk_forward(df5, df15, TradingParams())
    shocked = df5.copy()
    shocked.loc[1300:, ["open", "high", "low", "close"]] *= 1.3
    rep2 = walk_forward(shocked, aggregate(shocked), TradingParams())
    assert rep["periodo"]["divisao"] == rep2["periodo"]["divisao"]
    assert rep["escolhido"] == rep2["escolhido"]
    assert rep["dentro_da_amostra"] == rep2["dentro_da_amostra"]


def test_run_full_backtest_downloads_history_through_market_data():
    now = int(time.time() * 1000)
    start = now - now % M15 - 1200 * M5
    df5 = make_5m(1200, seed=51, start=start)
    df15 = aggregate(df5)
    market = MarketData([], providers=[FakeProvider("historico", df5, df15, Clock(now))])
    rep = run_full_backtest(Settings(), days=3, market=market)
    assert rep["fonte_de_dados"] == "historico"
    assert rep["candles_5m"] > 800


def test_cli_with_csv_files(tmp_path, capsys):
    df5, df15 = _data(900)
    df5.to_csv(tmp_path / "btcusdt_5m.csv", index=False)
    df15.to_csv(tmp_path / "btcusdt_15m.csv", index=False)
    out = tmp_path / "rel.json"
    assert main(["--csv-dir", str(tmp_path), "--json", str(out), "--markdown", str(tmp_path / "r.md")]) == 0
    assert json.loads(out.read_text())["fonte_de_dados"] == "arquivo CSV"
    assert "Resultado do backtest" in capsys.readouterr().out


def test_daily_loss_never_exceeds_3pct_of_initial_balance():
    """Sem gaps de preço, a perda de nenhum dia passa de 3% do saldo inicial (US$ 30)."""
    params = TradingParams(atr_stop_mult=1.0)
    tz = ZoneInfo(params.timezone)
    total_trades = 0
    for seed in (4, 8, 51):
        df5, df15 = _data(1500, seed=seed)
        a = Analyzer(df5, df15)
        res = simulate(a, precompute_signals(a), params, int(df5["close_time"].iloc[0]),
                       int(df5["close_time"].iloc[-1]))
        total_trades += res.metrics["trades"]
        day_start, prev = {}, params.initial_balance
        for t, eq in res.equity:
            day = datetime.fromtimestamp(t / 1000, tz).date()
            day_start.setdefault(day, prev)
            assert day_start[day] - eq <= 30.0 + 1e-6
            prev = eq
    assert total_trades > 0


def test_report_warns_when_every_combination_lost_and_that_live_robot_is_unchanged():
    for seed in (51, 4, 8):
        df5, df15 = _data(1500, seed=seed)
        rep = walk_forward(df5, df15, TradingParams())
        all_lost = all(row["retorno_pct"] <= 0 for row in rep["dentro_da_amostra"])
        optimized = any(row["operacoes"] >= 5 for row in rep["dentro_da_amostra"])
        warned = any("menos ruim" in n for n in rep["avisos"])
        assert warned == (all_lost and optimized), seed
        assert any("robô ao vivo usa ATR_STOP_MULT" in n for n in rep["avisos"])
