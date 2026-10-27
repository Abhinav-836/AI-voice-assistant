"""
Search tools for weather, time, news, and general search.
Non-blocking execution using asyncio loop executors with bounded timeouts and input sanitization.
"""

import re
import time
import urllib.parse
import asyncio
from typing import Dict, Any, Optional, Tuple
from datetime import datetime
import zoneinfo
import requests
from bs4 import BeautifulSoup

from agent.utils.logger import get_logger

logger = get_logger(__name__)

SEARCH_TIMEOUT_SECONDS = 4.0

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

COMMON_CITY_TIMEZONES = {
    "new york": "America/New_York",
    "nyc": "America/New_York",
    "san francisco": "America/Los_Angeles",
    "sf": "America/Los_Angeles",
    "los angeles": "America/Los_Angeles",
    "chicago": "America/Chicago",
    "london": "Europe/London",
    "paris": "Europe/Paris",
    "berlin": "Europe/Berlin",
    "tokyo": "Asia/Tokyo",
    "singapore": "Asia/Singapore",
    "hong kong": "Asia/Hong_Kong",
    "sydney": "Australia/Sydney",
    "utc": "UTC",
    "gmt": "GMT",
    # Additional mappings used in the logs
    "delhi": "Asia/Kolkata",
    "new delhi": "Asia/Kolkata",
    "mumbai": "Asia/Kolkata",
    "india": "Asia/Kolkata",
    "ohio": "America/New_York",
    "columbus": "America/New_York",
    "oregon": "America/Los_Angeles",
    "portland": "America/Los_Angeles",
    "cape town": "Africa/Johannesburg",
    "johannesburg": "Africa/Johannesburg",
    "south africa": "Africa/Johannesburg",
    # Countries and common names that aren't part of an IANA zone name
    "japan": "Asia/Tokyo",
    "china": "Asia/Shanghai",
    "beijing": "Asia/Shanghai",
    "uk": "Europe/London",
    "england": "Europe/London",
    "britain": "Europe/London",
    "usa": "America/New_York",
    "us": "America/New_York",
    "america": "America/New_York",
    "germany": "Europe/Berlin",
    "france": "Europe/Paris",
    "italy": "Europe/Rome",
    "spain": "Europe/Madrid",
    "russia": "Europe/Moscow",
    "uae": "Asia/Dubai",
    "abu dhabi": "Asia/Dubai",
    "saudi arabia": "Asia/Riyadh",
    "pakistan": "Asia/Karachi",
    "bangladesh": "Asia/Dhaka",
    "nepal": "Asia/Kathmandu",
    "sri lanka": "Asia/Colombo",
    "australia": "Australia/Sydney",
    "new zealand": "Pacific/Auckland",
    "canada": "America/Toronto",
    "brazil": "America/Sao_Paulo",
    "korea": "Asia/Seoul",
    "south korea": "Asia/Seoul",
    "bangalore": "Asia/Kolkata",
    "bengaluru": "Asia/Kolkata",
    "chennai": "Asia/Kolkata",
    "hyderabad": "Asia/Kolkata",
    "pune": "Asia/Kolkata",
    "patna": "Asia/Kolkata",
    "seattle": "America/Los_Angeles",
    "boston": "America/New_York",
    "texas": "America/Chicago",
    "california": "America/Los_Angeles",
}


def _zone_by_city_name(query_lower: str) -> Optional[str]:
    """Match a city against IANA zone names, e.g. "tokyo" -> Asia/Tokyo,
    "new york" -> America/New_York, "kathmandu" -> Asia/Kathmandu."""
    wanted = query_lower.strip().replace(" ", "_")
    if not wanted:
        return None
    for name in zoneinfo.available_timezones():
        if name.rsplit("/", 1)[-1].lower() == wanted:
            return name
    return None

# Short-lived TTL cache for time lookups. Time doesn't change meaningfully
# within 10s, and the logs showed the same timezone being fetched twice per turn.
_TIME_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_TIME_TTL_SECONDS = 10.0


async def _run_in_thread(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: func(*args, **kwargs))


# =============================================
# WEATHER (via wttr.in — sanitized & bounded)
# =============================================

def _get_weather_sync(city: str) -> Dict[str, Any]:
    clean_city = city.strip()
    if not clean_city:
        return {"error": "Please provide a valid city name"}

    encoded_city = urllib.parse.quote_plus(clean_city)
    url = f"https://wttr.in/{encoded_city}?format=j1"

    resp = requests.get(url, headers=HEADERS, timeout=SEARCH_TIMEOUT_SECONDS)
    resp.raise_for_status()
    data = resp.json()

    current = data.get("current_condition", [{}])[0]
    location = data.get("nearest_area", [{}])[0]

    return {
        "location": location.get("areaName", [{"value": clean_city}])[0].get("value", clean_city),
        "country": location.get("country", [{"value": "Unknown"}])[0].get("value", ""),
        "temperature_c": current.get("temp_C", "N/A"),
        "temperature_f": current.get("temp_F", "N/A"),
        "condition": current.get("weatherDesc", [{"value": "Unknown"}])[0].get("value", ""),
        "humidity": current.get("humidity", "N/A"),
        "wind_speed_kmh": current.get("windSpeedKmph", "N/A"),
        "wind_dir": current.get("winddir16Point", "N/A"),
        "feels_like_c": current.get("FeelsLikeC", "N/A"),
        "source": "wttr.in",
    }


async def get_weather(city: str) -> Dict[str, Any]:
    """
    Get current weather for a city using wttr.in.
    Non-blocking: executed in a background thread executor.
    """
    if not city or not city.strip():
        return {"error": "Please provide a city name"}

    try:
        return await _run_in_thread(_get_weather_sync, city)
    except Exception as e:
        logger.warning(f"Weather fetch failed for '{city}': {e}")
        return {"error": f"Could not get weather: {str(e)}", "location": city.strip()}


# =============================================
# TIME / DATE (via zoneinfo stdlib primary)
# =============================================

def _get_time_sync(timezone_or_city: str = "America/New_York") -> Dict[str, Any]:
    query = timezone_or_city.strip()
    query_lower = query.lower()

    # Cache check
    cached = _TIME_CACHE.get(query_lower)
    if cached and (time.time() - cached[0]) < _TIME_TTL_SECONDS:
        return dict(cached[1])

    iana_tz = COMMON_CITY_TIMEZONES.get(query_lower, query)

    dt = None
    tz_obj = None

    try:
        tz_obj = zoneinfo.ZoneInfo(iana_tz)
        dt = datetime.now(tz_obj)
    except Exception:
        pass

    if dt is None:
        zone = _zone_by_city_name(query_lower)
        if zone:
            tz_obj = zoneinfo.ZoneInfo(zone)
            dt = datetime.now(tz_obj)
            iana_tz = zone

    if dt is None:
        # "time in Mumbai right now" -> match known names as whole words, so
        # short keys like "us" don't match inside "Houston" or "Belarus".
        for known_city, tz_name in COMMON_CITY_TIMEZONES.items():
            if re.search(rf"\b{re.escape(known_city)}\b", query_lower):
                try:
                    tz_obj = zoneinfo.ZoneInfo(tz_name)
                    dt = datetime.now(tz_obj)
                    iana_tz = tz_name
                    break
                except Exception:
                    pass

    if dt is None:
        try:
            url = f"http://worldtimeapi.org/api/timezone/{urllib.parse.quote(query)}"
            resp = requests.get(url, headers=HEADERS, timeout=SEARCH_TIMEOUT_SECONDS)
            if resp.ok:
                data = resp.json()
                dt = datetime.fromisoformat(data["datetime"].replace("Z", "+00:00"))
                iana_tz = data.get("timezone", query)
        except Exception:
            pass

    if dt is None:
        # Previously this silently returned the SERVER's local time labelled
        # "Local", so "time in Japan" could be answered with Indian time.
        return {"error": f"Unknown location '{query}'. Use a city name or an IANA timezone like Asia/Tokyo."}

    utc_offset = dt.strftime("%z") or "UTC"

    result = {
        "timezone": iana_tz,
        "datetime": dt.strftime("%Y-%m-%d %H:%M:%S"),
        "spoken_time": dt.strftime("%I:%M %p").lstrip("0"),
        "day_of_week": dt.strftime("%A"),
        "month": dt.strftime("%B"),
        "year": dt.year,
        "utc_offset": utc_offset,
        "source": "Python zoneinfo",
    }

    _TIME_CACHE[query_lower] = (time.time(), result)
    return result


async def get_time(timezone: str = "America/New_York") -> Dict[str, Any]:
    """
    Get current time and date for a timezone or city.
    Non-blocking: executed in a background thread executor.
    """
    try:
        return await _run_in_thread(_get_time_sync, timezone)
    except Exception as e:
        logger.warning(f"Time fetch failed for '{timezone}': {e}")
        return {
            "error": f"Could not get time: {str(e)}",
            "timezone": timezone,
        }


# =============================================
# STOCK NEWS (via yfinance primary + Google News fallback)
# =============================================

def _get_stock_news_sync(symbol: str, limit: int = 3) -> Dict[str, Any]:
    company_name = symbol.strip().upper()
    if not company_name:
        return {"error": "Please provide a valid ticker symbol"}

    articles = []

    try:
        import yfinance as yf
        ticker = yf.Ticker(company_name)
        yf_news = getattr(ticker, "news", [])
        if yf_news:
            for item in yf_news[:limit]:
                content = item.get("content", {}) if isinstance(item.get("content"), dict) else {}
                title = content.get("title") or item.get("title")
                provider = content.get("provider", {}) if isinstance(content.get("provider"), dict) else {}
                source = provider.get("displayName") or item.get("publisher", "Financial News")
                click_url = content.get("canonicalUrl", {}).get("url") if isinstance(content.get("canonicalUrl"), dict) else item.get("link")

                if title:
                    articles.append({
                        "title": title,
                        "source": source,
                        "published": item.get("providerPublishTime"),
                        "link": click_url,
                    })
    except Exception as e:
        logger.debug(f"yfinance news lookup fallback: {e}")

    if not articles:
        try:
            query = f"{company_name} stock news"
            url = f"https://news.google.com/search?q={urllib.parse.quote_plus(query)}"
            resp = requests.get(url, headers=HEADERS, timeout=SEARCH_TIMEOUT_SECONDS)
            if resp.ok:
                soup = BeautifulSoup(resp.text, "html.parser")
                for item in soup.find_all("article")[:limit]:
                    try:
                        title_elem = item.find("a", class_="JtKRv") or item.find("a")
                        title = title_elem.get_text(strip=True) if title_elem else None
                        link = None
                        if title_elem and title_elem.get("href"):
                            href = title_elem["href"]
                            link = "https://news.google.com" + href[1:] if href.startswith(".") else href
                        source_elem = item.find("div", class_="vr1PYe") or item.find("span")
                        source = source_elem.get_text(strip=True) if source_elem else "Financial News"
                        if title:
                            articles.append({"title": title, "source": source, "published": None, "link": link})
                    except Exception:
                        continue
        except Exception as e:
            logger.warning(f"Google News fallback failed: {e}")

    if not articles:
        return {
            "symbol": company_name,
            "news": [],
            "note": "No recent news found for this ticker.",
            "source": "Stock News Service",
        }

    return {
        "symbol": company_name,
        "company": company_name,
        "news": articles,
        "count": len(articles),
        "source": "yfinance / News Service",
    }


async def get_stock_news(symbol: str, limit: int = 3) -> Dict[str, Any]:
    """
    Get latest news for a stock ticker.
    Non-blocking: executed in a background thread executor.
    """
    if not symbol or not symbol.strip():
        return {"error": "Please provide a ticker symbol"}

    try:
        return await _run_in_thread(_get_stock_news_sync, symbol, limit)
    except Exception as e:
        logger.warning(f"News fetch failed for '{symbol}': {e}")
        return {
            "error": f"Could not fetch news: {str(e)}",
            "symbol": symbol.strip().upper(),
        }


# =============================================
# GENERAL SEARCH (via DuckDuckGo Lite + Wikipedia fallback)
# =============================================

def _search_sync(query: str, limit: int = 5) -> Dict[str, Any]:
    clean_query = query.strip()
    if not clean_query:
        return {"error": "Please provide a search query"}

    results = []

    try:
        resp = requests.post(
            "https://lite.duckduckgo.com/lite/",
            data={"q": clean_query},
            headers=HEADERS,
            timeout=SEARCH_TIMEOUT_SECONDS,
        )
        if resp.ok:
            soup = BeautifulSoup(resp.text, "html.parser")
            for a in soup.select("a.result-link")[:limit]:
                title = a.get_text(strip=True)
                link = a.get("href")
                if title:
                    results.append({"title": title, "description": "", "link": link})
    except Exception as e:
        logger.debug(f"DDG Lite search failed: {e}")

    if not results:
        try:
            wiki_term = urllib.parse.quote(clean_query.replace(" ", "_"))
            wiki_url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{wiki_term}"
            resp = requests.get(wiki_url, headers={"User-Agent": "AIVoiceAssistant/1.0"}, timeout=SEARCH_TIMEOUT_SECONDS)
            if resp.ok:
                data = resp.json()
                if data.get("title") and data.get("extract"):
                    results.append({
                        "title": data["title"],
                        "description": data["extract"],
                        "link": data.get("content_urls", {}).get("desktop", {}).get("page"),
                    })
        except Exception as e:
            logger.debug(f"Wikipedia search fallback failed: {e}")

    if not results:
        return {
            "query": clean_query,
            "results": [],
            "note": "No results found. Try a different query.",
            "source": "Search Service",
        }

    return {
        "query": clean_query,
        "results": results,
        "count": len(results),
        "source": "DuckDuckGo / Wikipedia",
    }


async def search(query: str, limit: int = 5) -> Dict[str, Any]:
    """
    General web search with bounded timeouts.
    Non-blocking: executed in a background thread executor.
    """
    if not query or not query.strip():
        return {"error": "Please provide a search query"}

    try:
        return await _run_in_thread(_search_sync, query, limit)
    except Exception as e:
        logger.warning(f"Search failed for '{query}': {e}")
        return {"error": f"Could not search: {str(e)}", "query": query.strip()}


async def route_search_request(intent: Dict[str, Any]) -> Dict[str, Any]:
    """Route search intent to appropriate tool."""
    search_type = intent.get("type", "general")
    query = intent.get("query", "")
    extra = intent.get("extra", {})

    if search_type == "weather":
        return await get_weather(query)

    if search_type == "time":
        tz = extra.get("timezone", "America/New_York")
        return await get_time(tz)

    if search_type == "news":
        symbol = query.upper()
        limit = extra.get("limit", 3)
        return await get_stock_news(symbol, limit)

    return await search(query)