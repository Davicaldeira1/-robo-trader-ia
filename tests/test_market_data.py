"""Leitura dos formatos reais das APIs públicas (respostas simuladas) e troca de fonte."""
import httpx
import pytest

from app.market_data import (BinanceProvider, BlockedError, CoinbaseProvider, DataError, MarketData, OKXProvider,
                             count_gaps, normalize)

T0 = 1_700_000_100_000 - (1_700_000_100_000 % 900_000)
M5 = 300_000


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_binance_format():
    def handler(request):
        assert request.method == "GET" and request.url.path == "/api/v3/klines"
        assert request.url.params["symbol"] == "BTCUSDT"
        rows = [[T0 + i * M5, "100.0", "101.0", "99.0", "100.5", "12.5", T0 + (i + 1) * M5 - 1, "0", 1, "0", "0", "0"]
                for i in range(3)]
        return httpx.Response(200, json=rows)

    df = BinanceProvider(client_for(handler)).fetch_recent("BTCUSDT", "5m", 3)
    assert list(df["open_time"]) == [T0, T0 + M5, T0 + 2 * M5]
    assert df["close"].iloc[0] == 100.5 and df["volume"].iloc[0] == 12.5
    assert df["close_time"].iloc[0] == T0 + M5 - 1


def test_okx_format_newest_first():
    def handler(request):
        assert request.url.params["instId"] == "BTC-USDT"
        data = [[str(T0 + i * M5), "100", "102", "98", "101", "3", "0", "0", "1"] for i in (2, 1, 0)]
        return httpx.Response(200, json={"code": "0", "msg": "", "data": data})

    df = OKXProvider(client_for(handler)).fetch_recent("BTCUSDT", "5m", 3)
    assert list(df["open_time"]) == [T0, T0 + M5, T0 + 2 * M5]  # reordenado do mais antigo ao mais novo


def test_coinbase_format_low_high_order():
    def handler(request):
        assert request.url.path == "/products/BTC-USDT/candles"
        # Coinbase: [tempo_s, mínima, máxima, abertura, fechamento, volume]
        return httpx.Response(200, json=[[T0 // 1000, 98, 102, 100, 101, 7]])

    df = CoinbaseProvider(client_for(handler)).fetch_recent("BTCUSDT", "5m", 10)
    row = df.iloc[0]
    assert (row["open"], row["high"], row["low"], row["close"], row["volume"]) == (100, 102, 98, 101, 7)


def test_invalid_candles_are_dropped_and_gaps_counted():
    rows = [[T0, 100, 101, 99, 100, 1], [T0 + M5, 100, 90, 99, 100, 1],  # máxima < mínima: inválido
            [T0 + 3 * M5, 100, 101, 99, 100, 1], [T0 + 3 * M5, 100, 101, 99, 100.5, 1]]  # duplicado
    df = normalize(rows, "5m")
    assert list(df["open_time"]) == [T0, T0 + 3 * M5]
    assert df["close"].iloc[-1] == 100.5
    assert count_gaps(df, "5m") == 2


def test_geoblock_is_not_retried_and_next_source_is_used():
    calls = {"binance": 0}

    def handler(request):
        if "binance" in request.url.host:
            calls["binance"] += 1
            return httpx.Response(451, json={"msg": "Service unavailable from a restricted location"})
        data = [[str(T0 + i * M5), "100", "102", "98", "101", "3", "0", "0", "1"] for i in range(5)]
        return httpx.Response(200, json={"code": "0", "data": data})

    client = client_for(handler)
    md = MarketData(["binance", "okx"], client=client)
    provider, frames = md.fetch_bundle(["5m", "15m"], 5)
    assert provider == "okx"
    assert calls["binance"] == 1  # bloqueio não é repetido
    assert md.preferred == "okx"
    with pytest.raises(BlockedError):
        BinanceProvider(client).fetch_recent("BTCUSDT", "5m", 5)


def test_server_errors_are_retried_then_reported():
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        return httpx.Response(503)

    provider = BinanceProvider(client_for(handler))
    provider.retry_delay = 0
    with pytest.raises(DataError):
        provider.fetch_recent("BTCUSDT", "5m", 5)
    assert attempts["n"] == 3


def test_all_sources_failing_raises_clear_error():
    md = MarketData(["binance", "okx", "coinbase"], client=client_for(lambda r: httpx.Response(403)))
    with pytest.raises(DataError, match="Todas as fontes"):
        md.fetch_bundle(["5m"], 5)
    assert len(md.last_errors) == 3


def test_unknown_provider_name():
    with pytest.raises(ValueError):
        MarketData(["bolsa-inventada"])
