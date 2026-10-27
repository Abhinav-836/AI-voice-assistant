"""
General-knowledge tools: news on any topic, web search, encyclopedia lookup,
a safe calculator and currency conversion.

All of these use free endpoints that need no API key:
  * Google News RSS            - headlines for any topic, or top stories
  * DuckDuckGo Instant Answers - quick factual answers
  * DuckDuckGo Lite            - plain web results
  * Wikipedia REST/Action API  - encyclopedia summaries
  * open.er-api.com            - daily exchange rates

Every network call has a short timeout and runs in a worker thread, so a slow
site can never block the voice pipeline.
"""

import ast
import asyncio
import math
import operator
import os
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

from agent.utils.logger import get_logger

logger = get_logger(__name__)

TIMEOUT_SECONDS = 5.0
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
WIKI_HEADERS = {"User-Agent": "AIVoiceAssistant/1.0 (voice assistant; contact via GitHub)"}

# Region used for news when the user doesn't name one. "IN" gives Indian
# editions of Google News; "US" gives US editions.
NEWS_REGION = os.getenv("NEWS_REGION", "IN").strip().upper() or "IN"
_NEWS_EDITIONS = {
    "IN": {"hl": "en-IN", "gl": "IN", "ceid": "IN:en"},
    "US": {"hl": "en-US", "gl": "US", "ceid": "US:en"},
    "GB": {"hl": "en-GB", "gl": "GB", "ceid": "GB:en"},
}


async def _in_thread(func, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, func, *args)


# =============================================================================
# NEWS (Google News RSS)
# =============================================================================

def _news_params(region: Optional[str] = None) -> Dict[str, str]:
    return dict(_NEWS_EDITIONS.get((region or NEWS_REGION).upper(), _NEWS_EDITIONS["IN"]))


def parse_news_rss(xml_text: str, limit: int) -> List[Dict[str, Any]]:
    """Google News RSS -> [{title, source, published, link}], newest first.

    Google appends " - Publisher" to every title; that is stripped because
    the publisher has its own field (and reading it twice sounds robotic).
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        logger.warning("News feed returned invalid RSS")
        return []

    items, seen = [], set()
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        source = (item.findtext("source") or "").strip()
        if source and title.endswith(f" - {source}"):
            title = title[: -len(f" - {source}")].strip()
        if not title or title.lower() in seen:
            continue
        seen.add(title.lower())

        published = None
        pub_date = item.findtext("pubDate")
        if pub_date:
            try:
                dt = parsedate_to_datetime(pub_date)
                if dt.tzinfo is not None:
                    dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
                published = dt.isoformat()
            except (TypeError, ValueError):
                published = None

        items.append({
            "title": title,
            "source": source or "Google News",
            "published": published,
            "link": (item.findtext("link") or "").strip(),
        })

    items.sort(key=lambda i: i["published"] or "", reverse=True)
    return items[:limit]


def _get_news_sync(topic: str, limit: int, region: Optional[str]) -> Dict[str, Any]:
    params = _news_params(region)
    topic = (topic or "").strip()
    if topic:
        url = "https://news.google.com/rss/search"
        params["q"] = f"{topic} when:3d"
    else:
        url = "https://news.google.com/rss"  # top stories

    try:
        resp = requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_SECONDS)
        if resp.status_code != 200:
            return {"error": f"News service returned HTTP {resp.status_code}"}
        items = parse_news_rss(resp.text, limit)
    except requests.RequestException as e:
        return {"error": f"Could not reach the news service: {type(e).__name__}"}

    return {"topic": topic or "top stories", "news": items, "count": len(items), "source": "Google News"}


async def get_news(topic: str = "", limit: int = 3, region: Optional[str] = None) -> Dict[str, Any]:
    """Latest headlines about `topic` (a company, ticker, person, subject...), or top stories."""
    return await _in_thread(_get_news_sync, topic, max(1, min(limit, 8)), region)


# =============================================================================
# ENCYCLOPEDIA (Wikipedia)
# =============================================================================

def _wikipedia_sync(topic: str) -> Dict[str, Any]:
    topic = (topic or "").strip()
    if not topic:
        return {"error": "Please name a topic"}
    try:
        # Search first, so "who founded tesla" or a misspelling still finds the page.
        search = requests.get(
            "https://en.wikipedia.org/w/api.php",
            params={"action": "query", "list": "search", "srsearch": topic, "srlimit": 1, "format": "json"},
            headers=WIKI_HEADERS,
            timeout=TIMEOUT_SECONDS,
        )
        hits = search.json().get("query", {}).get("search", []) if search.ok else []
        if not hits:
            return {"topic": topic, "found": False}

        title = hits[0]["title"]
        summary = requests.get(
            f"https://en.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(title.replace(' ', '_'))}",
            headers=WIKI_HEADERS,
            timeout=TIMEOUT_SECONDS,
        )
        if not summary.ok:
            return {"topic": topic, "found": False}
        data = summary.json()
        return {
            "topic": topic,
            "found": True,
            "title": data.get("title", title),
            "summary": data.get("extract", ""),
            "link": data.get("content_urls", {}).get("desktop", {}).get("page"),
            "source": "Wikipedia",
        }
    except (requests.RequestException, ValueError) as e:
        return {"error": f"Could not reach Wikipedia: {type(e).__name__}"}


async def lookup_topic(topic: str) -> Dict[str, Any]:
    """Encyclopedia summary for a person, place, event, concept or organisation."""
    return await _in_thread(_wikipedia_sync, topic)


# =============================================================================
# WEB SEARCH (DuckDuckGo instant answer -> DuckDuckGo Lite -> Wikipedia)
# =============================================================================

def _web_search_sync(query: str, limit: int) -> Dict[str, Any]:
    query = (query or "").strip()
    if not query:
        return {"error": "Please provide a search query"}

    answer = None
    results: List[Dict[str, Any]] = []

    try:
        resp = requests.get(
            "https://api.duckduckgo.com/",
            params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1},
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT_SECONDS,
        )
        if resp.ok:
            data = resp.json()
            answer = (data.get("Answer") or data.get("AbstractText") or "").strip() or None
    except (requests.RequestException, ValueError) as e:
        logger.debug(f"DuckDuckGo instant answer failed: {e}")

    try:
        resp = requests.post(
            "https://lite.duckduckgo.com/lite/",
            data={"q": query},
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT_SECONDS,
        )
        if resp.ok:
            soup = BeautifulSoup(resp.text, "html.parser")
            snippets = [s.get_text(" ", strip=True) for s in soup.select("td.result-snippet")]
            for i, a in enumerate(soup.select("a.result-link")[:limit]):
                title = a.get_text(strip=True)
                if title:
                    results.append({
                        "title": title,
                        "snippet": snippets[i] if i < len(snippets) else "",
                        "link": a.get("href"),
                    })
    except requests.RequestException as e:
        logger.debug(f"DuckDuckGo Lite search failed: {e}")

    if not answer and not results:
        wiki = _wikipedia_sync(query)
        if wiki.get("found"):
            answer = wiki["summary"]
            results.append({"title": wiki["title"], "snippet": wiki["summary"][:300], "link": wiki.get("link")})

    if not answer and not results:
        return {"query": query, "answer": None, "results": [], "note": "No results found."}
    return {"query": query, "answer": answer, "results": results, "source": "DuckDuckGo / Wikipedia"}


async def web_search(query: str, limit: int = 4) -> Dict[str, Any]:
    """Search the web; returns a direct answer when one exists, plus top results."""
    return await _in_thread(_web_search_sync, query, max(1, min(limit, 6)))


# =============================================================================
# CALCULATOR (safe expression evaluation)
# =============================================================================

_BIN_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCTIONS = {
    "sqrt": math.sqrt, "cbrt": lambda x: math.copysign(abs(x) ** (1 / 3), x),
    "sin": lambda x: math.sin(math.radians(x)), "cos": lambda x: math.cos(math.radians(x)),
    "tan": lambda x: math.tan(math.radians(x)),
    "log": math.log10, "ln": math.log, "log2": math.log2, "exp": math.exp,
    "abs": abs, "round": round, "floor": math.floor, "ceil": math.ceil,
    "factorial": math.factorial,
}
_CONSTANTS = {"pi": math.pi, "e": math.e}
_MAX_EXPONENT = 1000
_MAX_FACTORIAL = 500


class CalculationError(ValueError):
    pass


def _normalize_expression(expr: str) -> str:
    """Turn spoken/typed maths into Python syntax: 'x'/'×' -> '*', '^' -> '**',
    '÷' -> '/', '15% of 200' -> '(15/100)*200', thousands separators removed."""
    text = expr.lower().strip()
    text = text.replace("×", "*").replace("÷", "/").replace("^", "**")
    text = re.sub(r"(?<=\d),(?=\d{3}\b)", "", text)
    text = re.sub(r"(\d+(?:\.\d+)?)\s*%\s*of\s*", r"(\1/100)*", text)
    text = re.sub(r"(\d+(?:\.\d+)?)\s*%", r"(\1/100)", text)
    text = re.sub(r"(?<=[\d)])\s*x\s*(?=[\d(])", "*", text)
    return text


def _eval_node(node):
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.Name) and node.id in _CONSTANTS:
        return _CONSTANTS[node.id]
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_eval_node(node.operand))
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        left, right = _eval_node(node.left), _eval_node(node.right)
        if isinstance(node.op, ast.Pow):
            # Cap both the exponent and the size of the result, so something
            # like (10**999)**999 can't tie up the CPU building a huge integer.
            if abs(right) > _MAX_EXPONENT or (
                isinstance(left, int) and left.bit_length() * abs(right) > 20_000
            ):
                raise CalculationError("That exponent is too large")
        if isinstance(node.op, ast.Mult) and isinstance(left, int) and isinstance(right, int) \
                and left.bit_length() + right.bit_length() > 20_000:
            raise CalculationError("That number is too large")
        return _BIN_OPS[type(node.op)](left, right)
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in _FUNCTIONS and not node.keywords and len(node.args) in (1, 2)):
        args = [_eval_node(a) for a in node.args]
        if node.func.id == "factorial" and (args[0] > _MAX_FACTORIAL or args[0] != int(args[0])):
            raise CalculationError("Factorial only works for whole numbers up to 500")
        return _FUNCTIONS[node.func.id](*args)
    raise CalculationError("Unsupported expression")


def calculate(expression: str) -> Dict[str, Any]:
    """Evaluate arithmetic safely (no eval(): only numbers, operators and math functions)."""
    if not expression or len(expression) > 200:
        return {"error": "Please give a shorter expression"}
    normalized = _normalize_expression(expression)
    try:
        value = _eval_node(ast.parse(normalized, mode="eval"))
    except ZeroDivisionError:
        return {"error": "Division by zero"}
    except (CalculationError, SyntaxError, TypeError, ValueError, OverflowError) as e:
        return {"error": f"I couldn't calculate that ({e.__class__.__name__}: {e})"}

    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return {"error": "The result isn't a finite number"}
        value = int(value) if value.is_integer() and abs(value) < 1e15 else round(value, 6)
    return {"expression": expression, "result": value}


# =============================================================================
# CURRENCY CONVERSION (open.er-api.com, daily rates, no key)
# =============================================================================

_RATE_CACHE: Dict[str, Tuple[float, Dict[str, float]]] = {}
_RATE_TTL_SECONDS = 3600
_CURRENCY_ALIASES = {
    "rupee": "INR", "rupees": "INR", "rs": "INR", "₹": "INR",
    "dollar": "USD", "dollars": "USD", "$": "USD", "usd": "USD",
    "euro": "EUR", "euros": "EUR", "€": "EUR",
    "pound": "GBP", "pounds": "GBP", "£": "GBP",
    "yen": "JPY", "¥": "JPY", "dirham": "AED", "dirhams": "AED",
}


def normalize_currency(code: str) -> Optional[str]:
    text = (code or "").strip().lower()
    if text in _CURRENCY_ALIASES:
        return _CURRENCY_ALIASES[text]
    text = text.upper()
    return text if re.fullmatch(r"[A-Z]{3}", text) else None


def _rates_for(base: str) -> Optional[Dict[str, float]]:
    cached = _RATE_CACHE.get(base)
    if cached and time.time() - cached[0] < _RATE_TTL_SECONDS:
        return cached[1]
    resp = requests.get(f"https://open.er-api.com/v6/latest/{base}", timeout=TIMEOUT_SECONDS)
    if not resp.ok:
        return None
    data = resp.json()
    if data.get("result") != "success":
        return None
    rates = data.get("rates") or {}
    _RATE_CACHE[base] = (time.time(), rates)
    return rates


def _convert_sync(amount: float, from_currency: str, to_currency: str) -> Dict[str, Any]:
    src, dst = normalize_currency(from_currency), normalize_currency(to_currency)
    if not src or not dst:
        return {"error": "Use currency codes like USD, INR or EUR"}
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return {"error": "The amount must be a number"}
    try:
        rates = _rates_for(src)
    except (requests.RequestException, ValueError) as e:
        return {"error": f"Could not reach the exchange-rate service: {type(e).__name__}"}
    if not rates or dst not in rates:
        return {"error": f"No exchange rate available for {src} to {dst}"}
    rate = float(rates[dst])
    return {
        "amount": amount, "from": src, "to": dst,
        "rate": round(rate, 6), "result": round(amount * rate, 2),
        "source": "open.er-api.com (daily rates)",
    }


async def convert_currency(amount: float, from_currency: str, to_currency: str) -> Dict[str, Any]:
    return await _in_thread(_convert_sync, amount, from_currency, to_currency)