"""Motor do robô ao vivo (sempre em modo simulado).

A cada ciclo (padrão: 20 s):
1. Busca candles de 5m e 15m numa fonte pública (com fontes reservas).
2. Para cada candle de 5m que FECHOU desde o último ciclo: executa ordens
   pendentes, verifica stop/alvo, aplica a trava diária e calcula o sinal.
3. Grava tudo numa única transação e coloca os alertas na fila do Telegram.

Recuperação de falhas:
- Falha de dados: o ciclo é pulado, o erro é registrado e o robô tenta de
  novo no próximo ciclo (sem operar com dados ruins).
- Robô reiniciado: o estado é lido do banco e o processamento continua do
  último candle gravado, sem repetir operações.
- Robô ficou fora do ar por muito tempo: os candles perdidos servem só para
  checar stop/alvo (como ordens que estariam na corretora); nenhuma compra
  é feita com sinais antigos.
"""
from __future__ import annotations

import copy
import logging
import threading

import numpy as np

from .analysis import Analyzer
from .config import Settings
from .market_data import MarketData
from .metrics import trade_stats
from .notifier import fmt_time, money
from .storage import Store, now_ms
from .strategy import BUY, SELL, Signal
from .trader import Candle, PaperTrader, TraderState

log = logging.getLogger(__name__)

MAX_CATCHUP = 3  # mais candles novos que isso = robô ficou fora do ar
STATE_KEY = "trader_state"


def describe_event(ev: dict, tz: str) -> tuple[str, str, str | None]:
    """Converte um evento da corretora simulada em (nível, texto do log, texto do Telegram)."""
    kind = ev["type"]
    if kind == "opened":
        t = ev["trade"]
        notional = t["qty"] * t["entry_price"]
        text = (f"🟢 COMPRA simulada BTC/USDT\n"
                f"Preço: {money(t['entry_price'])}\n"
                f"Quantidade: {t['qty']:.6f} BTC ({money(notional)})\n"
                f"Stop-loss: {money(t['stop'])} | Alvo: {money(t['take'])}\n"
                f"Risco máximo: {money(t['risk_amount'])}\n"
                f"Motivo: {t['reason']}\n"
                f"Horário: {fmt_time(ev['time'], tz)}\n"
                f"(PAPER TRADING — dinheiro fictício)")
        return "info", f"Compra simulada a {money(t['entry_price'])} ({t['qty']:.6f} BTC)", text
    if kind == "closed":
        t = ev["trade"]
        emoji = "✅" if t["pnl"] > 0 else "🔴"
        text = (f"{emoji} VENDA simulada BTC/USDT — {t['exit_reason']}\n"
                f"Entrada: {money(t['entry_price'])} → Saída: {money(t['exit_price'])}\n"
                f"Resultado: {money(t['pnl'])} ({t['pnl_pct']:+.2f}%)\n"
                f"Saldo fictício: {money(ev['equity'])}\n"
                f"Horário: {fmt_time(ev['time'], tz)}")
        return "info", f"Venda simulada ({t['exit_reason']}): resultado {money(t['pnl'])}", text
    if kind == "halted":
        return "aviso", ev["message"], f"⛔ Limite diário atingido. {ev['message']}"
    if kind == "resumed":
        return "info", ev["message"], f"🔓 {ev['message']}"
    if kind == "day_end":
        text = (f"📅 Resumo do dia {ev['day']}\n"
                f"Resultado: {money(ev['pnl'])} | Operações fechadas: {ev['trades']}\n"
                f"Saldo fictício: {money(ev['equity'])}")
        return "info", f"Fim do dia {ev['day']}: {money(ev['pnl'])}", text
    if kind in ("skipped", "cancelled"):
        return "aviso", ev["message"], None
    return "info", str(ev), None


def signal_alert(sig: Signal, paused: bool, tz: str) -> str:
    icon = "📈" if sig.action == BUY else "📉"
    lines = [f"{icon} Sinal de {sig.action} — BTC/USDT (5m)",
             f"Preço: {money(sig.price)}",
             f"Motivo: {sig.summary}"]
    lines += [f"• {r}" for r in sig.reasons]
    if sig.action == SELL:
        lines.append("(O robô só opera comprado: VENDA = encerrar posição ou ficar de fora.)")
    if paused:
        lines.append("⏸️ Robô pausado: nenhuma operação será aberta.")
    lines.append(f"Horário: {fmt_time(sig.time_ms, tz)}")
    return "\n".join(lines)


class Engine:
    def __init__(self, settings: Settings, store: Store, market: MarketData, clock=None):
        self.settings = settings
        self.params = settings.trading
        self.store = store
        self.market = market
        self.clock = clock or now_ms
        self.lock = threading.RLock()
        self.consecutive_failures = 0
        self.last_tick_ms: int | None = None
        self.last_success_ms: int | None = None
        self.last_error: str | None = None
        self.provider: str | None = None
        self.analyzer: Analyzer | None = None
        self.in_progress: Candle | None = None
        self.last_signal: Signal | None = None
        self.state = self._load_state()

    # ---------------------------------------------------------- estado
    def _load_state(self) -> TraderState:
        data = self.store.get_kv(STATE_KEY)
        if data:
            log.info("Estado restaurado do banco de dados.")
            return TraderState.from_dict(data)
        state = TraderState.new(self.params.initial_balance)
        with self.store.transaction() as conn:
            self.store.set_kv(conn, STATE_KEY, state.to_dict())
            self.store.add_event(conn, "info", f"Simulação criada com saldo fictício de "
                                               f"{money(state.initial_balance)}.")
        return state

    def _record(self, conn, events: list[dict]) -> None:
        tz = self.params.timezone
        for ev in events:
            if ev["type"] == "opened":
                self.store.save_trade(conn, ev["trade"], "open")
            elif ev["type"] == "closed":
                self.store.save_trade(conn, ev["trade"], "closed")
            level, text, message = describe_event(ev, tz)
            self.store.add_event(conn, level, text, ev.get("time"))
            if message:
                self.store.enqueue(conn, message)

    @property
    def last_price(self) -> float:
        if self.in_progress is not None:
            return self.in_progress.close
        return self.state.last_price

    # ------------------------------------------------------------ ciclo
    def tick(self) -> dict:
        now = self.clock()
        self.last_tick_ms = now
        s = self.settings
        try:
            provider, frames = self.market.fetch_bundle([s.entry_interval, s.trend_interval], s.candles_limit)
        except Exception as exc:  # noqa: BLE001
            self._data_failure(exc)
            return {"ok": False, "error": str(exc)}

        df5, df15 = frames[s.entry_interval], frames[s.trend_interval]
        closed5 = df5[df5["close_time"] < now].reset_index(drop=True)
        open5 = df5[df5["close_time"] >= now]
        closed15 = df15[df15["close_time"] < now].reset_index(drop=True)
        if closed5.empty or closed15.empty:
            self._data_failure(RuntimeError("a fonte não retornou candles fechados"))
            return {"ok": False, "error": "sem candles fechados"}

        analyzer = Analyzer(closed5, closed15, s.entry_interval, s.trend_interval)
        in_progress = Candle.from_row(open5.iloc[-1]) if not open5.empty else None
        close_times = closed5["close_time"].to_numpy()

        with self.lock:
            state = copy.deepcopy(self.state)
            trader = PaperTrader(self.params, state)
            if state.last_candle_close is None:
                new_idx = [len(closed5) - 1]
            else:
                new_idx = [int(i) for i in np.flatnonzero(close_times > state.last_candle_close)]
            catchup = len(new_idx) > MAX_CATCHUP
            events: list[dict] = []
            signals: list[Signal] = []
            equity_points = []
            notes: list[tuple[str, str]] = []
            if catchup:
                notes.append(("aviso", f"Robô retomado após {len(new_idx)} candles sem processar. "
                                       "Candles perdidos usados só para checar stop/alvo."))
            for k, i in enumerate(new_idx):
                candle = Candle.from_row(closed5.iloc[i])
                evaluate_now = (not catchup) or k == len(new_idx) - 1

                def signal_fn(i=i):
                    sig = analyzer.signal(i, now)
                    signals.append(sig)
                    return sig

                events += trader.process_candle(candle, signal_fn if evaluate_now else None)
                equity_points.append((candle.close_time, trader.equity(candle.close), candle.close))

            # A ordem decidida no último fechamento é executada na abertura do candle atual.
            if (state.pending is not None and in_progress is not None
                    and in_progress.open_time == state.pending.signal_time + 1):
                events += trader.start_candle(in_progress)
                events += trader.fill_pending(in_progress.open, in_progress.open_time)

            alerts = []
            for sig in signals:
                if sig.data_ok and sig.action != state.last_signal_action and sig.action in (BUY, SELL):
                    alerts.append(signal_alert(sig, state.paused, self.params.timezone))
                if sig.data_ok:
                    state.last_signal_action = sig.action

            if provider != self.provider:
                notes.append(("info", f"Fonte de dados em uso: {provider}."))
            if self.consecutive_failures >= 3:
                notes.append(("info", "Dados normalizados após falhas."))
                alerts.append(f"✅ Dados de mercado normalizados (fonte: {provider}).")

            if new_idx or events or alerts or notes:
                with self.store.transaction() as conn:
                    for level, text in notes:
                        self.store.add_event(conn, level, text)
                    for sig in signals:
                        self.store.save_signal(conn, sig.to_dict())
                    self._record(conn, events)
                    for t, eq, price in equity_points:
                        self.store.save_equity(conn, t, eq, price)
                    for text in alerts:
                        self.store.enqueue(conn, text)
                    self.store.set_kv(conn, STATE_KEY, state.to_dict())
            self.state = state

        self.provider = provider
        self.analyzer = analyzer
        self.in_progress = in_progress
        if signals:
            self.last_signal = signals[-1]
        elif self.last_signal is None:
            self.last_signal = analyzer.signal(len(closed5) - 1, now)
        self.consecutive_failures = 0
        self.last_error = None
        self.last_success_ms = now
        return {"ok": True, "provider": provider, "processed": len(new_idx), "events": len(events)}

    def _data_failure(self, exc: Exception) -> None:
        self.consecutive_failures += 1
        self.last_error = str(exc)
        log.error("Falha ao obter dados (%d seguidas): %s", self.consecutive_failures, exc)
        n = self.consecutive_failures
        if n in (1, 3) or n % 30 == 0:
            with self.store.transaction() as conn:
                self.store.add_event(conn, "erro", f"Falha ao obter dados ({n}x seguidas): {exc}")
                if n == 3:
                    self.store.enqueue(conn, "⚠️ O robô não consegue obter dados de mercado há 3 ciclos. "
                                             "Nenhuma operação nova será feita até normalizar.\n"
                                             f"Erro: {str(exc)[:300]}")

    def run_forever(self, stop: threading.Event) -> None:
        with self.store.transaction() as conn:
            self.store.add_event(conn, "info", "Robô iniciado (modo PAPER TRADING).")
            self.store.enqueue(conn, "🤖 Robô BTC/USDT iniciado em modo SIMULADO (paper trading).")
        while not stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - o robô nunca deve morrer por um erro
                log.exception("Erro inesperado no ciclo do robô")
                self.last_error = f"Erro interno: {exc}"
                try:
                    self.store.log_event("erro", f"Erro interno no ciclo: {exc}")
                except Exception:  # noqa: BLE001
                    log.exception("Não foi possível registrar o erro no banco")
            stop.wait(self.settings.poll_seconds)

    # ------------------------------------------------------- comandos
    def set_paused(self, paused: bool) -> None:
        with self.lock:
            state = copy.deepcopy(self.state)
            state.paused = paused
            if paused and state.pending is not None and state.pending.side == "BUY":
                state.pending = None
            text = ("⏸️ Robô PAUSADO pelo painel. Não abrirá novas operações; "
                    "posição aberta continua protegida por stop e alvo.") if paused else \
                "▶️ Robô RETOMADO pelo painel."
            with self.store.transaction() as conn:
                self.store.set_kv(conn, STATE_KEY, state.to_dict())
                self.store.add_event(conn, "info", text)
                self.store.enqueue(conn, text)
            self.state = state

    def close_position(self) -> bool:
        with self.lock:
            if self.state.position is None:
                return False
            price = self.last_price
            if not price:
                raise RuntimeError("Sem preço atual para encerrar a posição.")
            state = copy.deepcopy(self.state)
            trader = PaperTrader(self.params, state)
            events = trader.close_now(price, self.clock(), "Encerrada manualmente pelo painel")
            with self.store.transaction() as conn:
                self._record(conn, events)
                self.store.set_kv(conn, STATE_KEY, state.to_dict())
            self.state = state
            return True

    # ---------------------------------------------------------- status
    def status(self) -> dict:
        with self.lock:
            s = copy.deepcopy(self.state)
        price = self.last_price or s.last_price
        trader = PaperTrader(self.params, s)
        equity = trader.equity(price)
        pos = s.position
        position = None
        if pos is not None:
            unrealized = pos.qty * price * (1 - self.params.fee_rate) - (pos.qty * pos.entry_price + pos.entry_fee)
            position = {**pos.__dict__, "price": price, "unrealized": unrealized,
                        "notional": pos.qty * price}
        stats = trade_stats(self.store.closed_trades())
        now = self.clock()
        day_loss = s.day_start_equity - equity if s.day else 0.0
        return {
            "paper_trading": True,
            "symbol": "BTC/USDT",
            "price": price,
            "provider": self.provider,
            "initial_balance": s.initial_balance,
            "cash": s.cash,
            "equity": equity,
            "total_pnl": equity - s.initial_balance,
            "total_pnl_pct": (equity / s.initial_balance - 1) * 100,
            "realized_pnl": s.realized_pnl,
            "fees_paid": s.fees_paid,
            "day": s.day,
            "day_pnl": -day_loss,
            "daily_loss_cap": trader.daily_loss_cap,
            "day_loss_used_pct": max(0.0, day_loss) / trader.daily_loss_cap * 100,
            "paused": s.paused,
            "halted": trader.halted,
            "position": position,
            "pending": s.pending.__dict__ if s.pending else None,
            "max_drawdown_pct": s.max_drawdown_pct * 100,
            "max_drawdown_abs": s.max_drawdown_abs,
            "stats": stats,
            "signal": self.last_signal.to_dict() if self.last_signal else None,
            "last_tick_ms": self.last_tick_ms,
            "last_success_ms": self.last_success_ms,
            "last_candle_close": s.last_candle_close,
            "data_age_s": (now - self.last_success_ms) / 1000 if self.last_success_ms else None,
            "consecutive_failures": self.consecutive_failures,
            "last_error": self.last_error,
            "params": self.params.__dict__,
            "now_ms": now,
        }

    def chart_data(self, interval: str) -> dict:
        analyzer, in_progress = self.analyzer, self.in_progress
        if analyzer is None:
            return {"candles": [], "ema_fast": [], "ema_slow": [], "volume": []}
        df = analyzer.ind5 if interval == self.settings.entry_interval else analyzer.ind15
        df = df.tail(200)
        to_s = lambda ms: int(ms) // 1000  # noqa: E731
        candles = [{"time": to_s(r.open_time), "open": r.open, "high": r.high, "low": r.low, "close": r.close}
                   for r in df.itertuples()]
        volume = [{"time": to_s(r.open_time), "value": r.volume, "up": r.close >= r.open} for r in df.itertuples()]
        if interval == self.settings.entry_interval and in_progress is not None:
            c = in_progress
            candles.append({"time": to_s(c.open_time), "open": c.open, "high": c.high, "low": c.low,
                            "close": c.close})
            volume.append({"time": to_s(c.open_time), "value": c.volume, "up": c.close >= c.open})

        def line(col):
            return [{"time": to_s(t), "value": float(v)} for t, v in zip(df["open_time"], df[col]) if v == v]

        return {"candles": candles, "ema_fast": line("ema_fast"), "ema_slow": line("ema_slow"), "volume": volume,
                "labels": ["EMA9", "EMA21"] if interval == self.settings.entry_interval else ["EMA20", "EMA50"]}

