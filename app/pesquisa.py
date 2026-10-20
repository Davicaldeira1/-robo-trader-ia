"""Estudo comparativo de estratégias em períodos NOVOS (plano pré-registrado).

Regras definidas ANTES de ver qualquer resultado:

Períodos (UTC, sem sobreposição, em ordem de tempo):
  desenvolvimento  2025-04-01 → 2025-12-31  cada estratégia escolhe seus parâmetros
  validação        2026-01-01 → 2026-05-31  compara as estratégias já congeladas
  reserva final    2026-06-01 → 2026-09-17  só para quem passou na validação
  EXCLUÍDO         2026-09-18 → 2026-10-20  já foi visto no backtest anterior (nem é baixado)

Estratégias candidatas (lista fechada):
  A  regras atuais (entrada 5m, tendência 15m)
  B  A + filtro de custo: só compra se o alvo for >= 2x o custo de ida e volta
  C  as mesmas regras em tempos maiores (entrada 15m, tendência 1h)
  D  seguidor de tendência 1h: compra no cruzamento EMA20 > EMA50, sai no cruzamento contrário

Regra de decisão: uma estratégia só é "candidata" se tiver lucro DEPOIS dos
custos e pelo menos 30 operações na validação E na reserva final. Não há
meta de taxa de acerto. Nada aqui altera o robô ao vivo e tudo é simulado.

Uso:  python -m app.pesquisa --json pesquisa.json --markdown pesquisa.md
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

from .analysis import Analyzer
from .backtest import BacktestResult, iso, precompute_signals, simulate
from .config import TradingParams, load_settings
from .indicators import add_indicators
from .market_data import MarketData, interval_ms
from .metrics import json_safe
from .strategy import BUY, SELL, WAIT, Signal

log = logging.getLogger(__name__)

PERIODS: dict[str, tuple[str, str]] = {
    "desenvolvimento": ("2025-04-01", "2025-12-31"),
    "validacao": ("2026-01-01", "2026-05-31"),
    "reserva": ("2026-06-01", "2026-09-17"),
}
EXCLUDED = ("2026-09-18", "2026-10-20")
WARMUP_DAYS = 10
MIN_TRADES = 30
N_RANDOM = 200
INTERVALS = ("5m", "15m", "1h")

GRID_RULES = ((1.5, 1.5), (1.5, 2.0), (1.5, 3.0), (3.0, 1.5), (3.0, 2.0), (3.0, 3.0))
# take_profit_r = 100 equivale a "sem alvo": a saída fica por conta do stop ou do cruzamento.
GRID_TREND = ((2.0, 3.0), (2.0, 100.0), (3.0, 3.0), (3.0, 100.0))


@dataclass(frozen=True)
class Candidate:
    key: str
    name: str
    description: str
    entry: str
    trend: str | None
    grid: tuple[tuple[float, float], ...]
    cost_filter: bool = False


CANDIDATES = (
    Candidate("A", "Regras atuais (5m + 15m)", "Exatamente as regras do robô hoje.", "5m", "15m", GRID_RULES),
    Candidate("B", "Atuais + filtro de custo", "Só compra se o alvo for pelo menos 2x o custo de ida e volta.",
              "5m", "15m", GRID_RULES, cost_filter=True),
    Candidate("C", "Mesmas regras em 15m + 1h", "Entrada no 15m, tendência no 1h (movimentos maiores).",
              "15m", "1h", GRID_RULES),
    Candidate("D", "Tendência 1h (EMA20/50)", "Compra quando a EMA20 cruza acima da EMA50 no 1h; "
              "vende no cruzamento contrário.", "1h", None, GRID_TREND),
)


# ----------------------------------------------------------------- períodos
def day_start_ms(day: str) -> int:
    return int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp() * 1000)


def period_bounds(period: tuple[str, str]) -> tuple[int, int]:
    """Do início do 1º dia até o último milissegundo do último dia (UTC)."""
    return day_start_ms(period[0]), day_start_ms(period[1]) + 86_400_000 - 1


def check_periods(periods: dict[str, tuple[str, str]], excluded=EXCLUDED) -> None:
    """Garante ordem cronológica, nenhuma sobreposição e nada dentro do período já visto."""
    bounds = [period_bounds(p) for p in periods.values()]
    for (_, end), (start, _) in zip(bounds, bounds[1:]):
        if start <= end:
            raise ValueError("Períodos fora de ordem ou sobrepostos.")
    ex_start, ex_end = period_bounds(excluded)
    for start, end in bounds:
        if start <= ex_end and end >= ex_start:
            raise ValueError("Um período usa dados já vistos no backtest anterior.")


# ------------------------------------------------------------------ sinais
def trend_signals(df: pd.DataFrame, interval: str) -> tuple[pd.DataFrame, list[Signal]]:
    """Estratégia D: cruzamento de EMA20/EMA50 no próprio tempo gráfico (sem olhar o futuro)."""
    ind = add_indicators(df.reset_index(drop=True), 20, 50)
    fast, slow = ind["ema_fast"].to_numpy(), ind["ema_slow"].to_numpy()
    close, atr_ = ind["close"].to_numpy(), ind["atr"].to_numpy()
    times = ind["close_time"].to_numpy(dtype=np.int64)
    missing = np.zeros(len(ind), dtype=np.int64)
    missing[1:] = np.diff(ind["open_time"].to_numpy(dtype=np.int64)) // interval_ms(interval) - 1
    cum = np.cumsum(missing)
    signals = []
    for i in range(len(ind)):
        window_gaps = cum[i] - cum[max(0, i - 99)]
        values = (fast[i], slow[i], atr_[i], fast[i - 1] if i else np.nan, slow[i - 1] if i else np.nan)
        ok = i + 1 >= 100 and window_gaps == 0 and not any(np.isnan(v) for v in values) and atr_[i] > 0
        t, price = int(times[i]), float(close[i])
        if not ok:
            signals.append(Signal(WAIT, "Dados insuficientes", [], t, price, float(atr_[i]), False))
        elif fast[i] > slow[i] and fast[i - 1] <= slow[i - 1]:
            signals.append(Signal(BUY, "EMA20 cruzou acima da EMA50 (1h)", [], t, price, float(atr_[i]), True))
        elif fast[i] < slow[i]:
            signals.append(Signal(SELL, "EMA20 abaixo da EMA50 (1h)", [], t, price, float(atr_[i]), True))
        else:
            signals.append(Signal(WAIT, "Sem cruzamento novo", [], t, price, float(atr_[i]), True))
    return ind, signals


def apply_cost_filter(signals: list[Signal], params: TradingParams) -> list[Signal]:
    """Estratégia B: troca COMPRA por AGUARDAR quando o alvo não paga 2x o custo de ida e volta."""
    round_trip = 2 * params.fee_rate + 2 * params.slippage_rate
    out = []
    for s in signals:
        if s.action == BUY and s.price > 0:
            target_pct = params.take_profit_r * params.atr_stop_mult * s.atr / s.price
            if target_pct < 2 * round_trip:
                s = replace(s, action=WAIT, summary="Alvo pequeno demais para os custos (filtro B)")
        out.append(s)
    return out


@dataclass
class Market:
    """Candles (com indicadores) de um tempo gráfico e os sinais de uma estratégia."""

    candles: SimpleNamespace  # .ind5 (DataFrame) e .close_time5 (array): o formato que simulate() usa
    signals: list[Signal]

    @property
    def times(self) -> np.ndarray:
        return self.candles.close_time5


def build_market(candidate: Candidate, frames: dict[str, pd.DataFrame]) -> Market:
    if candidate.trend is None:
        ind, signals = trend_signals(frames[candidate.entry], candidate.entry)
        return Market(SimpleNamespace(ind5=ind, close_time5=ind["close_time"].to_numpy(dtype=np.int64)), signals)
    analyzer = Analyzer(frames[candidate.entry], frames[candidate.trend], candidate.entry, candidate.trend)
    return Market(analyzer, precompute_signals(analyzer))


def run(candidate: Candidate, market: Market, params: TradingParams, bounds: tuple[int, int]) -> BacktestResult:
    signals = apply_cost_filter(market.signals, params) if candidate.cost_filter else market.signals
    return simulate(market.candles, signals, params, *bounds)


# ------------------------------------------------------------- estatística
def bootstrap_mean_ci(pnls: list[float], n: int = 2000, seed: int = 0) -> tuple[float, float] | None:
    """Intervalo de 95% para o resultado médio por operação (reamostragem das operações)."""
    if len(pnls) < 2:
        return None
    rng = np.random.default_rng(seed)
    arr = np.asarray(pnls)
    means = arr[rng.integers(0, len(arr), (n, len(arr)))].mean(axis=1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


class _SparseSignals:
    """Lista de sinais em que só algumas posições são COMPRA (o resto é AGUARDAR)."""

    _wait = Signal(WAIT, "", [], 0, 0.0, 0.0, True)

    def __init__(self, buys: dict[int, Signal]):
        self.buys = buys

    def __getitem__(self, i: int) -> Signal:
        return self.buys.get(i, self._wait)


def random_benchmark(candidate: Candidate, market: Market, params: TradingParams, bounds: tuple[int, int],
                     strategy_return: float, n: int = N_RANDOM, seed: int = 42) -> dict:
    """Entradas ALEATÓRIAS com a mesma frequência de compras e as mesmas saídas (stop/alvo).

    Mede se o sinal da estratégia acrescenta algo além da sorte.
    """
    signals = apply_cost_filter(market.signals, params) if candidate.cost_filter else market.signals
    times = market.times
    idx = np.flatnonzero((times >= bounds[0]) & (times <= bounds[1]))
    usable = np.array([int(i) for i in idx if signals[int(i)].data_ok], dtype=np.int64)
    n_buys = sum(1 for i in usable if signals[int(i)].action == BUY)
    if len(usable) == 0 or n_buys == 0:
        return {"simulacoes": 0}
    prob = n_buys / len(usable)
    rng = np.random.default_rng(seed)
    returns = []
    for _ in range(n):
        chosen = [int(i) for i in usable[rng.random(len(usable)) < prob]]
        buys = {i: Signal(BUY, "Entrada aleatória", [], signals[i].time_ms, signals[i].price, signals[i].atr, True)
                for i in chosen}
        returns.append(simulate(market.candles, _SparseSignals(buys), params, *bounds).metrics["return_pct"])
    arr = np.asarray(returns)
    return {"simulacoes": n, "retorno_mediano_pct": float(np.median(arr)),
            "pct_aleatorias_melhores": float((arr >= strategy_return).mean() * 100)}


def summarize(res: BacktestResult) -> dict:
    m = res.metrics
    return {
        "operacoes": m["trades"], "retorno_pct": m["return_pct"], "antes_dos_custos": m["gross_pnl"],
        "custos": m["costs"], "taxa_acerto": m["win_rate"], "fator_lucro": m["profit_factor"],
        "drawdown_pct": m["max_drawdown_pct"], "tempo_medio_min": m["avg_holding_min"],
        "saidas": m["exit_reasons"], "comprar_e_segurar_pct": m["buy_hold_pct"],
        "ic95_media_por_operacao": bootstrap_mean_ci([t["pnl"] for t in res.trades]),
    }


def select_params(candidate: Candidate, market: Market, base: TradingParams,
                  dev: tuple[int, int]) -> tuple[TradingParams, list[dict], str]:
    """Escolhe os parâmetros usando SOMENTE o período de desenvolvimento."""
    rows = []
    for atr_mult, tp_r in candidate.grid:
        params = replace(base, atr_stop_mult=atr_mult, take_profit_r=tp_r)
        res = run(candidate, market, params, dev)
        rows.append((params, res))
    table = [{"atr_stop_mult": p.atr_stop_mult, "take_profit_r": p.take_profit_r, **summarize(r)}
             for p, r in rows]
    for min_trades, note in ((MIN_TRADES, ""), (10, f"nenhuma combinação teve {MIN_TRADES}+ operações")):
        eligible = [(p, r) for p, r in rows if r.metrics["trades"] >= min_trades]
        if eligible:
            params = max(eligible, key=lambda pr: pr[1].metrics["return_pct"])[0]
            if all(r.metrics["return_pct"] <= 0 for _, r in rows):
                note = (note + "; " if note else "") + "todas as combinações perderam — escolhida a menos ruim"
            return params, table, note
    return replace(base, atr_stop_mult=candidate.grid[0][0], take_profit_r=candidate.grid[0][1]), table, \
        "menos de 10 operações em todas as combinações — usado o primeiro parâmetro da lista"


def market_diagnostics(frames: dict[str, pd.DataFrame], bounds: tuple[int, int]) -> dict:
    """ATR mediano em % do preço por tempo gráfico (para comparar com o custo de 0,30%)."""
    out = {}
    for interval, df in frames.items():
        ind = add_indicators(df, 9, 21)
        mask = (ind["close_time"] >= bounds[0]) & (ind["close_time"] <= bounds[1])
        pct = (ind["atr"] / ind["close"])[mask].dropna()
        out[interval] = float(pct.median() * 100) if len(pct) else None
    return out


def buy_and_hold(df: pd.DataFrame, bounds: tuple[int, int]) -> float | None:
    sl = df[(df["close_time"] >= bounds[0]) & (df["close_time"] <= bounds[1])]
    if sl.empty:
        return None
    return float((sl["close"].iloc[-1] / sl["open"].iloc[0] - 1) * 100)


# ------------------------------------------------------------------ estudo
def run_study(frames: dict[str, pd.DataFrame], base: TradingParams,
              periods: dict[str, tuple[str, str]] = PERIODS, n_random: int = N_RANDOM,
              candidates=CANDIDATES) -> dict:
    check_periods(periods)
    t0 = time.time()
    names = list(periods)
    dev, val, hold = (period_bounds(periods[n]) for n in names)
    round_trip = 2 * base.fee_rate + 2 * base.slippage_rate
    costs = {"zero": (0.0, 0.0), "menores (0,075% + 0,05%)": (0.00075, 0.0005),
             "maiores (0,10% + 0,10%)": (0.001, 0.001)}
    report: dict = {
        "gerado_em": iso(int(time.time() * 1000)),
        "periodos": {n: {"inicio": p[0], "fim": p[1]} for n, p in periods.items()},
        "excluido": {"inicio": EXCLUDED[0], "fim": EXCLUDED[1]},
        "custo_ida_e_volta_pct": round_trip * 100,
        "aleatorias": n_random,
        "regra": (f"Candidata só com lucro depois dos custos e {MIN_TRADES}+ operações na validação "
                  "E na reserva final."),
        "mercado": {n: {"atr_mediano_pct": market_diagnostics(frames, period_bounds(p)),
                        "comprar_e_segurar_pct": buy_and_hold(frames["5m"], period_bounds(p))}
                    for n, p in periods.items()},
        "estrategias": [],
    }
    for cand in candidates:
        market = build_market(cand, frames)
        params, dev_table, note = select_params(cand, market, base, dev)
        val_res = run(cand, market, params, val)
        val_sum = summarize(val_res)
        entry = {
            "chave": cand.key, "nome": cand.name, "descricao": cand.description,
            "parametros": {"atr_stop_mult": params.atr_stop_mult, "take_profit_r": params.take_profit_r},
            "nota_selecao": note,
            "desenvolvimento": dev_table,
            "validacao": val_sum,
            "validacao_aleatorio": random_benchmark(cand, market, params, val, val_sum["retorno_pct"], n_random),
            "validacao_custos": {label: summarize(run(cand, market, replace(params, fee_rate=f, slippage_rate=s),
                                                      val))["retorno_pct"]
                                 for label, (f, s) in costs.items()},
        }
        if val_sum["operacoes"] < MIN_TRADES:
            entry["veredito"] = f"inconclusiva: menos de {MIN_TRADES} operações na validação"
        elif val_sum["retorno_pct"] <= 0:
            entry["veredito"] = "reprovada na validação (prejuízo depois dos custos)"
        else:
            hold_res = run(cand, market, params, hold)
            hold_sum = summarize(hold_res)
            entry["reserva"] = hold_sum
            entry["reserva_aleatorio"] = random_benchmark(cand, market, params, hold, hold_sum["retorno_pct"],
                                                          n_random)
            if hold_sum["operacoes"] < MIN_TRADES:
                entry["veredito"] = f"inconclusiva: menos de {MIN_TRADES} operações na reserva final"
            elif hold_sum["retorno_pct"] <= 0:
                entry["veredito"] = "reprovada na reserva final"
            else:
                entry["veredito"] = "CANDIDATA (passou na validação e na reserva; ainda exige paper trading)"
        # Referência: o robô exatamente como está hoje. A reserva final fica de fora de propósito:
        # ela só é usada por estratégias que passaram na validação.
        if cand.key == "A":
            report["robo_atual"] = {n: summarize(run(cand, market, base, period_bounds(periods[n])))
                                    for n in names[:2]}
        report["estrategias"].append(entry)
        log.info("Estratégia %s: %s", cand.key, entry["veredito"])
    report["duracao_s"] = round(time.time() - t0, 1)
    return json_safe(report)


def fetch_frames(market: MarketData | None = None, periods=PERIODS) -> tuple[str, dict[str, pd.DataFrame]]:
    check_periods(periods)
    settings = load_settings()
    market = market or MarketData(settings.data_providers, settings.symbol, timeout=20)
    start = period_bounds(next(iter(periods.values())))[0] - WARMUP_DAYS * 86_400_000
    end = period_bounds(list(periods.values())[-1])[1]
    provider, frames = market.fetch_history(list(INTERVALS), start, end)
    return provider, {k: df[df["close_time"] <= end].reset_index(drop=True) for k, df in frames.items()}


# ---------------------------------------------------------------- relatório
def _pct(x) -> str:
    return "—" if x is None else f"{x:+.2f}%"


def _num(x, nd=2) -> str:
    return "—" if x is None else f"{x:.{nd}f}"


def _ci(ci) -> str:
    return "—" if not ci else f"US$ {ci[0]:+.2f} a {ci[1]:+.2f}"


def format_markdown(report: dict) -> str:
    p = report["periodos"]
    lines = [
        "## Estudo comparativo de estratégias (dados reais, dinheiro fictício)",
        "",
        f"Fonte: **{report.get('fonte_de_dados', '?')}** · Gerado em {report['gerado_em']} · "
        f"Custo de ida e volta simulado: {report['custo_ida_e_volta_pct']:.2f}%",
        "",
        "| Período | Datas (UTC) | Uso | Comprar e segurar |",
        "|---|---|---|---|",
    ]
    usos = {"desenvolvimento": "escolher parâmetros", "validacao": "comparar estratégias",
            "reserva": "teste final (só aprovadas)"}
    for name, d in p.items():
        lines.append(f"| {name} | {d['inicio']} → {d['fim']} | {usos.get(name, '')} | "
                     f"{_pct(report['mercado'][name]['comprar_e_segurar_pct'])} |")
    lines.append(f"| excluído | {report['excluido']['inicio']} → {report['excluido']['fim']} | "
                 "já visto antes — não usado | — |")
    lines += ["", f"**Regra fixada antes:** {report['regra']}", "",
              "### Tamanho médio dos candles (ATR mediano, % do preço) × custo de ida e volta",
              "", "| Período | 5m | 15m | 1h | custo |", "|---|---|---|---|---|"]
    for name, d in report["mercado"].items():
        a = d["atr_mediano_pct"]
        lines.append(f"| {name} | {_num(a.get('5m'), 3)}% | {_num(a.get('15m'), 3)}% | {_num(a.get('1h'), 3)}% | "
                     f"{report['custo_ida_e_volta_pct']:.2f}% |")

    span = {k: f"{v['inicio']} a {v['fim']}" for k, v in p.items()}
    lines += ["", f"### Validação ({span['validacao']}): estratégias com parâmetros congelados", "",
              "| Estratégia | Stop / alvo | Operações | Retorno | Antes dos custos | Custos | Acerto | "
              "Fator de lucro | Drawdown | Aleatório (mediana) | % aleatórias melhores | IC 95% por operação |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for e in report["estrategias"]:
        v, r = e["validacao"], e["validacao_aleatorio"]
        lines.append(
            f"| {e['chave']}. {e['nome']} | {e['parametros']['atr_stop_mult']}× ATR / "
            f"{e['parametros']['take_profit_r']}× | {v['operacoes']} | {_pct(v['retorno_pct'])} | "
            f"US$ {_num(v['antes_dos_custos'])} | US$ {_num(v['custos'])} | {_num(v['taxa_acerto'], 1)}% | "
            f"{_num(v['fator_lucro'])} | {_num(v['drawdown_pct'])}% | {_pct(r.get('retorno_mediano_pct'))} | "
            f"{_num(r.get('pct_aleatorias_melhores'), 0)}% | {_ci(v['ic95_media_por_operacao'])} |")
    lines += ["", f"_% aleatórias melhores_: fração de {report['aleatorias']} sorteios de entradas aleatórias (mesma frequência e "
              "mesmas saídas) que foram iguais ou melhores. Perto de 50% = o sinal não ajuda mais que a sorte.",
              "", "### Sensibilidade a custos na validação (retorno)", "",
              "| Estratégia | Atual (0,10% + 0,05%) | " + " | ".join(next(iter(report["estrategias"]))
                                                             ["validacao_custos"].keys()) + " |",
              "|---|---|" + "---|" * len(next(iter(report["estrategias"]))["validacao_custos"])]
    for e in report["estrategias"]:
        lines.append(f"| {e['chave']} | {_pct(e['validacao']['retorno_pct'])} | "
                     + " | ".join(_pct(x) for x in e["validacao_custos"].values()) + " |")

    lines += ["", f"### Reserva final ({span['reserva']}) e veredito", "",
              "| Estratégia | Reserva: operações | Reserva: retorno | Aleatório (mediana) | Veredito |",
              "|---|---|---|---|---|"]
    for e in report["estrategias"]:
        h = e.get("reserva")
        hr = e.get("reserva_aleatorio", {})
        lines.append(f"| {e['chave']} | {h['operacoes'] if h else 'não avaliada'} | "
                     f"{_pct(h['retorno_pct']) if h else '—'} | {_pct(hr.get('retorno_mediano_pct'))} | "
                     f"{e['veredito']} |")

    lines += ["", f"### Escolha de parâmetros no desenvolvimento ({span['desenvolvimento']})", ""]
    for e in report["estrategias"]:
        lines += [f"**{e['chave']}. {e['nome']}** — escolhido: stop {e['parametros']['atr_stop_mult']}× ATR, "
                  f"alvo {e['parametros']['take_profit_r']}×" + (f" ({e['nota_selecao']})" if e['nota_selecao']
                                                                    else ""), "",
                  "| Stop | Alvo | Operações | Retorno | Antes dos custos | Acerto |", "|---|---|---|---|---|---|"]
        for row in e["desenvolvimento"]:
            lines.append(f"| {row['atr_stop_mult']} | {row['take_profit_r']} | {row['operacoes']} | "
                         f"{_pct(row['retorno_pct'])} | US$ {_num(row['antes_dos_custos'])} | "
                         f"{_num(row['taxa_acerto'], 1)}% |")
        lines.append("")

    lines += ["### Robô atual exatamente como está (regras A, stop 1,5× ATR, alvo 2×; sem a reserva final)", "",
              "| Período | Operações | Retorno | Antes dos custos | Custos | Acerto | Saídas |",
              "|---|---|---|---|---|---|---|"]
    for name, s in report.get("robo_atual", {}).items():
        saidas = " · ".join(f"{k}: {v}" for k, v in s["saidas"].items()) or "—"
        lines.append(f"| {name} | {s['operacoes']} | {_pct(s['retorno_pct'])} | US$ {_num(s['antes_dos_custos'])} | "
                     f"US$ {_num(s['custos'])} | {_num(s['taxa_acerto'], 1)}% | {saidas} |")
    candidates = [e["chave"] for e in report["estrategias"] if e["veredito"].startswith("CANDIDATA")]
    lines += ["", "### Conclusão automática", "",
              (f"Estratégia(s) candidata(s): {', '.join(candidates)}. Mesmo assim, o próximo passo é paper "
               "trading — nada aqui autoriza dinheiro real." if candidates else
               "Nenhuma estratégia passou pela regra pré-definida: **sem vantagem demonstrada** depois dos custos."),
              "", "Resultados passados não garantem resultados futuros.", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Estudo comparativo pré-registrado (simulado)")
    parser.add_argument("--json", type=Path)
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--aleatorias", type=int, default=N_RANDOM)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        provider, frames = fetch_frames()
    except Exception as exc:  # noqa: BLE001
        print(f"ERRO ao baixar dados históricos: {exc}", file=sys.stderr)
        return 2
    print("Dados:", provider, {k: len(v) for k, v in frames.items()})
    report = run_study(frames, load_settings().trading, n_random=args.aleatorias)
    report["fonte_de_dados"] = provider
    report["candles"] = {k: len(v) for k, v in frames.items()}
    md = format_markdown(report)
    print(md)
    if args.json:
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    if args.markdown:
        args.markdown.write_text(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())

