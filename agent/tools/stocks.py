"""
Financial data tool with Finnhub primary + yfinance fallback.

Finnhub: Real-time quotes (requires API key, 60 calls/min free)
yfinance: Fallback when Finnhub key is missing or fails, OR when volume is requested
          (Finnhub free /quote endpoint does not return trading volume).

Multi-ticker support: parallel fetch for comparison queries.
Includes a short-lived TTL cache to prevent duplicate calls within a turn.
"""

import asyncio
import time
from typing import List, Dict, Any, Optional, Tuple
import requests
import yfinance as yf

from agent.config import config
from agent.utils.logger import get_logger

logger = get_logger(__name__)

FINNHUB_QUOTE_URL = "https://finnhub.io/api/v1/quote"
DEFAULT_TIMEOUT_SECONDS = 4.0

# ---------------------------------------------------------------------------
# TTL cache for quote lookups. Prevents the duplicate stock_quote calls we saw
# in the logs (same symbol fetched twice within 2 seconds).
# ---------------------------------------------------------------------------
_QUOTE_CACHE: Dict[Tuple[str, str], Tuple[float, Dict[str, Any]]] = {}
_QUOTE_TTL_SECONDS = 10.0


def _cache_get(symbol: str, field: str) -> Optional[Dict[str, Any]]:
    key = (symbol.upper(), field.lower())
    hit = _QUOTE_CACHE.get(key)
    if hit and (time.time() - hit[0]) < _QUOTE_TTL_SECONDS:
        logger.debug(f"Cache hit for {key}")
        return dict(hit[1])
    return None


def _cache_put(symbol: str, field: str, value: Dict[str, Any]) -> None:
    key = (symbol.upper(), field.lower())
    _QUOTE_CACHE[key] = (time.time(), dict(value))


# =============================================================================
# FORMATTING HELPERS
# =============================================================================

def format_volume(vol: int) -> str:
    """Format volume into human-friendly spoken text."""
    if vol >= 1_000_000_000:
        return f"{vol / 1_000_000_000:.2f} billion"
    if vol >= 1_000_000:
        return f"{vol / 1_000_000:.2f} million"
    if vol >= 1_000:
        return f"{vol / 1_000:.1f} thousand"
    return f"{vol:,}"


# =============================================================================
# FINNHUB QUOTE (Primary for Price)
# =============================================================================

def _get_finnhub_quote(symbol: str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> Optional[Dict[str, Any]]:
    """Fetch quote from Finnhub (synchronous, runs in thread)."""
    api_key = config.finnhub_api_key
    if not api_key:
        logger.debug(f"Finnhub API key not set, skipping {symbol}")
        return None

    try:
        resp = requests.get(
            FINNHUB_QUOTE_URL,
            params={"symbol": symbol.upper(), "token": api_key},
            timeout=timeout,
        )
        if resp.status_code != 200:
            logger.warning(f"Finnhub HTTP {resp.status_code} for {symbol}")
            return None

        data = resp.json()
        current_price = data.get("c")
        if current_price is None or current_price == 0:
            logger.warning(f"Finnhub returned no price for {symbol}")
            return None

        change = data.get("d")
        pct = data.get("dp")

        return {
            "symbol": symbol.upper(),
            "current_price": round(float(current_price), 2),
            "change": round(float(change), 2) if change is not None else 0.0,
            "percent_change": round(float(pct), 2) if pct is not None else 0.0,
            "day_high": round(float(data["h"]), 2) if data.get("h") else None,
            "day_low": round(float(data["l"]), 2) if data.get("l") else None,
            "prev_close": round(float(data["pc"]), 2) if data.get("pc") else None,
            "volume": None,  # Finnhub free /quote endpoint does not provide volume
            "source": "finnhub",
        }
    except requests.Timeout:
        logger.warning(f"Finnhub timeout ({timeout}s) for {symbol}")
        return None
    except Exception as e:
        logger.warning(f"Finnhub request error for {symbol}: {e}")
        return None


# =============================================================================
# YFINANCE QUOTE (Fallback & Volume Source)
# =============================================================================

def _get_yfinance_quote(symbol: str) -> Optional[Dict[str, Any]]:
    """Fetch quote and volume from yfinance (synchronous, runs in thread)."""
    try:
        ticker = yf.Ticker(symbol.upper())
        hist = ticker.history(period="5d")

        if hist.empty:
            logger.warning(f"yfinance returned empty history for {symbol}")
            return None

        current_price = float(hist["Close"].iloc[-1])
        day_high = float(hist["High"].iloc[-1])
        day_low = float(hist["Low"].iloc[-1])
        volume = int(hist["Volume"].iloc[-1]) if "Volume" in hist and not hist["Volume"].empty else 0

        if len(hist) > 1:
            prev_close = float(hist["Close"].iloc[-2])
        else:
            prev_close = current_price

        change = current_price - prev_close
        percent_change = ((change) / prev_close) * 100 if prev_close else 0.0

        return {
            "symbol": symbol.upper(),
            "current_price": round(current_price, 2),
            "change": round(change, 2),
            "percent_change": round(percent_change, 2),
            "day_high": round(day_high, 2),
            "day_low": round(day_low, 2),
            "prev_close": round(prev_close, 2),
            "volume": volume,
            "source": "yfinance",
        }
    except Exception as e:
        logger.warning(f"yfinance failed for {symbol}: {e}")
        return None


# =============================================================================
# UNIFIED FETCH WITH FIELD-AWARE FALLBACK + CACHE
# =============================================================================

def _fetch_quote_sync(symbol: str, field: str = "price") -> Optional[Dict[str, Any]]:
    """
    Fetch market data for a symbol with field-aware fallback.

    Volume requests must use yfinance directly because Finnhub's free
    /quote endpoint does not return trading volume.
    """
    symbol_clean = symbol.strip().upper()
    field_clean = field.strip().lower() if field else "price"

    # Cache check first — avoids duplicate calls within a short window
    cached = _cache_get(symbol_clean, field_clean)
    if cached is not None:
        return cached

    if field_clean == "volume":
        yf_result = _get_yfinance_quote(symbol_clean)
        if yf_result and yf_result.get("volume") is not None and yf_result["volume"] > 0:
            yf_result["field"] = "volume"
            _cache_put(symbol_clean, field_clean, yf_result)
            return yf_result
        logger.warning(f"Volume lookup failed for {symbol_clean} - yfinance returned no data")
        return None

    # Price/general: Finnhub first
    fh_result = _get_finnhub_quote(symbol_clean)
    if fh_result:
        fh_result["field"] = field_clean
        _cache_put(symbol_clean, field_clean, fh_result)
        return fh_result

    # Fallback to yfinance
    logger.info(f"Falling back to yfinance for {symbol_clean}")
    yf_result = _get_yfinance_quote(symbol_clean)
    if yf_result:
        yf_result["field"] = field_clean
        _cache_put(symbol_clean, field_clean, yf_result)
        return yf_result

    return None


# =============================================================================
# SPOKEN FORMATTING
# =============================================================================

def price_text(symbol: str, price: float) -> str:
    """Price as it should be spoken: dollars for US tickers, rupees for NSE/BSE."""
    if symbol.upper().endswith((".NS", ".BO")):
        return f"{price:,.2f} rupees"
    return f"${price:,.2f}"


def spoken_quote(data: Dict[str, Any], field: str) -> str:
    """One stock, one sentence."""
    if field == "volume":
        vol = data.get("volume")
        if vol:
            return f"{data['symbol']} trading volume is {format_volume(vol)} shares today."
        return f"Trading volume for {data['symbol']} is currently unavailable."
    pct = data.get("percent_change") or 0.0
    direction = "up" if pct >= 0 else "down"
    return (
        f"{data['symbol']} is at {price_text(data['symbol'], data['current_price'])}, "
        f"{direction} {abs(pct):.2f}% today."
    )


def spoken_comparison(results: List[Dict[str, Any]], field: str, failed: List[str]) -> str:
    """Several stocks side by side, plus a note about any that couldn't be fetched."""
    parts = []
    for data in results:
        if field == "volume":
            vol = data.get("volume")
            parts.append(f"{data['symbol']} {format_volume(vol)} shares" if vol else f"{data['symbol']} volume unavailable")
        else:
            pct = data.get("percent_change") or 0.0
            direction = "up" if pct >= 0 else "down"
            parts.append(
                f"{data['symbol']} at {price_text(data['symbol'], data['current_price'])} "
                f"{direction} {abs(pct):.2f}%"
            )
    text = " vs ".join(parts) if parts else "No comparison data available."
    if failed:
        text += f" I couldn't get data for {', '.join(failed)}."
    return text


# =============================================================================
# ASYNC TOOL CALLS (EXPOSED TO AGENT)
# =============================================================================

async def get_stock_quote(intent: Dict[str, Any]) -> Dict[str, Any]:
    """
    Fetch a quote for one symbol, or several for a comparison.
    Non-blocking: synchronous network calls run in a background executor.

    Every successful result carries a ready-to-speak `spoken_text`, so the
    voice layer never has to rebuild the sentence (and can't get the field
    wrong, e.g. answering a volume question with the price).
    """
    symbols = intent.get("symbols", [])
    field = (intent.get("field") or "price").lower()
    if field not in ("price", "volume"):
        field = "price"
    is_comparison = bool(intent.get("is_comparison")) or len(symbols) > 1

    if not symbols:
        return {"error": "No ticker symbol provided"}

    delay = config.artificial_delay_seconds
    if delay > 0:
        logger.info(f"Applying artificial delay {delay}s for stress testing")
        await asyncio.sleep(delay)

    loop = asyncio.get_running_loop()
    tasks = [
        loop.run_in_executor(None, _fetch_quote_sync, sym, field)
        for sym in symbols
    ]
    results = await asyncio.gather(*tasks, return_exceptions=False)
    valid_results = [r for r in results if r is not None]
    failed = [sym.upper() for sym, r in zip(symbols, results) if r is None]

    if not valid_results:
        return {
            "error": f"Could not fetch data for {', '.join(symbols)}. Check tickers or connection.",
            "symbols": symbols,
        }

    if not is_comparison:
        data = valid_results[0]
        data["field"] = field
        data["spoken_text"] = spoken_quote(data, field)
        return data

    # A comparison always returns a list, even if only one symbol came back,
    # and says which ones failed instead of reporting "no data".
    return {
        "is_comparison": True,
        "field": field,
        "results": valid_results,
        "failed_symbols": failed,
        "spoken_text": spoken_comparison(valid_results, field, failed),
    }


async def compare_stocks(intent: Dict[str, Any]) -> Dict[str, Any]:
    """Explicit multi-stock comparison tool."""
    symbols = intent.get("symbols", [])
    if len(symbols) < 2:
        return {"error": "Need at least two symbols to compare"}

    intent["is_comparison"] = True
    return await get_stock_quote(intent)