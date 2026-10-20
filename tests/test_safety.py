"""Garantias de segurança: só paper trading, sem credenciais, sem ordens reais."""
import re
from pathlib import Path

import pytest

from app import config
from app.config import SafetyError, TradingParams, check_safety, load_settings

APP_DIR = Path(__file__).resolve().parent.parent / "app"


def test_paper_trading_is_hardcoded_on():
    assert config.PAPER_TRADING is True
    assert load_settings({}).paper_trading is True


@pytest.mark.parametrize("value", ["false", "0", "no", "nao", "off", "real"])
def test_refuses_to_start_if_paper_trading_disabled(value):
    with pytest.raises(SafetyError):
        load_settings({"PAPER_TRADING": value})


@pytest.mark.parametrize("value", ["true", "1", "sim", "TRUE"])
def test_paper_trading_on_values_are_accepted(value):
    assert load_settings({"PAPER_TRADING": value}).paper_trading


@pytest.mark.parametrize("name", ["BINANCE_API_KEY", "BINANCE_SECRET", "OKX_PASSPHRASE",
                                  "COINBASE_API_SECRET", "EXCHANGE_API_KEY", "BYBIT_KEY"])
def test_refuses_exchange_credentials(name):
    with pytest.raises(SafetyError):
        check_safety({name: "qualquer-coisa"})


def test_allowed_secrets_do_not_trigger_safety():
    check_safety({"TELEGRAM_BOT_TOKEN": "x", "DASHBOARD_PASSWORD": "y", "SECRET_KEY": "z"})


@pytest.mark.parametrize("kwargs", [{"risk_per_trade": 0.02}, {"risk_per_trade": 0},
                                    {"daily_loss_limit": 0.05}, {"initial_balance": -1},
                                    {"fee_rate": -0.1}])
def test_risk_limits_cannot_be_raised(kwargs):
    with pytest.raises(ValueError):
        TradingParams(**kwargs)


def test_env_risk_above_limit_is_refused():
    with pytest.raises(ValueError):
        load_settings({"RISK_PER_TRADE": "0.05"})


def test_no_real_order_code_anywhere():
    """Nenhum arquivo do robô contém endpoints de ordens ou autenticação de corretoras."""
    forbidden = [r"/api/v3/order", r"/sapi/", r"/api/v5/trade", r"/orders\b", r"X-MBX-APIKEY",
                 r"OK-ACCESS-KEY", r"CB-ACCESS-KEY", r"signature=", r"new_order", r"create_order",
                 r"\bccxt\b", r"python-binance"]
    for path in list(APP_DIR.rglob("*.py")) + [APP_DIR.parent / "requirements.txt"]:
        text = path.read_text()
        for pattern in forbidden:
            assert not re.search(pattern, text, re.IGNORECASE), f"{pattern} encontrado em {path.name}"


def test_market_data_only_reads():
    text = (APP_DIR / "market_data.py").read_text()
    assert ".post(" not in text and ".put(" not in text and ".delete(" not in text
    assert "api_key" not in text.lower()
