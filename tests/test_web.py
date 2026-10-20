"""Painel web: login, controles, monitoramento e cabeçalhos de segurança."""
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.engine import Engine
from app.market_data import MarketData
from app.storage import Store
from app.web import create_app
from tests.helpers import Clock, FakeProvider, FakeTelegram, aggregate, make_5m

PASSWORD = "senha-de-teste-123"
ACTION = {"X-Requested-With": "painel"}


def build(tmp_path, password=PASSWORD, start_background=False, telegram=None, strategy="regras_5m"):
    df5 = make_5m(600, seed=51)
    df15 = aggregate(df5)
    clock = Clock(int(df5["close_time"].iloc[500]) + 10_000)
    settings = Settings(data_dir=tmp_path, dashboard_password=password, secret_key="chave-teste",
                        engine_enabled=start_background, poll_seconds=5, strategy=strategy)
    engine = Engine(settings, Store(settings.db_path),
                    MarketData([], providers=[FakeProvider("teste", df5, df15, clock)]), clock=clock)
    engine.tick()
    app = create_app(settings, engine, telegram=telegram or FakeTelegram(), start_background=start_background)
    return app, engine


@pytest.fixture
def client(tmp_path):
    app, engine = build(tmp_path)
    c = TestClient(app, follow_redirects=False)
    c.engine = engine
    return c


def login(c, password=PASSWORD):
    return c.post("/login", data={"senha": password})


def test_dashboard_requires_login(client):
    r = client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.get("/api/status").status_code == 401


def test_wrong_password_is_rejected_and_rate_limited(client):
    for _ in range(5):
        r = login(client, "errada")
        assert r.headers["location"] == "/login?erro=senha"
    assert login(client, PASSWORD).headers["location"] == "/login?erro=bloqueado"
    assert client.get("/api/status").status_code == 401


def test_login_and_status(client):
    r = login(client)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert "httponly" in r.headers["set-cookie"].lower()
    page = client.get("/")
    assert page.status_code == 200 and "PAPER TRADING" in page.text
    st = client.get("/api/status").json()
    assert st["paper_trading"] is True
    assert st["initial_balance"] == 1000.0
    assert st["signal"]["action"] in ("COMPRA", "VENDA", "AGUARDAR")
    assert st["senha_configurada"] is True


def test_forged_cookie_is_rejected(client):
    client.cookies.set("robo_sessao", "9999999999.assinatura-falsa")
    assert client.get("/api/status").status_code == 401


def test_pause_requires_panel_header_and_persists(client):
    login(client)
    assert client.post("/api/pause").status_code == 403  # sem cabeçalho do painel (proteção CSRF)
    r = client.post("/api/pause", headers=ACTION)
    assert r.json() == {"ok": True, "paused": True}
    assert client.get("/api/status").json()["paused"] is True
    assert client.engine.store.get_kv("trader_state")["paused"] is True
    assert client.post("/api/resume", headers=ACTION).json()["paused"] is False


def test_data_endpoints(client):
    login(client)
    chart = client.get("/api/chart?tf=5m").json()
    assert chart["candles"] and chart["labels"] == ["EMA9", "EMA21"]
    assert client.get("/api/chart?tf=15m").json()["labels"] == ["EMA20", "EMA50"]
    assert client.get("/api/chart?tf=1h").status_code == 400
    assert isinstance(client.get("/api/trades").json(), list)
    assert isinstance(client.get("/api/equity").json(), list)
    assert client.get("/api/events").json()
    assert client.get("/api/backtest").json()["running"] is False
    assert client.post("/api/backtest?days=500", headers=ACTION).status_code == 400
    r = client.post("/api/close-position", headers=ACTION).json()
    assert r["ok"] is False and "Não há posição" in r["message"]


def test_telegram_endpoints(tmp_path):
    tg = FakeTelegram()
    app, _ = build(tmp_path, telegram=tg)
    c = TestClient(app, follow_redirects=False)
    login(c)
    assert c.post("/api/telegram/test", headers=ACTION).json()["ok"] is True
    assert tg.sent and "Telegram está funcionando" in tg.sent[0]
    chats = c.get("/api/telegram/chats").json()
    assert chats["chats"][0]["chat_id"] == 123


def test_health_endpoint_and_security_headers(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["paper_trading"] is True
    assert body["motor_rodando"] is False  # motor não foi iniciado neste teste
    assert client.get("/health?strict=true").status_code == 503
    assert r.headers["x-frame-options"] == "DENY"
    assert "script-src 'self'" in r.headers["content-security-policy"]


def test_without_password_panel_is_open_but_warns(tmp_path):
    app, _ = build(tmp_path, password="")
    c = TestClient(app, follow_redirects=False)
    assert c.get("/").status_code == 200
    assert c.get("/api/status").json()["senha_configurada"] is False


def test_only_one_engine_per_database(tmp_path):
    """Dois processos com o mesmo banco: só um roda o robô (evita operações duplicadas)."""
    app1, _ = build(tmp_path, start_background=True)
    app2, _ = build(tmp_path, start_background=True)
    with TestClient(app1) as c1, TestClient(app2) as c2:
        assert c1.get("/health").json()["motor_rodando"] is True
        assert c2.get("/health").json()["motor_rodando"] is False
