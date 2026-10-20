"""Dados de mercado reais (candles e volume) de APIs PÚBLICAS.

Somente leitura: nenhuma destas classes envia ordens nem usa chaves de API.
Se uma fonte falhar (fora do ar, bloqueio por região, limite de requisições),
a próxima da lista é usada automaticamente.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import httpx
import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000}
COLUMNS = ["open_time", "open", "high", "low", "close", "volume", "close_time"]
USER_AGENT = "robo-trader-ia/1.0 (paper trading; dados publicos)"


class DataError(RuntimeError):
    """Não foi possível obter dados válidos."""


class BlockedError(DataError):
    """A fonte recusou o acesso (ex.: bloqueio por região). Não adianta repetir."""


def interval_ms(interval: str) -> int:
    try:
        return INTERVAL_MS[interval]
    except KeyError as exc:
        raise ValueError(f"Intervalo não suportado: {interval}") from exc


def normalize(rows: list[list[float]], interval: str) -> pd.DataFrame:
    """Converte linhas [open_time_ms, o, h, l, c, v] num DataFrame limpo e ordenado."""
    step = interval_ms(interval)
    if not rows:
        return pd.DataFrame(columns=COLUMNS)
    df = pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close", "volume"])
    df = df.astype({"open_time": "int64", "open": float, "high": float, "low": float,
                    "close": float, "volume": float})
    df = df.replace([np.inf, -np.inf], np.nan).dropna()
    prices = df[["open", "high", "low", "close"]]
    valid = (prices > 0).all(axis=1) & (df["volume"] >= 0)
    valid &= df["high"] >= df[["open", "close", "low"]].max(axis=1)
    valid &= df["low"] <= df[["open", "close", "high"]].min(axis=1)
    dropped = int((~valid).sum())
    if dropped:
        log.warning("%d candle(s) inválido(s) descartado(s)", dropped)
    df = df[valid]
    df = df[df["open_time"] % step == 0]
    df = df.drop_duplicates("open_time", keep="last").sort_values("open_time")
    df["close_time"] = df["open_time"] + step - 1
    return df.reset_index(drop=True)[COLUMNS]


def count_gaps(df: pd.DataFrame, interval: str) -> int:
    """Quantos candles estão faltando dentro da série."""
    if len(df) < 2:
        return 0
    diffs = np.diff(df["open_time"].to_numpy()) // interval_ms(interval)
    return int(np.sum(diffs - 1))


class Provider:
    name = "base"
    max_recent = 300
    retry_delay = 0.5  # segundos (dobra a cada tentativa)

    def __init__(self, client: httpx.Client):
        self.client = client

    def _get(self, url: str, params: dict) -> object:
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                resp = self.client.get(url, params=params)
            except httpx.HTTPError as exc:  # timeout, conexão recusada, DNS...
                last_exc = exc
            else:
                if resp.status_code in (401, 403, 451):
                    raise BlockedError(f"{self.name}: acesso recusado (HTTP {resp.status_code})")
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_exc = DataError(f"{self.name}: HTTP {resp.status_code}")
                elif resp.status_code != 200:
                    raise DataError(f"{self.name}: HTTP {resp.status_code} {resp.text[:200]}")
                else:
                    try:
                        return resp.json()
                    except ValueError as exc:
                        raise DataError(f"{self.name}: resposta não é JSON") from exc
            if attempt < 2:
                time.sleep(self.retry_delay * (2 ** attempt))
        raise DataError(f"{self.name}: falhou após 3 tentativas ({last_exc})")

    def fetch_recent(self, symbol: str, interval: str, limit: int) -> pd.DataFrame:
        raise NotImplementedError

    def fetch_range(self, symbol: str, interval: str, start_ms: int, end_ms: int) -> pd.DataFrame:
        raise NotImplementedError


class BinanceProvider(Provider):
    """Endpoint oficial da Binance só para dados de mercado (sem login)."""

    name = "binance"
    base_url = "https://data-api.binance.vision"
    max_recent = 1000

    def _klines(self, params: dict) -> list[list[float]]:
        data = self._get(f"{self.base_url}/api/v3/klines", params)
        if not isinstance(data, list):
            raise DataError(f"binance: resposta inesperada {str(data)[:200]}")
        return [[int(k[0]), k[1], k[2], k[3], k[4], k[5]] for k in data]

    def fetch_recent(self, symbol, interval, limit):
        rows = self._klines({"symbol": symbol, "interval": interval, "limit": min(limit, self.max_recent)})
        return normalize(rows, interval)

    def fetch_range(self, symbol, interval, start_ms, end_ms):
        rows: list = []
        cursor = start_ms
        while cursor <= end_ms:
            batch = self._klines({"symbol": symbol, "interval": interval, "startTime": cursor,
                                  "endTime": end_ms, "limit": 1000})
            if not batch:
                break
            rows.extend(batch)
            cursor = int(batch[-1][0]) + interval_ms(interval)
            time.sleep(0.1)
        return normalize(rows, interval)


class OKXProvider(Provider):
    name = "okx"
    base_url = "https://www.okx.com"
    max_recent = 300
    _bars = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1H"}

    @staticmethod
    def _inst(symbol: str) -> str:
        return symbol.replace("USDT", "-USDT")

    def _candles(self, path: str, params: dict) -> list[list[float]]:
        data = self._get(f"{self.base_url}{path}", params)
        if not isinstance(data, dict) or data.get("code") != "0":
            raise DataError(f"okx: resposta inesperada {str(data)[:200]}")
        return [[int(c[0]), c[1], c[2], c[3], c[4], c[5]] for c in data.get("data", [])]

    def fetch_recent(self, symbol, interval, limit):
        rows = self._candles("/api/v5/market/candles", {
            "instId": self._inst(symbol), "bar": self._bars[interval], "limit": min(limit, self.max_recent)})
        return normalize(rows, interval)

    def fetch_range(self, symbol, interval, start_ms, end_ms):
        rows: list = []
        after = end_ms + 1  # OKX devolve candles mais antigos que "after"
        while True:
            batch = self._candles("/api/v5/market/history-candles", {
                "instId": self._inst(symbol), "bar": self._bars[interval], "after": after, "limit": 100})
            if not batch:
                break
            rows.extend(batch)
            oldest = min(int(r[0]) for r in batch)
            if oldest <= start_ms:
                break
            after = oldest
            time.sleep(0.15)
        df = normalize(rows, interval)
        return df[(df["open_time"] >= start_ms) & (df["open_time"] <= end_ms)].reset_index(drop=True)


class CoinbaseProvider(Provider):
    name = "coinbase"
    base_url = "https://api.exchange.coinbase.com"
    max_recent = 300
    _gran = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}

    @staticmethod
    def _product(symbol: str) -> str:
        return symbol.replace("USDT", "-USDT")

    def _candles(self, symbol: str, params: dict) -> list[list[float]]:
        data = self._get(f"{self.base_url}/products/{self._product(symbol)}/candles", params)
        if not isinstance(data, list):
            raise DataError(f"coinbase: resposta inesperada {str(data)[:200]}")
        # formato Coinbase: [tempo_s, mínima, máxima, abertura, fechamento, volume]
        return [[int(c[0]) * 1000, c[3], c[2], c[1], c[4], c[5]] for c in data]

    def fetch_recent(self, symbol, interval, limit):
        rows = self._candles(symbol, {"granularity": self._gran[interval]})
        return normalize(rows, interval).tail(limit).reset_index(drop=True)

    def fetch_range(self, symbol, interval, start_ms, end_ms):
        step = interval_ms(interval)
        rows: list = []
        cursor = start_ms
        while cursor <= end_ms:
            chunk_end = min(end_ms, cursor + step * 299)
            iso = lambda ms: datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
            rows.extend(self._candles(symbol, {"granularity": self._gran[interval],
                                               "start": iso(cursor), "end": iso(chunk_end)}))
            cursor = chunk_end + step
            time.sleep(0.2)
        df = normalize(rows, interval)
        return df[(df["open_time"] >= start_ms) & (df["open_time"] <= end_ms)].reset_index(drop=True)


PROVIDERS = {"binance": BinanceProvider, "okx": OKXProvider, "coinbase": CoinbaseProvider}


class MarketData:
    """Busca candles tentando cada fonte em ordem, preferindo a última que funcionou.

    Os dois tempos gráficos de um ciclo vêm sempre da MESMA fonte, para que
    a análise não misture preços de corretoras diferentes.
    """

    def __init__(self, provider_names, symbol: str = "BTCUSDT", timeout: float = 10.0,
                 client: httpx.Client | None = None, providers: list[Provider] | None = None):
        self.symbol = symbol
        self.client = client or httpx.Client(timeout=timeout, headers={"User-Agent": USER_AGENT})
        if providers is not None:
            self.providers = providers
        else:
            unknown = [n for n in provider_names if n not in PROVIDERS]
            if unknown:
                raise ValueError(f"Fonte de dados desconhecida: {', '.join(unknown)}")
            self.providers = [PROVIDERS[n](self.client) for n in provider_names]
        self.preferred: str | None = None
        self.last_errors: list[str] = []

    def _ordered(self) -> list[Provider]:
        return sorted(self.providers, key=lambda p: p.name != self.preferred)

    def _try_all(self, fetch) -> tuple[str, dict[str, pd.DataFrame]]:
        errors = []
        for provider in self._ordered():
            try:
                frames = fetch(provider)
            except Exception as exc:  # noqa: BLE001 - qualquer falha leva à próxima fonte
                log.warning("Fonte %s falhou: %s", provider.name, exc)
                errors.append(str(exc))
                continue
            self.preferred = provider.name
            self.last_errors = errors
            return provider.name, frames
        self.last_errors = errors
        raise DataError("Todas as fontes de dados falharam — " + " | ".join(errors))

    def fetch_bundle(self, intervals: list[str], limit: int) -> tuple[str, dict[str, pd.DataFrame]]:
        def fetch(provider: Provider):
            frames = {}
            for interval in intervals:
                df = provider.fetch_recent(self.symbol, interval, limit)
                if len(df) < 2:
                    raise DataError(f"{provider.name}: poucos candles de {interval}")
                frames[interval] = df
            return frames

        return self._try_all(fetch)

    def fetch_history(self, intervals: list[str], start_ms: int, end_ms: int):
        def fetch(provider: Provider):
            frames = {}
            for interval in intervals:
                df = provider.fetch_range(self.symbol, interval, start_ms, end_ms)
                if len(df) < 2:
                    raise DataError(f"{provider.name}: histórico vazio para {interval}")
                frames[interval] = df
            return frames

        return self._try_all(fetch)
