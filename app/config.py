"""Configurações do robô, lidas de variáveis de ambiente.

Segredos (token do Telegram, senha do painel) NUNCA ficam no código:
vêm de variáveis de ambiente configuradas na hospedagem (ou num arquivo
.env local, que não vai para o GitHub).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# Este projeto só funciona em modo simulado. Não existe código para enviar
# ordens reais a nenhuma corretora, e esta constante não pode ser alterada
# por variável de ambiente.
PAPER_TRADING = True

# Limites máximos exigidos pelas regras do projeto.
MAX_RISK_PER_TRADE = 0.01  # 1% do saldo por operação
MAX_DAILY_LOSS = 0.03  # 3% do saldo inicial por dia

_EXCHANGE_WORDS = ("BINANCE", "OKX", "BYBIT", "KUCOIN", "COINBASE", "KRAKEN", "EXCHANGE", "BROKER", "CORRETORA")
_CREDENTIAL_WORDS = ("KEY", "SECRET", "PASSPHRASE", "TOKEN", "PASSWORD", "SENHA")
_TRUE_VALUES = {"1", "true", "yes", "sim", "on", "ligado"}


class SafetyError(RuntimeError):
    """Configuração que violaria o modo simulado obrigatório."""


def check_safety(env: dict[str, str] | None = None) -> None:
    """Recusa iniciar se alguém tentar desligar o paper trading ou
    fornecer credenciais de corretora."""
    env = dict(os.environ if env is None else env)

    value = env.get("PAPER_TRADING")
    if value is not None and value.strip().lower() not in _TRUE_VALUES:
        raise SafetyError(
            "PAPER_TRADING precisa ficar ligado. Este robô só opera com dinheiro fictício."
        )

    suspicious = [
        name
        for name in env
        if any(w in name.upper() for w in _EXCHANGE_WORDS)
        and any(w in name.upper() for w in _CREDENTIAL_WORDS)
    ]
    if suspicious:
        raise SafetyError(
            "Credenciais de corretora detectadas nas variáveis de ambiente "
            f"({', '.join(sorted(suspicious))}). Remova-as: este robô não usa contas reais."
        )


def _float(env: dict[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw.replace(",", "."))
    except ValueError as exc:
        raise ValueError(f"Variável {name} inválida: {raw!r} (esperado um número)") from exc


def _int(env: dict[str, str], name: str, default: int) -> int:
    return int(_float(env, name, default))


@dataclass(frozen=True)
class TradingParams:
    """Parâmetros de risco e execução simulada (usados no robô e no backtest)."""

    initial_balance: float = 1000.0
    risk_per_trade: float = 0.01
    daily_loss_limit: float = 0.03
    fee_rate: float = 0.001  # 0,1% por lado (taxa típica de corretora spot)
    slippage_rate: float = 0.0005  # 0,05% de deslizamento por execução
    atr_stop_mult: float = 1.5  # stop-loss = entrada - 1,5 x ATR
    take_profit_r: float = 2.0  # alvo = 2x a distância do stop
    min_notional: float = 10.0  # valor mínimo de uma ordem (US$)
    timezone: str = "America/Sao_Paulo"  # define quando "vira o dia"

    def __post_init__(self) -> None:
        if not 0 < self.risk_per_trade <= MAX_RISK_PER_TRADE:
            raise ValueError("RISK_PER_TRADE deve ser maior que 0 e no máximo 0.01 (1%).")
        if not 0 < self.daily_loss_limit <= MAX_DAILY_LOSS:
            raise ValueError("DAILY_LOSS_LIMIT deve ser maior que 0 e no máximo 0.03 (3%).")
        if self.initial_balance <= 0:
            raise ValueError("INITIAL_BALANCE deve ser positivo.")
        if self.fee_rate < 0 or self.slippage_rate < 0:
            raise ValueError("Taxas e slippage não podem ser negativos.")
        if self.atr_stop_mult <= 0 or self.take_profit_r <= 0:
            raise ValueError("ATR_STOP_MULT e TAKE_PROFIT_R devem ser positivos.")


@dataclass(frozen=True)
class Settings:
    symbol: str = "BTCUSDT"
    entry_interval: str = "5m"
    trend_interval: str = "15m"
    candles_limit: int = 300
    poll_seconds: int = 20
    data_providers: tuple[str, ...] = ("binance", "okx", "coinbase")
    data_dir: Path = Path("data")
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    dashboard_password: str = ""
    secret_key: str = ""
    engine_enabled: bool = True
    trading: TradingParams = field(default_factory=TradingParams)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "robo.db"

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def paper_trading(self) -> bool:
        return PAPER_TRADING


def load_settings(env: dict[str, str] | None = None) -> Settings:
    env = dict(os.environ if env is None else env)
    check_safety(env)

    trading = TradingParams(
        initial_balance=_float(env, "INITIAL_BALANCE", 1000.0),
        risk_per_trade=_float(env, "RISK_PER_TRADE", 0.01),
        daily_loss_limit=_float(env, "DAILY_LOSS_LIMIT", 0.03),
        fee_rate=_float(env, "FEE_RATE", 0.001),
        slippage_rate=_float(env, "SLIPPAGE_RATE", 0.0005),
        atr_stop_mult=_float(env, "ATR_STOP_MULT", 1.5),
        take_profit_r=_float(env, "TAKE_PROFIT_R", 2.0),
        min_notional=_float(env, "MIN_NOTIONAL", 10.0),
        timezone=env.get("TIMEZONE", "America/Sao_Paulo") or "America/Sao_Paulo",
    )
    providers = tuple(
        p.strip().lower()
        for p in env.get("DATA_PROVIDERS", "binance,okx,coinbase").split(",")
        if p.strip()
    )
    return Settings(
        poll_seconds=max(5, _int(env, "POLL_SECONDS", 20)),
        data_providers=providers or ("binance", "okx", "coinbase"),
        data_dir=Path(env.get("DATA_DIR", "data") or "data"),
        telegram_bot_token=env.get("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=env.get("TELEGRAM_CHAT_ID", "").strip(),
        dashboard_password=env.get("DASHBOARD_PASSWORD", ""),
        secret_key=env.get("SECRET_KEY", ""),
        engine_enabled=env.get("ENGINE_ENABLED", "true").strip().lower() in _TRUE_VALUES,
        trading=trading,
    )
