"""Backtest com validação em dados novos (walk-forward), sem vazamento do futuro.

Como funciona:
1. Baixa candles reais de 5m e 15m (histórico público).
2. Divide o período em duas partes pelo TEMPO:
   - Parte 1 ("dentro da amostra", 70%): testa algumas combinações de
     stop/alvo e escolhe a melhor.
   - Parte 2 ("fora da amostra", 30%): testa a escolha em dados que NÃO
     foram usados para escolher — é a parte que mais se aproxima do futuro.
3. Toda decisão usa só candles já fechados; a ordem é executada na abertura
   do candle seguinte; taxas e slippage são descontados em cada execução.

Uso (linha de comando):
    python -m app.backtest --days 30
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .analysis import Analyzer
from .config import NO_TARGET_R, Settings, TradingParams, load_settings
from .estrategias import build_analysis
from .market_data import MarketData, interval_ms
from .metrics import json_safe, max_drawdown, trade_stats
from .strategy import BUY, SELL, WAIT, Signal
from .trader import Candle, PaperTrader, TraderState

log = logging.getLogger(__name__)

GRID = [(atr, r) for atr in (1.0, 1.5, 2.0, 3.0) for r in (1.5, 2.0, 3.0)]
# Estratégia de tendência: alvo 100x = sem alvo fixo (sai pelo stop ou pelo cruzamento contrário).
GRID_TREND = [(2.0, 3.0), (2.0, 100.0), (3.0, 3.0), (3.0, 100.0)]
MIN_TRADES_TO_SELECT = 5


@dataclass
class BacktestResult:
    params: dict
    start_ms: int
    end_ms: int
    candles: int
    trades: list[dict]
    equity: list[tuple[int, float]]
    metrics: dict


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def precompute_signals(analyzer: Analyzer) -> list[Signal]:
    """Sinal de cada candle usando apenas dados até ele (o teste de vazamento confere isso)."""
    return [analyzer.signal(i) for i in range(len(analyzer))]


def simulate(analyzer: Analyzer, signals: list[Signal], params: TradingParams,
             start_ms: int, end_ms: int) -> BacktestResult:
    df = analyzer.ind5
    times = analyzer.close_time5
    idx = np.flatnonzero((times >= start_ms) & (times <= end_ms))
    if len(idx) == 0:
        raise ValueError("Período de teste sem candles.")
    rows = df[["open_time", "open", "high", "low", "close", "volume", "close_time"]].to_numpy()
    trader = PaperTrader(params, TraderState.new(params.initial_balance))
    trades: list[dict] = []
    equity: list[tuple[int, float]] = []
    halts = 0
    last_i = int(idx[-1])
    for i in idx:
        i = int(i)
        r = rows[i]
        candle = Candle(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]), int(r[6]))
        signal_fn = (lambda i=i: signals[i]) if i < last_i else None  # o último sinal não teria execução
        for ev in trader.process_candle(candle, signal_fn):
            if ev["type"] == "closed":
                trades.append(ev["trade"])
            elif ev["type"] == "halted":
                halts += 1
        equity.append((candle.close_time, trader.equity(candle.close)))
    if trader.s.position is not None:
        last_close = float(rows[last_i][4])
        for ev in trader.close_now(last_close, int(rows[last_i][6]), "Fim do período de teste"):
            trades.append(ev["trade"])
        equity[-1] = (equity[-1][0], trader.equity(last_close))

    final = equity[-1][1]
    dd_abs, dd_pct = max_drawdown([params.initial_balance] + [e for _, e in equity])
    first_open, last_close = float(rows[int(idx[0])][1]), float(rows[last_i][4])
    metrics = trade_stats(trades) | {
        "initial_balance": params.initial_balance,
        "final_equity": final,
        "return_pct": (final / params.initial_balance - 1) * 100,
        "max_drawdown_pct": dd_pct * 100,
        "max_drawdown_abs": dd_abs,
        "daily_halts": halts,
        "buy_hold_pct": (last_close / first_open - 1) * 100,
        "fees_paid": trader.s.fees_paid,
    }
    return BacktestResult(asdict(params), int(times[idx[0]]), int(times[last_i]), len(idx),
                          trades, equity, metrics)


def _downsample(equity: list[tuple[int, float]], points: int = 300) -> list[dict]:
    step = max(1, len(equity) // points)
    sampled = equity[::step]
    if sampled[-1] != equity[-1]:
        sampled.append(equity[-1])
    return [{"time": t // 1000, "value": round(v, 2)} for t, v in sampled]


def _summary(result: BacktestResult) -> dict:
    return {
        "inicio": iso(result.start_ms),
        "fim": iso(result.end_ms),
        "candles": result.candles,
        "parametros": {"atr_stop_mult": result.params["atr_stop_mult"],
                       "take_profit_r": result.params["take_profit_r"]},
        "metricas": result.metrics,
        "curva": _downsample(result.equity),
        "operacoes": result.trades[-50:],
    }


def walk_forward(df5: pd.DataFrame | None, df15: pd.DataFrame | None, base: TradingParams,
                 split: float = 0.7, grid=GRID, analysis=None) -> dict:
    """`analysis` (opcional) é a análise já montada de outra estratégia; sem ela, usa as regras 5m + 15m."""
    t0 = time.time()
    analyzer = analysis if analysis is not None else Analyzer(df5, df15)
    signals = precompute_signals(analyzer)
    usable = [i for i, s in enumerate(signals) if s.data_ok]
    if len(usable) < 50:
        raise ValueError("Dados insuficientes para o backtest (menos de 50 candles analisáveis).")
    start_ms = int(analyzer.close_time5[usable[0]])
    end_ms = int(analyzer.close_time5[-1])
    split_ms = int(start_ms + split * (end_ms - start_ms))

    in_sample = []
    for atr_mult, tp_r in grid:
        params = replace(base, atr_stop_mult=atr_mult, take_profit_r=tp_r)
        res = simulate(analyzer, signals, params, start_ms, split_ms - 1)
        in_sample.append((params, res))

    eligible = [(p, r) for p, r in in_sample if r.metrics["trades"] >= MIN_TRADES_TO_SELECT]
    notes = []
    if eligible:
        chosen, chosen_is = max(eligible, key=lambda pr: pr[1].metrics["return_pct"])
        if all(r.metrics["return_pct"] <= 0 for _, r in in_sample):
            notes.append(f"Todas as {len(in_sample)} combinações perderam dinheiro na parte 1: o parâmetro "
                         "'escolhido' é só o menos ruim e não há evidência de vantagem.")
    else:
        chosen = base
        chosen_is = simulate(analyzer, signals, base, start_ms, split_ms - 1)
        notes.append(f"Menos de {MIN_TRADES_TO_SELECT} operações por combinação na parte 1: "
                     "mantidos os parâmetros padrão (sem otimização).")

    oos_chosen = simulate(analyzer, signals, chosen, split_ms, end_ms)
    oos_default = simulate(analyzer, signals, base, split_ms, end_ms)
    # Diagnóstico (não usado em nenhuma escolha): quanto os custos pesaram na parte 2.
    oos_free = simulate(analyzer, signals, replace(chosen, fee_rate=0.0, slippage_rate=0.0), split_ms, end_ms)

    counts = {a: 0 for a in (BUY, SELL, WAIT)}
    for s in signals:
        counts[s.action] += 1
    insufficient = sum(1 for s in signals if not s.data_ok)
    total_oos = oos_chosen.metrics["trades"]
    if total_oos < 30:
        notes.append(f"Apenas {total_oos} operações fora da amostra: amostra pequena, "
                     "resultado pouco confiável estatisticamente.")
    notes.append(f"Os parâmetros escolhidos aqui (stop {chosen.atr_stop_mult}× ATR, alvo "
                 f"{_fmt_target(chosen.take_profit_r)}) só aparecem no relatório: o robô ao vivo usa "
                 f"ATR_STOP_MULT e TAKE_PROFIT_R da configuração (hoje stop {base.atr_stop_mult}× ATR, alvo "
                 f"{_fmt_target(base.take_profit_r)}).")
    notes.append("Resultados passados não garantem resultados futuros.")

    report = {
        "gerado_em": iso(int(time.time() * 1000)),
        "periodo": {"inicio": iso(int(analyzer.close_time5[0])), "fim": iso(end_ms),
                    "inicio_analise": iso(start_ms), "divisao": iso(split_ms)},
        "custos": {"taxa_por_execucao_pct": base.fee_rate * 100, "slippage_pct": base.slippage_rate * 100},
        "regras_de_risco": {"risco_por_operacao_pct": base.risk_per_trade * 100,
                            "perda_diaria_max_pct": base.daily_loss_limit * 100,
                            "saldo_inicial": base.initial_balance},
        "sinais": {"compra": counts[BUY], "venda": counts[SELL], "aguardar": counts[WAIT],
                   "dados_insuficientes": insufficient},
        "dentro_da_amostra": [
            {"atr_stop_mult": p.atr_stop_mult, "take_profit_r": p.take_profit_r,
             "operacoes": r.metrics["trades"], "retorno_pct": r.metrics["return_pct"],
             "taxa_acerto": r.metrics["win_rate"], "drawdown_pct": r.metrics["max_drawdown_pct"]}
            for p, r in in_sample
        ],
        "escolhido": {"atr_stop_mult": chosen.atr_stop_mult, "take_profit_r": chosen.take_profit_r},
        "parte1_escolhido": _summary(chosen_is),
        "parte2_fora_da_amostra": _summary(oos_chosen),
        "parte2_parametros_padrao": _summary(oos_default),
        "parte2_sem_custos": {"retorno_pct": oos_free.metrics["return_pct"],
                              "taxa_acerto": oos_free.metrics["win_rate"],
                              "operacoes": oos_free.metrics["trades"]},
        "avisos": notes,
        "duracao_s": round(time.time() - t0, 1),
    }
    return json_safe(report)


def fetch_history(settings: Settings, days: float, market: MarketData | None = None):
    market = market or MarketData(settings.data_providers, settings.symbol)
    end_ms = int(time.time() * 1000)
    # Aquecimento dos indicadores: 110 candles do maior tempo gráfico (mínimo 2 dias)
    warmup_ms = max(2 * 86_400_000, 110 * max(interval_ms(i) for i in settings.intervals))
    start_ms = end_ms - int(days * 86_400_000) - warmup_ms
    provider, frames = market.fetch_history(list(settings.intervals), start_ms, end_ms)
    # Remove o candle que ainda está em andamento
    for k, df in frames.items():
        frames[k] = df[df["close_time"] < end_ms].reset_index(drop=True)
    return provider, frames


def run_full_backtest(settings: Settings, days: float = 30, split: float = 0.7,
                      market: MarketData | None = None) -> dict:
    provider, frames = fetch_history(settings, days, market)
    return backtest_frames(settings, frames, split) | {"fonte_de_dados": provider, "dias": days}


def grid_for(settings: Settings):
    return GRID_TREND if settings.trend_interval is None else GRID


def backtest_frames(settings: Settings, frames: dict[str, pd.DataFrame], split: float = 0.7) -> dict:
    """Backtest walk-forward da estratégia configurada (a mesma que o robô ao vivo usa)."""
    report = walk_forward(None, None, settings.trading, split, grid_for(settings),
                          analysis=build_analysis(settings, frames))
    report["estrategia"] = settings.strategy_info.name
    report["candles"] = {k: len(v) for k, v in frames.items()}
    return report


def _fmt_pct(x) -> str:
    return "—" if x is None else f"{x:+.2f}%"


def _fmt_target(r: float) -> str:
    return "sem alvo fixo" if r >= NO_TARGET_R else f"{r}× o risco"


def _fmt_min(x) -> str:
    return "—" if x is None else f"{x:.0f} min"


def _fmt_reasons(reasons: dict) -> str:
    return " · ".join(f"{k}: {v}" for k, v in sorted(reasons.items(), key=lambda kv: -kv[1])) or "—"


def format_markdown(report: dict) -> str:
    def block(title: str, part: dict) -> list[str]:
        m = part["metricas"]
        wr = "—" if m["win_rate"] is None else f"{m['win_rate']:.1f}%"
        pf = "—" if m["profit_factor"] is None else f"{m['profit_factor']:.2f}"
        return [
            f"### {title}",
            f"Período: {part['inicio']} → {part['fim']} ({part['candles']} candles de 5m)  ",
            f"Parâmetros: stop = {part['parametros']['atr_stop_mult']}× ATR, "
            f"alvo = {_fmt_target(part['parametros']['take_profit_r'])}",
            "",
            "| Métrica | Valor |",
            "|---|---|",
            f"| Saldo final | US$ {m['final_equity']:.2f} (início US$ {m['initial_balance']:.2f}) |",
            f"| Retorno | {_fmt_pct(m['return_pct'])} |",
            f"| Comprar e segurar BTC no mesmo período | {_fmt_pct(m['buy_hold_pct'])} |",
            f"| Operações | {m['trades']} ({m['wins']} ganhos / {m['losses']} perdas) |",
            f"| Taxa de acerto | {wr} |",
            f"| Fator de lucro | {pf} |",
            f"| Drawdown máximo | {m['max_drawdown_pct']:.2f}% (US$ {m['max_drawdown_abs']:.2f}) |",
            f"| Taxas pagas | US$ {m['fees_paid']:.2f} |",
            f"| Resultado antes de taxas e slippage | US$ {m['gross_pnl']:.2f} |",
            f"| Custos totais (taxas + slippage) | US$ {m['costs']:.2f} |",
            f"| Tempo médio por operação | {_fmt_min(m['avg_holding_min'])} |",
            f"| Motivos de saída | {_fmt_reasons(m['exit_reasons'])} |",
            f"| Dias interrompidos pela trava de 3% | {m['daily_halts']} |",
            "",
        ]

    s = report["sinais"]
    lines = [
        "## Resultado do backtest (dados reais, dinheiro fictício)",
        "",
        f"Estratégia: **{report.get('estrategia', 'Regras 5m + 15m')}** · "
        f"Fonte: **{report.get('fonte_de_dados', '?')}** · Período: {report['periodo']['inicio']} → "
        f"{report['periodo']['fim']} · Divisão treino/teste: {report['periodo']['divisao']}",
        f"Custos simulados: taxa {report['custos']['taxa_por_execucao_pct']:.2f}% por execução + "
        f"slippage {report['custos']['slippage_pct']:.2f}%",
        f"Sinais gerados: {s['compra']} COMPRA · {s['venda']} VENDA · {s['aguardar']} AGUARDAR "
        f"({s['dados_insuficientes']} por dados insuficientes)",
        "",
    ]
    lines += block("Parte 2 — FORA da amostra (dados novos, o teste mais importante)",
                   report["parte2_fora_da_amostra"])
    free = report["parte2_sem_custos"]
    lines += [f"_Diagnóstico: sem taxas nem slippage, a parte 2 teria dado {_fmt_pct(free['retorno_pct'])} "
              f"({free['operacoes']} operações). Isso mostra quanto os custos pesam; não é um resultado possível._",
              ""]
    lines += block("Parte 2 — fora da amostra com parâmetros padrão", report["parte2_parametros_padrao"])
    lines += block("Parte 1 — dentro da amostra (usada para escolher parâmetros)", report["parte1_escolhido"])
    lines += ["### Combinações testadas na parte 1", "", "| Stop (×ATR) | Alvo | Operações | Retorno | Acerto |",
              "|---|---|---|---|---|"]
    for row in report["dentro_da_amostra"]:
        wr = "—" if row["taxa_acerto"] is None else f"{row['taxa_acerto']:.0f}%"
        lines.append(f"| {row['atr_stop_mult']} | {_fmt_target(row['take_profit_r'])} | {row['operacoes']} | "
                     f"{_fmt_pct(row['retorno_pct'])} | {wr} |")
    lines += ["", "### Avisos"] + [f"- {n}" for n in report["avisos"]]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backtest walk-forward do robô BTC/USDT (simulado)")
    parser.add_argument("--days", type=float, default=30, help="dias de histórico (padrão 30)")
    parser.add_argument("--split", type=float, default=0.7, help="fração para a parte 1 (padrão 0.7)")
    parser.add_argument("--json", type=Path, help="salvar relatório completo em JSON")
    parser.add_argument("--markdown", type=Path, help="salvar resumo em Markdown")
    parser.add_argument("--csv-dir", type=Path, help="usar/salvar candles em CSV nesta pasta")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    settings = load_settings()
    csvs = {i: args.csv_dir / f"btcusdt_{i}.csv" for i in settings.intervals} if args.csv_dir else {}
    if csvs and all(p.exists() for p in csvs.values()):
        frames = {i: pd.read_csv(p) for i, p in csvs.items()}
        provider = "arquivo CSV"
    else:
        try:
            provider, frames = fetch_history(settings, args.days)
        except Exception as exc:  # noqa: BLE001
            print(f"ERRO: não foi possível baixar dados históricos: {exc}", file=sys.stderr)
            return 2
        if args.csv_dir:
            args.csv_dir.mkdir(parents=True, exist_ok=True)
            for i, df in frames.items():
                df.to_csv(csvs[i], index=False)
    print(f"Estratégia: {settings.strategy_info.name} · Dados: {provider} — "
          + ", ".join(f"{len(df)} candles de {i}" for i, df in frames.items()))
    report = backtest_frames(settings, frames, args.split)
    report["fonte_de_dados"] = provider
    md = format_markdown(report)
    print(md)
    if args.json:
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    if args.markdown:
        args.markdown.write_text(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
