"""
Unit tests for financial data tools in agent/tools/stocks.py.
"""

import pytest
from unittest.mock import patch, MagicMock
from agent.tools.stocks import (
    format_volume,
    _get_finnhub_quote,
    _fetch_quote_sync,
    get_stock_quote,
    compare_stocks,
)


def test_format_volume():
    assert format_volume(1_500_000_000) == "1.50 billion"
    assert format_volume(42_150_000) == "42.15 million"
    assert format_volume(12_400) == "12.4 thousand"
    assert format_volume(500) == "500"


def test_finnhub_quote_mock_success():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "c": 182.50,
        "d": 1.25,
        "dp": 0.69,
        "h": 184.00,
        "l": 181.10,
        "pc": 181.25,
    }

    with patch("requests.get", return_value=mock_resp):
        with patch("agent.tools.stocks.config.finnhub_api_key", "test_key"):
            quote = _get_finnhub_quote("AAPL")
            assert quote is not None
            assert quote["symbol"] == "AAPL"
            assert quote["current_price"] == 182.50
            assert quote["change"] == 1.25
            assert quote["percent_change"] == 0.69
            assert quote["source"] == "finnhub"
            assert quote["volume"] is None


def test_volume_request_triggers_yfinance_fallback():
    """Verify that field='volume' bypasses or augments Finnhub with yfinance volume."""
    mock_yf = {
        "symbol": "AAPL",
        "current_price": 182.50,
        "change": 1.25,
        "percent_change": 0.69,
        "day_high": 184.00,
        "day_low": 181.10,
        "prev_close": 181.25,
        "volume": 45000000,
        "source": "yfinance",
    }

    with patch("agent.tools.stocks._get_yfinance_quote", return_value=mock_yf):
        result = _fetch_quote_sync("AAPL", field="volume")
        assert result is not None
        assert result["field"] == "volume"
        assert result["volume"] == 45000000
        assert result["source"] == "yfinance"


def test_finnhub_failure_falls_back_to_yfinance():
    mock_yf = {
        "symbol": "MSFT",
        "current_price": 420.00,
        "change": 3.00,
        "percent_change": 0.72,
        "day_high": 422.00,
        "day_low": 418.00,
        "prev_close": 417.00,
        "volume": 20000000,
        "source": "yfinance",
    }

    with patch("agent.tools.stocks._get_finnhub_quote", return_value=None):
        with patch("agent.tools.stocks._get_yfinance_quote", return_value=mock_yf):
            result = _fetch_quote_sync("MSFT", field="price")
            assert result is not None
            assert result["symbol"] == "MSFT"
            assert result["current_price"] == 420.00
            assert result["source"] == "yfinance"


@pytest.mark.asyncio
async def test_get_stock_quote_spoken_formatting():
    mock_quote = {
        "symbol": "AAPL",
        "current_price": 180.00,
        "change": 2.00,
        "percent_change": 1.12,
        "day_high": 181.0,
        "day_low": 178.0,
        "prev_close": 178.0,
        "volume": 50000000,
        "source": "yfinance",
        "field": "price"
    }

    with patch("agent.tools.stocks._fetch_quote_sync", return_value=mock_quote):
        res = await get_stock_quote({"symbols": ["AAPL"], "field": "price"})
        assert "AAPL is at $180.00, up 1.12% today." in res["spoken_text"]


@pytest.mark.asyncio
async def test_compare_stocks_requires_two():
    res = await compare_stocks({"symbols": ["AAPL"]})
    assert "error" in res
    assert "at least two" in res["error"]


def _quote(symbol, price=100.0, pct=1.0, volume=9_030_436):
    return {
        "symbol": symbol, "current_price": price, "change": 1.0, "percent_change": pct,
        "day_high": price, "day_low": price, "prev_close": price, "volume": volume,
        "source": "yfinance",
    }


@pytest.mark.asyncio
async def test_volume_question_speaks_volume_not_price():
    # Regression: the voice layer used to answer volume questions with the price.
    with patch("agent.tools.stocks._fetch_quote_sync", return_value=_quote("AAPL")):
        res = await get_stock_quote({"symbols": ["AAPL"], "field": "volume"})
    assert res["spoken_text"] == "AAPL trading volume is 9.03 million shares today."
    assert "$" not in res["spoken_text"]


@pytest.mark.asyncio
async def test_comparison_with_one_failed_ticker_still_answers():
    def fake(symbol, field="price"):
        return None if symbol == "XYZQ" else _quote(symbol, price=180.0, pct=-0.5)

    with patch("agent.tools.stocks._fetch_quote_sync", side_effect=fake):
        res = await compare_stocks({"symbols": ["AAPL", "XYZQ"], "field": "price"})

    assert res["is_comparison"] is True
    assert [r["symbol"] for r in res["results"]] == ["AAPL"]
    assert res["failed_symbols"] == ["XYZQ"]
    assert "AAPL at $180.00 down 0.50%" in res["spoken_text"]
    assert "couldn't get data for XYZQ" in res["spoken_text"]


@pytest.mark.asyncio
async def test_volume_comparison_reports_volumes():
    with patch("agent.tools.stocks._fetch_quote_sync", side_effect=lambda s, f="price": _quote(s, volume=2_500_000)):
        res = await compare_stocks({"symbols": ["AAPL", "TSLA"], "field": "volume"})
    assert res["spoken_text"] == "AAPL 2.50 million shares vs TSLA 2.50 million shares"


@pytest.mark.asyncio
async def test_indian_ticker_is_spoken_in_rupees():
    with patch("agent.tools.stocks._fetch_quote_sync", return_value=_quote("RELIANCE.NS", price=1202.8)):
        res = await get_stock_quote({"symbols": ["RELIANCE.NS"], "field": "price"})
    assert "1,202.80 rupees" in res["spoken_text"]