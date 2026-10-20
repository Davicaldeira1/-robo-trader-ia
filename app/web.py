"""Painel web (adaptado ao celular) e API do robô.

Iniciar:  uvicorn app.web:create_app --factory --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import fcntl
import hashlib
import hmac
import logging
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .backtest import format_markdown, run_full_backtest
from .config import Settings, load_settings
from .engine import Engine
from .market_data import MarketData
from .metrics import json_safe
from .notifier import Notifier, TelegramClient
from .storage import Store, now_ms

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
COOKIE = "robo_sessao"
SESSION_DAYS = 30


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # O httpx registra URLs; a do Telegram contém o token. Nunca deixar ir para o log.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


class Auth:
    """Login simples por senha (variável DASHBOARD_PASSWORD) com cookie assinado."""

    def __init__(self, password: str, secret: str):
        self.password = password
        key_material = secret or ("robo:" + password)
        self.key = hashlib.sha256(key_material.encode()).digest()
        self.failures: deque[float] = deque()
        self.failures_by_ip: dict[str, deque[float]] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.password)

    def _sig(self, payload: str) -> str:
        return hmac.new(self.key, payload.encode(), hashlib.sha256).hexdigest()

    def make_token(self) -> str:
        exp = str(int(time.time()) + SESSION_DAYS * 86400)
        return f"{exp}.{self._sig(exp)}"

    def valid(self, token: str | None) -> bool:
        if not self.enabled:
            return True
        if not token or "." not in token:
            return False
        exp, sig = token.split(".", 1)
        if not hmac.compare_digest(sig, self._sig(exp)):
            return False
        return exp.isdigit() and int(exp) > time.time()

    def blocked(self, ip: str) -> bool:
        now = time.time()
        for q in (self.failures, self.failures_by_ip.setdefault(ip, deque())):
            while q and now - q[0] > 300:
                q.popleft()
        return len(self.failures_by_ip[ip]) >= 5 or len(self.failures) >= 30

    def check_password(self, ip: str, password: str) -> bool:
        ok = hmac.compare_digest(password.encode(), self.password.encode())
        if not ok:
            now = time.time()
            self.failures.append(now)
            self.failures_by_ip.setdefault(ip, deque()).append(now)
        return ok


class BacktestRunner:
    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store
        self.lock = threading.Lock()
        self.running = False
        self.started_ms: int | None = None
        self.error: str | None = None

    def start(self, days: float) -> bool:
        with self.lock:
            if self.running:
                return False
            self.running, self.error, self.started_ms = True, None, now_ms()
        threading.Thread(target=self._run, args=(days,), daemon=True, name="backtest").start()
        return True

    def _run(self, days: float) -> None:
        try:
            report = run_full_backtest(self.settings, days)
            report["markdown"] = format_markdown(report)
            self.store.save_backtest(report)
            self.store.log_event("info", f"Backtest de {days:g} dias concluído (fonte {report['fonte_de_dados']}).")
        except Exception as exc:  # noqa: BLE001
            log.exception("Backtest falhou")
            self.error = str(exc)
            self.store.log_event("erro", f"Backtest falhou: {exc}")
        finally:
            self.running = False


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "?"


def create_app(settings: Settings | None = None, engine: Engine | None = None,
               telegram: TelegramClient | None = None, start_background: bool | None = None) -> FastAPI:
    setup_logging()
    settings = settings or load_settings()
    store = engine.store if engine else Store(settings.db_path)
    engine = engine or Engine(settings, store, MarketData(settings.data_providers, settings.symbol))
    telegram = telegram or TelegramClient(settings.telegram_bot_token, settings.telegram_chat_id)
    notifier = Notifier(store, telegram)
    auth = Auth(settings.dashboard_password, settings.secret_key)
    runner = BacktestRunner(settings, store)
    run_bg = settings.engine_enabled if start_background is None else start_background
    stop = threading.Event()
    threads: dict[str, threading.Thread] = {}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        lock_file = None
        if run_bg:
            # Garante UM único robô por banco de dados (evita operações duplicadas).
            lock_file = open(settings.data_dir / "engine.lock", "w")  # noqa: SIM115
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                log.error("Outro processo já está rodando o robô com este banco. Motor NÃO iniciado.")
                store.log_event("erro", "Motor não iniciado: outro processo já usa este banco de dados.")
                lock_file.close()
                lock_file = None
            else:
                threads["engine"] = threading.Thread(target=engine.run_forever, args=(stop,),
                                                     daemon=True, name="engine")
                threads["notifier"] = threading.Thread(target=notifier.run_forever, args=(stop,),
                                                       daemon=True, name="notifier")
                for t in threads.values():
                    t.start()
                log.info("Robô iniciado em modo PAPER TRADING.")
        yield
        stop.set()
        for t in threads.values():
            t.join(timeout=10)
        if lock_file:
            lock_file.close()

    app = FastAPI(title="Robô BTC/USDT (simulado)", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.engine, app.state.store, app.state.notifier = engine, store, notifier
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'")
        return response

    def authed(request: Request) -> bool:
        return auth.valid(request.cookies.get(COOKIE))

    def require_auth(request: Request) -> None:
        if not authed(request):
            raise HTTPException(401, "Faça login no painel.")

    def require_action(request: Request) -> None:
        require_auth(request)
        # Bloqueia formulários de outros sites (CSRF): só o painel envia este cabeçalho.
        if request.headers.get("x-requested-with") != "painel":
            raise HTTPException(403, "Requisição recusada.")

    # ------------------------------------------------------------ páginas
    @app.get("/", include_in_schema=False)
    def index(request: Request):
        if not authed(request):
            return RedirectResponse("/login", status_code=303)
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return FileResponse(STATIC / "icon-192.png")

    @app.get("/login", include_in_schema=False)
    def login_page(request: Request):
        if not auth.enabled or authed(request):
            return RedirectResponse("/", status_code=303)
        return FileResponse(STATIC / "login.html")

    @app.post("/login", include_in_schema=False)
    def login(request: Request, senha: str = Form("")):
        ip = _client_ip(request)
        if auth.blocked(ip):
            return RedirectResponse("/login?erro=bloqueado", status_code=303)
        if not auth.check_password(ip, senha):
            log.warning("Senha incorreta no painel (ip %s)", ip)
            return RedirectResponse("/login?erro=senha", status_code=303)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(COOKIE, auth.make_token(), max_age=SESSION_DAYS * 86400, httponly=True,
                        samesite="lax", secure=request.url.scheme == "https"
                        or request.headers.get("x-forwarded-proto") == "https")
        return resp

    @app.get("/logout", include_in_schema=False)
    def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE)
        return resp

    # ---------------------------------------------------- monitoramento
    @app.get("/health")
    def health(strict: bool = False):
        st = engine.status()
        alive = "engine" in threads and threads["engine"].is_alive()
        age = st["data_age_s"]
        data_ok = age is not None and age < 300
        body = {
            "status": "ok" if (alive and data_ok) else "degradado",
            "paper_trading": True,
            "motor_rodando": alive,
            "dados_ok": data_ok,
            "idade_dados_s": age,
            "falhas_seguidas": st["consecutive_failures"],
            "pausado": st["paused"],
            "fonte": st["provider"],
        }
        code = 503 if (strict and body["status"] != "ok") else 200
        return JSONResponse(json_safe(body), status_code=code)

    # --------------------------------------------------------------- API
    @app.get("/api/status", dependencies=[Depends(require_auth)])
    def api_status():
        st = engine.status()
        st["telegram_configurado"] = settings.telegram_enabled
        st["senha_configurada"] = auth.enabled
        st["motor_rodando"] = "engine" in threads and threads["engine"].is_alive()
        st["erros_24h"] = store.error_count(now_ms() - 86_400_000)
        return json_safe(st)

    @app.get("/api/chart", dependencies=[Depends(require_auth)])
    def api_chart(tf: str | None = None):
        tf = tf or settings.entry_interval
        if tf not in settings.intervals:
            raise HTTPException(400, "Tempo gráfico inválido.")
        data = engine.chart_data(tf)
        data["trades"] = store.trades(100)
        return json_safe(data)

    @app.get("/api/trades", dependencies=[Depends(require_auth)])
    def api_trades(limit: int = 100):
        return json_safe(store.trades(min(limit, 500)))

    @app.get("/api/equity", dependencies=[Depends(require_auth)])
    def api_equity(days: int = 7):
        since = now_ms() - min(days, 90) * 86_400_000
        series = store.equity_series(since)
        step = max(1, len(series) // 500)
        return [{"time": r["time_ms"] // 1000, "value": round(r["equity"], 2)} for r in series[::step]]

    @app.get("/api/events", dependencies=[Depends(require_auth)])
    def api_events(limit: int = 60):
        return store.events(min(limit, 300))

    @app.get("/api/signals", dependencies=[Depends(require_auth)])
    def api_signals(limit: int = 30):
        return json_safe(store.signals(min(limit, 200)))

    @app.post("/api/pause", dependencies=[Depends(require_action)])
    def api_pause():
        engine.set_paused(True)
        return {"ok": True, "paused": True}

    @app.post("/api/resume", dependencies=[Depends(require_action)])
    def api_resume():
        engine.set_paused(False)
        return {"ok": True, "paused": False}

    @app.post("/api/close-position", dependencies=[Depends(require_action)])
    def api_close():
        try:
            closed = engine.close_position()
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": closed, "message": "Posição encerrada." if closed else "Não há posição aberta."}

    @app.post("/api/backtest", dependencies=[Depends(require_action)])
    def api_backtest_start(days: float = 30):
        if not 3 <= days <= 60:
            raise HTTPException(400, "Escolha entre 3 e 60 dias.")
        started = runner.start(days)
        return {"ok": started, "message": "Backtest iniciado." if started else "Já existe um backtest rodando."}

    @app.get("/api/backtest", dependencies=[Depends(require_auth)])
    def api_backtest():
        return json_safe({"running": runner.running, "started_ms": runner.started_ms, "error": runner.error,
                          "report": store.latest_backtest()})

    @app.post("/api/telegram/test", dependencies=[Depends(require_action)])
    def api_telegram_test():
        if not telegram.enabled:
            return {"ok": False, "message": "Configure TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID na hospedagem."}
        try:
            telegram.send("✅ Teste do robô BTC/USDT: o Telegram está funcionando! (modo simulado)")
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"Falhou: {str(exc).replace(telegram.token, '***')}"}
        return {"ok": True, "message": "Mensagem de teste enviada. Confira o Telegram."}

    @app.get("/api/telegram/chats", dependencies=[Depends(require_auth)])
    def api_telegram_chats():
        try:
            chats = telegram.discover_chats()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": str(exc).replace(telegram.token or "\0", "***"), "chats": []}
        msg = ("Nenhuma conversa encontrada. Abra o seu bot no Telegram, toque em Iniciar (/start) "
               "e tente de novo.") if not chats else "Copie o número em chat_id."
        return {"ok": True, "message": msg, "chats": chats}

    @app.get("/api/messages", dependencies=[Depends(require_auth)])
    def api_messages():
        return store.messages(30)

    return app
