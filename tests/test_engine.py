"""Testes do robô ao vivo: duplicidade, reinício, falhas de dados e recuperação."""
import pytest

from app.analysis import Analyzer
from app.backtest import precompute_signals, simulate
from app.config import Settings
from app.engine import Engine
from app.market_data import MarketData
from app.notifier import Notifier
from app.storage import Store
from tests.helpers import M5, Clock, FakeProvider, FakeTelegram, aggregate, make_5m

SEED = 51  # semente que gera várias compras com os dados sintéticos
N = 1200


@pytest.fixture
def data():
    df5 = make_5m(N, seed=SEED)
    return df5, aggregate(df5)


def make_engine(tmp_path, data, clock, providers=None, db="robo.db"):
    df5, df15 = data
    providers = providers or [FakeProvider("fonte1", df5, df15, clock)]
    settings = Settings(data_dir=tmp_path, candles_limit=5000, strategy="regras_5m")
    store = Store(tmp_path / db)
    return Engine(settings, store, MarketData([], providers=providers), clock=clock)


def run(engine, clock, start_idx, end_idx, df5):
    """Avança o relógio candle a candle (10 s após cada fechamento), como no servidor."""
    for i in range(start_idx, end_idx):
        clock.now = int(df5["close_time"].iloc[i]) + 10_000
        engine.tick()


def trade_rows(store):
    return sorted((t["id"], t["status"], round(t["entry_price"], 6), t["exit_reason"],
                   None if t["pnl"] is None else round(t["pnl"], 6)) for t in store.trades(1000))


def test_first_tick_analyzes_only_latest_closed_candle(tmp_path, data):
    df5, _ = data
    clock = Clock(int(df5["close_time"].iloc[400]) + 10_000)
    eng = make_engine(tmp_path, data, clock)
    result = eng.tick()
    assert result["ok"] and result["processed"] == 1
    assert eng.state.last_candle_close == int(df5["close_time"].iloc[400])
    assert len(eng.store.signals()) == 1


def test_repeated_ticks_do_not_duplicate_anything(tmp_path, data):
    df5, _ = data
    clock = Clock(0)
    eng = make_engine(tmp_path, data, clock)
    run(eng, clock, 300, N - 1, df5)
    trades, signals = trade_rows(eng.store), len(eng.store.signals(10_000))
    outbox = len(eng.store.messages(10_000))
    assert len(trades) >= 2, "os dados de teste deveriam gerar operações"
    for _ in range(5):  # mesmo candle processado de novo (ex.: ciclo repetido)
        eng.tick()
    assert trade_rows(eng.store) == trades
    assert len(eng.store.signals(10_000)) == signals
    assert len(eng.store.messages(10_000)) == outbox


def test_restart_resumes_without_duplicates_and_matches_uninterrupted_run(tmp_path, data):
    df5, _ = data
    # execução sem interrupção
    clock_a = Clock(0)
    eng_a = make_engine(tmp_path, data, clock_a, db="a.db")
    run(eng_a, clock_a, 300, N - 1, df5)

    # execução com "queda" do servidor no meio (novo Engine lendo o mesmo banco)
    clock_b = Clock(0)
    eng_b = make_engine(tmp_path, data, clock_b, db="b.db")
    run(eng_b, clock_b, 300, 700, df5)
    saved = eng_b.state
    eng_b2 = make_engine(tmp_path, data, clock_b, db="b.db")
    assert eng_b2.state == saved  # estado restaurado do disco
    run(eng_b2, clock_b, 700, N - 1, df5)

    assert trade_rows(eng_b2.store) == trade_rows(eng_a.store)
    assert eng_b2.state.cash == pytest.approx(eng_a.state.cash)


def test_live_engine_and_backtest_produce_identical_trades(tmp_path, data):
    """O backtest testa exatamente o mesmo comportamento do robô ao vivo."""
    df5, df15 = data
    clock = Clock(0)
    eng = make_engine(tmp_path, data, clock)
    run(eng, clock, 300, N - 1, df5)
    live = [(t["entry_time"], round(t["entry_price"], 6), t["exit_time"], round(t["pnl"], 6))
            for t in sorted(eng.store.closed_trades(), key=lambda t: t["entry_time"])]

    analyzer = Analyzer(df5.iloc[:N - 1], df15[df15["close_time"] <= df5["close_time"].iloc[N - 2]])
    signals = precompute_signals(analyzer)
    res = simulate(analyzer, signals, eng.params, int(df5["close_time"].iloc[300]),
                   int(df5["close_time"].iloc[N - 2]))
    bt = [(t["entry_time"], round(t["entry_price"], 6), t["exit_time"], round(t["pnl"], 6))
          for t in res.trades if t["exit_reason"] != "Fim do período de teste"]
    assert live and live == bt


def test_provider_failure_falls_back_to_next_source(tmp_path, data):
    df5, df15 = data
    clock = Clock(int(df5["close_time"].iloc[400]) + 10_000)
    bad = FakeProvider("fonte_ruim", df5, df15, clock)
    bad.fail = True
    good = FakeProvider("fonte_boa", df5, df15, clock)
    eng = make_engine(tmp_path, data, clock, providers=[bad, good])
    assert eng.tick()["provider"] == "fonte_boa"
    assert eng.market.last_errors and "fonte_ruim" in eng.market.last_errors[0]


def test_all_sources_down_no_trading_then_recovers(tmp_path, data):
    df5, df15 = data
    clock = Clock(0)
    src = FakeProvider("fonte", df5, df15, clock)
    eng = make_engine(tmp_path, data, clock, providers=[src])
    run(eng, clock, 300, 400, df5)
    state_before = eng.state
    src.fail = True
    for i in range(400, 404):
        clock.now = int(df5["close_time"].iloc[i]) + 10_000
        assert eng.tick()["ok"] is False  # não quebra, só registra
    assert eng.state == state_before  # nenhuma operação com dados ruins
    assert eng.consecutive_failures == 4
    events = [e["message"] for e in eng.store.events(50)]
    assert any("Falha ao obter dados" in m for m in events)
    texts = [m["text"] for m in eng.store.messages(50)]
    assert any("não consegue obter dados" in t for t in texts)
    src.fail = False
    assert eng.tick()["ok"] is True
    assert eng.consecutive_failures == 0
    assert any("normalizados" in m["text"] for m in eng.store.messages(50))


def test_long_downtime_only_checks_stops_and_does_not_buy_with_old_signals(tmp_path, data):
    df5, _ = data
    clock = Clock(0)
    eng = make_engine(tmp_path, data, clock)
    run(eng, clock, 300, 500, df5)
    signals_before = len(eng.store.signals(10_000))
    trades_before = {t["id"] for t in eng.store.trades(1000)}
    clock.now = int(df5["close_time"].iloc[700]) + 10_000  # 200 candles (~16 h) sem rodar
    eng.tick()
    assert len(eng.store.signals(10_000)) == signals_before + 1  # só o candle mais recente
    new_trades = [t for t in eng.store.trades(1000) if t["id"] not in trades_before]
    assert all(t["signal_time"] >= int(df5["close_time"].iloc[699]) for t in new_trades)
    assert any("Robô retomado" in e["message"] for e in eng.store.events(20))
    assert eng.state.last_candle_close == int(df5["close_time"].iloc[700])


def test_pause_blocks_new_entries_and_persists(tmp_path, data):
    df5, _ = data
    clock = Clock(0)
    eng = make_engine(tmp_path, data, clock)
    run(eng, clock, 300, 301, df5)
    eng.set_paused(True)
    run(eng, clock, 301, N - 1, df5)
    assert eng.store.trades(1000) == []
    assert make_engine(tmp_path, data, clock).state.paused is True  # sobrevive a reinício
    assert any("PAUSADO" in m["text"] for m in eng.store.messages(100))


def test_manual_close_position(tmp_path, data):
    df5, _ = data
    clock = Clock(0)
    eng = make_engine(tmp_path, data, clock)
    for i in range(300, N - 1):
        clock.now = int(df5["close_time"].iloc[i]) + 10_000
        eng.tick()
        if eng.state.position is not None:
            break
    assert eng.state.position is not None
    assert eng.close_position() is True
    assert eng.state.position is None
    closed = eng.store.closed_trades()[-1]
    assert closed["exit_reason"] == "Encerrada manualmente pelo painel"
    assert eng.close_position() is False


def test_status_reports_paper_trading_and_metrics(tmp_path, data):
    df5, _ = data
    clock = Clock(0)
    eng = make_engine(tmp_path, data, clock)
    run(eng, clock, 300, N - 1, df5)
    st = eng.status()
    assert st["paper_trading"] is True
    assert st["stats"]["trades"] == len(eng.store.closed_trades())
    assert st["equity"] == pytest.approx(st["cash"] + (st["position"]["qty"] * st["price"] if st["position"] else 0))
    assert 0 <= st["max_drawdown_pct"] < 100
    chart = eng.chart_data("5m")
    assert chart["candles"] and chart["ema_fast"]


def test_telegram_outbox_retries_and_never_duplicates(tmp_path, data):
    df5, _ = data
    clock = Clock(0)
    eng = make_engine(tmp_path, data, clock)
    run(eng, clock, 300, N - 1, df5)
    pending = len(eng.store.pending_messages(10_000))
    assert pending > 0
    tg = FakeTelegram(fail_times=2)
    notifier = Notifier(eng.store, tg)
    assert notifier.flush() == 0  # 1ª falha: para e tenta depois
    assert notifier.flush() == 0  # 2ª falha
    sent = 0
    while eng.store.pending_messages():
        sent += notifier.flush()
    assert sent == pending == len(tg.sent)
    assert notifier.flush() == 0  # nada é reenviado


def test_without_telegram_messages_are_marked_not_sent(tmp_path, data):
    df5, _ = data
    clock = Clock(int(df5["close_time"].iloc[400]) + 10_000)
    eng = make_engine(tmp_path, data, clock)
    eng.tick()
    eng.set_paused(True)
    Notifier(eng.store, FakeTelegram(enabled=False)).flush()
    assert eng.store.pending_messages() == []
    assert {m["status"] for m in eng.store.messages()} == {"sem_telegram"}
