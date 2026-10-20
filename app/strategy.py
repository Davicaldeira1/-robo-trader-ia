"""Regras de decisão: COMPRA, VENDA ou AGUARDAR.

Entrada no gráfico de 5 minutos, confirmada pela tendência do gráfico de
15 minutos. Opera apenas "comprado" (como no mercado à vista): VENDA
significa encerrar a posição ou, sem posição, ficar de fora.

COMPRA exige TODAS as condições:
  1. Tendência de alta no 15m: EMA20 > EMA50
  2. Preço do 15m acima da EMA50
  3. EMA9 > EMA21 no 5m
  4. MACD (histograma) positivo e subindo no 5m
  5. RSI entre 50 e 70 no 5m (força sem estar "esticado")
  6. Volume do candle >= média dos 20 anteriores

VENDA se QUALQUER grupo for verdadeiro:
  A. EMA9 < EMA21 E MACD negativo no 5m (momento de baixa)
  B. Tendência de baixa no 15m: EMA20 < EMA50 E preço abaixo da EMA50

Caso contrário: AGUARDAR. Se faltar dado (poucos candles, buracos na
série, dados atrasados ou indicador incalculável) a resposta é sempre
AGUARDAR — o robô não inventa sinal.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

BUY, SELL, WAIT = "COMPRA", "VENDA", "AGUARDAR"

MIN_CANDLES_5M = 100
MIN_CANDLES_15M = 100
MAX_STALE_MS = 2 * 300_000  # dados com mais de 10 min de atraso são recusados
RSI_MIN, RSI_MAX = 50.0, 70.0
VOLUME_MIN_RATIO = 1.0


@dataclass
class Snapshot:
    """Tudo o que a estratégia pode ver no fechamento de um candle de 5m."""

    time_ms: int  # horário de fechamento do candle de 5m analisado
    price: float
    n5: int
    n15: int
    gaps5: int
    gaps15: int
    stale_ms: int
    ema_fast: float
    ema_slow: float
    rsi: float
    macd_hist: float
    macd_hist_prev: float
    vol_ratio: float
    atr: float
    close15: float
    ema20_15: float
    ema50_15: float
    time15_ms: int  # fechamento do candle de 15m usado (sempre <= time_ms)


@dataclass
class Check:
    label: str
    ok: bool
    value: str


@dataclass
class Signal:
    action: str
    summary: str
    reasons: list[str]
    time_ms: int
    price: float
    atr: float
    data_ok: bool
    buy_checks: list[Check] = field(default_factory=list)
    sell_checks: list[Check] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _fmt(x: float, nd: int = 2) -> str:
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:,.{nd}f}"


def evaluate(s: Snapshot) -> Signal:
    problems = []
    if s.n5 < MIN_CANDLES_5M:
        problems.append(f"Só há {s.n5} candles de 5m (mínimo {MIN_CANDLES_5M}).")
    if s.n15 < MIN_CANDLES_15M:
        problems.append(f"Só há {s.n15} candles de 15m (mínimo {MIN_CANDLES_15M}).")
    if s.gaps5 > 0 or s.gaps15 > 0:
        problems.append(f"Faltam candles na série recente (5m: {s.gaps5}, 15m: {s.gaps15}).")
    if s.stale_ms > MAX_STALE_MS:
        problems.append(f"Dados atrasados em {s.stale_ms // 60000} min.")
    if s.time15_ms > s.time_ms:
        problems.append("Candle de 15m posterior ao momento da análise (bloqueado).")
    required = [s.price, s.ema_fast, s.ema_slow, s.rsi, s.macd_hist, s.macd_hist_prev,
                s.vol_ratio, s.atr, s.close15, s.ema20_15, s.ema50_15]
    if any(x is None or math.isnan(x) for x in required):
        problems.append("Indicadores ainda não podem ser calculados com os dados disponíveis.")
    if not problems and s.atr <= 0:
        problems.append("Volatilidade (ATR) zerada: dados suspeitos.")
    if problems:
        return Signal(WAIT, "Dados insuficientes — nenhum sinal gerado.", problems,
                      s.time_ms, s.price, s.atr, data_ok=False)

    buy_checks = [
        Check("Tendência de alta no 15m (EMA20 > EMA50)", s.ema20_15 > s.ema50_15,
              f"{_fmt(s.ema20_15)} vs {_fmt(s.ema50_15)}"),
        Check("Preço do 15m acima da EMA50", s.close15 > s.ema50_15,
              f"{_fmt(s.close15)} vs {_fmt(s.ema50_15)}"),
        Check("EMA9 acima da EMA21 no 5m", s.ema_fast > s.ema_slow,
              f"{_fmt(s.ema_fast)} vs {_fmt(s.ema_slow)}"),
        Check("MACD positivo e subindo no 5m", s.macd_hist > 0 and s.macd_hist > s.macd_hist_prev,
              f"{_fmt(s.macd_hist)} (antes {_fmt(s.macd_hist_prev)})"),
        Check(f"RSI entre {RSI_MIN:.0f} e {RSI_MAX:.0f} no 5m", RSI_MIN <= s.rsi <= RSI_MAX, _fmt(s.rsi, 1)),
        Check("Volume acima da média de 20 candles", s.vol_ratio >= VOLUME_MIN_RATIO,
              f"{_fmt(s.vol_ratio)}x a média"),
    ]
    momentum_down = s.ema_fast < s.ema_slow and s.macd_hist < 0
    trend_down = s.ema20_15 < s.ema50_15 and s.close15 < s.ema50_15
    sell_checks = [
        Check("Momento de baixa no 5m (EMA9 < EMA21 e MACD negativo)", momentum_down,
              f"EMA {_fmt(s.ema_fast)}/{_fmt(s.ema_slow)}, MACD {_fmt(s.macd_hist)}"),
        Check("Tendência de baixa no 15m (EMA20 < EMA50 e preço abaixo da EMA50)", trend_down,
              f"EMA {_fmt(s.ema20_15)}/{_fmt(s.ema50_15)}, preço {_fmt(s.close15)}"),
    ]

    if all(c.ok for c in buy_checks):
        return Signal(BUY, "Todas as 6 condições de compra foram atendidas.",
                      [c.label for c in buy_checks], s.time_ms, s.price, s.atr, True,
                      buy_checks, sell_checks)
    if momentum_down or trend_down:
        reasons = [c.label for c in sell_checks if c.ok]
        return Signal(SELL, "Condições de venda presentes: " + "; ".join(reasons) + ".",
                      reasons, s.time_ms, s.price, s.atr, True, buy_checks, sell_checks)
    missing = [c.label for c in buy_checks if not c.ok]
    return Signal(WAIT, f"Faltam {len(missing)} de 6 condições para comprar.", missing,
                  s.time_ms, s.price, s.atr, True, buy_checks, sell_checks)
