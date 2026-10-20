"""Corretora SIMULADA (paper trading). Nenhuma ordem sai deste arquivo.

Como as ordens simuladas funcionam (iguais no robô ao vivo e no backtest):
- O sinal é calculado no FECHAMENTO de um candle de 5m.
- A ordem é executada na ABERTURA do candle seguinte (primeiro preço
  disponível depois da decisão), com slippage e taxa.
- Stop-loss e alvo funcionam como ordens deixadas na corretora: são
  verificados com a máxima/mínima de cada candle. Se os dois forem
  tocados no mesmo candle, assume-se o pior caso (stop).
- Risco por operação: no máximo 1% do saldo (perda no stop já incluindo
  taxas e slippage). A posição também nunca passa do dinheiro disponível.
- Se a perda do dia chegar a 3% do saldo inicial, a posição é fechada e o
  robô não abre novas operações até o dia seguinte.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import TradingParams
from .strategy import BUY, SELL, Signal


@dataclass
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int

    @classmethod
    def from_row(cls, row) -> "Candle":
        return cls(int(row["open_time"]), float(row["open"]), float(row["high"]), float(row["low"]),
                   float(row["close"]), float(row["volume"]), int(row["close_time"]))


@dataclass
class Position:
    trade_id: str
    entry_time: int
    entry_price: float
    qty: float
    stop: float
    take: float
    entry_fee: float
    risk_amount: float
    signal_time: int
    reason: str


@dataclass
class PendingOrder:
    side: str  # "BUY" ou "SELL"
    signal_time: int
    reason: str
    atr: float


@dataclass
class TraderState:
    initial_balance: float
    cash: float
    position: Position | None = None
    pending: PendingOrder | None = None
    day: str | None = None
    day_start_equity: float = 0.0
    day_realized: float = 0.0
    day_trades: int = 0
    halted_day: str | None = None
    paused: bool = False
    peak_equity: float = 0.0
    max_drawdown_pct: float = 0.0
    max_drawdown_abs: float = 0.0
    last_candle_close: int | None = None
    last_price: float = 0.0
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    closed_trades: int = 0
    wins: int = 0
    last_signal_action: str | None = None

    @classmethod
    def new(cls, initial_balance: float) -> "TraderState":
        return cls(initial_balance=initial_balance, cash=initial_balance, peak_equity=initial_balance)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "TraderState":
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in data.items() if k in known}
        if data.get("position"):
            data["position"] = Position(**data["position"])
        if data.get("pending"):
            data["pending"] = PendingOrder(**data["pending"])
        return cls(**data)


class PaperTrader:
    def __init__(self, params: TradingParams, state: TraderState):
        self.p = params
        self.s = state
        self.tz = ZoneInfo(params.timezone)

    # ----------------------------------------------------------------- util
    def day_of(self, ms: int) -> str:
        return datetime.fromtimestamp(ms / 1000, tz=self.tz).date().isoformat()

    def equity(self, price: float | None = None) -> float:
        price = self.s.last_price if price is None else price
        pos = self.s.position
        return self.s.cash + (pos.qty * price if pos else 0.0)

    @property
    def halted(self) -> bool:
        return self.s.halted_day is not None and self.s.halted_day == self.s.day

    @property
    def daily_loss_cap(self) -> float:
        return self.p.daily_loss_limit * self.s.initial_balance

    # ------------------------------------------------------------ fluxo
    def start_candle(self, c: Candle) -> list[dict]:
        """Troca de dia (no fuso configurado): zera a contagem diária."""
        events: list[dict] = []
        day = self.day_of(c.open_time)
        if self.s.day == day:
            return events
        if self.s.day is not None:
            events.append({"type": "day_end", "time": c.open_time, "day": self.s.day,
                           "pnl": self.equity(c.open) - self.s.day_start_equity,
                           "realized": self.s.day_realized, "trades": self.s.day_trades,
                           "equity": self.equity(c.open)})
        self.s.day = day
        self.s.day_start_equity = self.equity(c.open)
        self.s.day_realized = 0.0
        self.s.day_trades = 0
        if self.s.halted_day is not None and self.s.halted_day != day:
            self.s.halted_day = None
            events.append({"type": "resumed", "time": c.open_time,
                           "message": "Novo dia: limite diário zerado, robô liberado."})
        return events

    def fill_pending(self, price: float, time_ms: int) -> list[dict]:
        order = self.s.pending
        if order is None:
            return []
        self.s.pending = None
        if order.side == "BUY":
            return self._open(price, time_ms, order)
        if order.side == "SELL" and self.s.position:
            return [self._close(price, time_ms, "Sinal de VENDA")]
        return []

    def check_exits(self, c: Candle) -> list[dict]:
        pos = self.s.position
        if pos is None or pos.entry_time > c.open_time:
            return []
        if c.open <= pos.stop:
            return [self._close(c.open, c.open_time, "Stop-loss (preço abriu abaixo do stop)")]
        if c.low <= pos.stop:
            return [self._close(pos.stop, c.close_time, "Stop-loss")]
        if c.open >= pos.take:
            return [self._close(c.open, c.open_time, "Alvo atingido (take-profit)")]
        if c.high >= pos.take:
            return [self._close(pos.take, c.close_time, "Alvo atingido (take-profit)")]
        return []

    def end_candle(self, c: Candle) -> list[dict]:
        """Marca o saldo no fechamento, mede drawdown e aplica a trava diária."""
        events: list[dict] = []
        self.s.last_price = c.close
        eq = self.equity(c.close)
        loss_today = self.s.day_start_equity - eq
        if not self.halted and loss_today >= self.daily_loss_cap - 1e-9:
            if self.s.position:
                events.append(self._close(c.close, c.close_time, "Limite de perda diária atingido"))
            self.s.pending = None
            self.s.halted_day = self.s.day
            eq = self.equity(c.close)
            events.append({"type": "halted", "time": c.close_time, "loss": self.s.day_start_equity - eq,
                           "message": f"Perda do dia chegou a US$ {self.s.day_start_equity - eq:.2f} "
                                      f"(limite US$ {self.daily_loss_cap:.2f}). Sem novas operações até amanhã."})
        self.s.peak_equity = max(self.s.peak_equity, eq)
        dd_abs = self.s.peak_equity - eq
        dd_pct = dd_abs / self.s.peak_equity if self.s.peak_equity > 0 else 0.0
        self.s.max_drawdown_abs = max(self.s.max_drawdown_abs, dd_abs)
        self.s.max_drawdown_pct = max(self.s.max_drawdown_pct, dd_pct)
        return events

    def on_signal(self, signal: Signal) -> list[dict]:
        """Cria a ordem pendente (executada na abertura do próximo candle)."""
        if signal.action == BUY and self.s.position is None and self.s.pending is None:
            if self.halted or self.s.paused:
                return []
            self.s.pending = PendingOrder("BUY", signal.time_ms, signal.summary, signal.atr)
        elif signal.action == SELL and self.s.position is not None and self.s.pending is None:
            self.s.pending = PendingOrder("SELL", signal.time_ms, signal.summary, signal.atr)
        return []

    def process_candle(self, c: Candle, signal_fn=None) -> list[dict]:
        """Processa um candle FECHADO, na mesma ordem no robô e no backtest."""
        events = self.start_candle(c)
        pending = self.s.pending
        if pending is not None:
            if pending.signal_time == c.open_time - 1:
                events += self.fill_pending(c.open, c.open_time)
            else:
                self.s.pending = None
                events.append({"type": "cancelled", "time": c.open_time,
                               "message": "Ordem pendente expirada (dados atrasados ou robô fora do ar)."})
        events += self.check_exits(c)
        events += self.end_candle(c)
        if signal_fn is not None:
            events += self.on_signal(signal_fn())
        self.s.last_candle_close = c.close_time
        return events

    def close_now(self, price: float, time_ms: int, reason: str) -> list[dict]:
        self.s.pending = None
        if self.s.position is None:
            return []
        return [self._close(price, time_ms, reason)]

    # ------------------------------------------------------------- ordens
    def _skip(self, time_ms: int, why: str) -> list[dict]:
        return [{"type": "skipped", "time": time_ms, "message": f"Compra não executada: {why}"}]

    def _open(self, price: float, time_ms: int, order: PendingOrder) -> list[dict]:
        p, s = self.p, self.s
        if s.position is not None:
            return self._skip(time_ms, "já existe uma posição aberta.")
        if self.halted:
            return self._skip(time_ms, "limite de perda diária atingido.")
        if s.paused:
            return self._skip(time_ms, "robô pausado.")
        fill = price * (1 + p.slippage_rate)
        stop = fill - p.atr_stop_mult * order.atr
        if stop <= 0 or order.atr <= 0:
            return self._skip(time_ms, "stop-loss inválido.")
        take = fill + p.take_profit_r * (fill - stop)
        equity = self.equity(price)
        risk_budget = equity * p.risk_per_trade
        daily_room = self.daily_loss_cap - (s.day_start_equity - equity)
        risk_amount = min(risk_budget, daily_room)
        if risk_amount <= 0.5:
            # Não sobra espaço para arriscar sem ultrapassar a perda máxima do dia.
            s.halted_day = s.day
            loss = s.day_start_equity - equity
            return [{"type": "halted", "time": time_ms, "loss": loss,
                     "message": f"Perda do dia em US$ {loss:.2f}, praticamente no limite de "
                                f"US$ {self.daily_loss_cap:.2f}. Sem novas operações até amanhã."}]
        stop_fill = stop * (1 - p.slippage_rate)
        loss_per_unit = (fill - stop_fill) + p.fee_rate * fill + p.fee_rate * stop_fill
        qty = min(risk_amount / loss_per_unit, s.cash / (fill * (1 + p.fee_rate)))
        notional = qty * fill
        if notional < p.min_notional:
            return self._skip(time_ms, f"valor da ordem (US$ {notional:.2f}) abaixo do mínimo.")
        fee = notional * p.fee_rate
        s.cash -= notional + fee
        s.fees_paid += fee
        s.position = Position(
            trade_id=f"T{order.signal_time}", entry_time=time_ms, entry_price=fill, qty=qty,
            stop=stop, take=take, entry_fee=fee, risk_amount=qty * loss_per_unit,
            signal_time=order.signal_time, reason=order.reason,
        )
        return [{"type": "opened", "time": time_ms, "trade": asdict(s.position),
                 "equity": self.equity(price)}]

    def _close(self, price: float, time_ms: int, reason: str) -> dict:
        p, s = self.p, self.s
        pos = s.position
        assert pos is not None
        fill = price * (1 - p.slippage_rate)
        proceeds = pos.qty * fill
        fee = proceeds * p.fee_rate
        cost = pos.qty * pos.entry_price + pos.entry_fee
        pnl = proceeds - fee - cost
        s.cash += proceeds - fee
        s.fees_paid += fee
        s.realized_pnl += pnl
        s.day_realized += pnl
        s.day_trades += 1
        s.closed_trades += 1
        s.wins += int(pnl > 0)
        s.position = None
        raw_entry = pos.entry_price / (1 + p.slippage_rate)  # preço antes do slippage
        trade = asdict(pos) | {
            "exit_time": time_ms, "exit_price": fill, "exit_fee": fee, "exit_reason": reason,
            "pnl": pnl, "pnl_pct": pnl / cost * 100 if cost else 0.0,
            "r_multiple": pnl / pos.risk_amount if pos.risk_amount else 0.0,
            "gross_pnl": pos.qty * (price - raw_entry),  # resultado se não houvesse taxas nem slippage
        }
        return {"type": "closed", "time": time_ms, "trade": trade, "equity": self.equity(price)}
