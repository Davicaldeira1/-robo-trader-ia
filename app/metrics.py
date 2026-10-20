"""Estatísticas de desempenho usadas no painel e no backtest."""
from __future__ import annotations

import math


def max_drawdown(equity: list[float]) -> tuple[float, float]:
    """Maior queda do saldo a partir de um pico: (valor em US$, fração)."""
    peak = -math.inf
    worst_abs = worst_pct = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            worst_abs = max(worst_abs, peak - value)
            worst_pct = max(worst_pct, (peak - value) / peak)
    return worst_abs, worst_pct


def trade_stats(trades: list[dict]) -> dict:
    pnls = [t["pnl"] for t in trades if t.get("pnl") is not None]
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x <= 0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    n = len(pnls)
    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / n * 100 if n else None,
        "net_pnl": sum(pnls),
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": (gross_profit / gross_loss) if gross_loss > 0 else (None if not wins else math.inf),
        "avg_win": gross_profit / len(wins) if wins else None,
        "avg_loss": -gross_loss / len(losses) if losses else None,
        "expectancy": sum(pnls) / n if n else None,
        "fees": sum(t.get("entry_fee", 0) + t.get("exit_fee", 0) for t in trades),
    }


def json_safe(value):
    """Troca infinito/NaN por None para o JSON ficar válido."""
    if isinstance(value, float) and (math.isinf(value) or math.isnan(value)):
        return None
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return value
