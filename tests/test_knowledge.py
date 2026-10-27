"""
Tests for the general-knowledge tools (agent/tools/knowledge.py), the time
lookup, and the agent's tool definitions. Network calls are mocked, so the
suite runs offline.
"""

from unittest.mock import MagicMock, patch

import pytest

from agent.tools import knowledge
from agent.tools.search import _get_time_sync


def _resp(status=200, text="", json_data=None):
    r = MagicMock()
    r.status_code = status
    r.ok = status == 200
    r.text = text
    r.json.return_value = json_data or {}
    return r


# --- Calculator ---------------------------------------------------------------

@pytest.mark.parametrize("expr,expected", [
    ("2+2", 4),
    ("15% of 2,500", 375),
    ("18% of 1200 + 50", 266),
    ("12 x 12", 144),
    ("2^10", 1024),
    ("sqrt(144) * 3", 36),
    ("sin(30)", 0.5),
    ("factorial(5)", 120),
    ("(1.5+2.5)*3", 12),
    ("10 / 4", 2.5),
])
def test_calculator(expr, expected):
    assert knowledge.calculate(expr)["result"] == expected


@pytest.mark.parametrize("expr", [
    '__import__("os").system("dir")',
    "open('x')",
    "9**9**9",
    "(10**999)**999",
    "factorial(100000)",
    "a + 1",
    "",
])
def test_calculator_rejects_unsafe_or_huge(expr):
    assert "error" in knowledge.calculate(expr)


def test_calculator_division_by_zero():
    assert knowledge.calculate("5/0") == {"error": "Division by zero"}


# --- Currency ----------------------------------------------------------------------

def test_currency_aliases():
    assert knowledge.normalize_currency("rupees") == "INR"
    assert knowledge.normalize_currency("usd") == "USD"
    assert knowledge.normalize_currency("$") == "USD"
    assert knowledge.normalize_currency("bitcoins please") is None


def test_currency_conversion():
    knowledge._RATE_CACHE.clear()
    rates = {"result": "success", "rates": {"INR": 88.25, "EUR": 0.86}}
    with patch("agent.tools.knowledge.requests.get", return_value=_resp(json_data=rates)) as get:
        res = knowledge._convert_sync(100, "dollars", "rupees")
        knowledge._convert_sync(5, "USD", "EUR")  # served from cache
    assert res["result"] == 8825.0
    assert (res["from"], res["to"]) == ("USD", "INR")
    assert get.call_count == 1


def test_currency_unknown_code():
    assert "error" in knowledge._convert_sync(10, "USD", "monopoly money")


# --- News ------------------------------------------------------------------------------

RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Sensex climbs 500 points - Mint</title><source>Mint</source>
  <pubDate>Mon, 26 Oct 2026 08:00:00 GMT</pubDate><link>https://a</link></item>
<item><title>RBI holds rates - Reuters</title><source>Reuters</source>
  <pubDate>Mon, 26 Oct 2026 10:00:00 GMT</pubDate><link>https://b</link></item>
<item><title>Sensex climbs 500 points - Mint</title><source>Mint</source>
  <pubDate>Mon, 26 Oct 2026 07:00:00 GMT</pubDate></item>
</channel></rss>"""


def test_news_rss_parsing():
    items = knowledge.parse_news_rss(RSS, limit=5)
    assert [i["title"] for i in items] == ["RBI holds rates", "Sensex climbs 500 points"]
    assert items[0]["source"] == "Reuters"


def test_news_topic_and_top_stories_urls():
    with patch("agent.tools.knowledge.requests.get", return_value=_resp(text=RSS)) as get:
        topic = knowledge._get_news_sync("stock market", 3, "IN")
        top = knowledge._get_news_sync("", 3, "US")
    assert topic["count"] == 2 and topic["topic"] == "stock market"
    first_url, first_kwargs = get.call_args_list[0].args[0], get.call_args_list[0].kwargs
    assert first_url.endswith("/rss/search") and first_kwargs["params"]["q"].startswith("stock market")
    assert get.call_args_list[1].args[0].endswith("/rss")
    assert get.call_args_list[1].kwargs["params"]["gl"] == "US"
    assert top["topic"] == "top stories"


def test_news_service_down():
    with patch("agent.tools.knowledge.requests.get", return_value=_resp(status=503)):
        assert "error" in knowledge._get_news_sync("AI", 3, None)


# --- Encyclopedia and web search ----------------------------------------------------------

def test_wikipedia_lookup_searches_then_summarises():
    search = _resp(json_data={"query": {"search": [{"title": "A. P. J. Abdul Kalam"}]}})
    summary = _resp(json_data={"title": "A. P. J. Abdul Kalam", "extract": "Indian aerospace scientist."})
    with patch("agent.tools.knowledge.requests.get", side_effect=[search, summary]):
        res = knowledge._wikipedia_sync("who was abdul kalam")
    assert res["found"] is True
    assert res["summary"] == "Indian aerospace scientist."


def test_web_search_prefers_instant_answer_and_lists_results():
    ddg_json = _resp(json_data={"AbstractText": "Paris is the capital of France."})
    lite_html = _resp(text=(
        '<a class="result-link" href="https://x">Paris - Wikipedia</a>'
        '<td class="result-snippet">Capital city of France.</td>'
    ))
    with patch("agent.tools.knowledge.requests.get", return_value=ddg_json), \
         patch("agent.tools.knowledge.requests.post", return_value=lite_html):
        res = knowledge._web_search_sync("capital of france", 3)
    assert res["answer"] == "Paris is the capital of France."
    assert res["results"][0] == {"title": "Paris - Wikipedia", "snippet": "Capital city of France.", "link": "https://x"}


def test_web_search_falls_back_to_wikipedia():
    import requests as _requests
    wiki = {"found": True, "title": "Iceland", "summary": "Nordic island country.", "link": "https://w"}
    with patch("agent.tools.knowledge.requests.get", side_effect=_requests.ConnectionError), \
         patch("agent.tools.knowledge.requests.post", side_effect=_requests.ConnectionError), \
         patch("agent.tools.knowledge._wikipedia_sync", return_value=wiki):
        res = knowledge._web_search_sync("iceland", 3)
    assert res["answer"] == "Nordic island country."


# --- Time -------------------------------------------------------------------------------------

def test_time_resolves_countries_and_cities():
    assert _get_time_sync("Japan")["timezone"] == "Asia/Tokyo"
    assert _get_time_sync("Kathmandu")["timezone"] == "Asia/Kathmandu"
    assert _get_time_sync("Patna")["timezone"] == "Asia/Kolkata"
    assert "spoken_time" in _get_time_sync("London")


def test_time_unknown_place_is_an_error_not_wrong_time():
    with patch("agent.tools.search.requests.get", side_effect=Exception("offline")):
        res = _get_time_sync("Atlantis")
    assert "error" in res


# --- Agent definition ---------------------------------------------------------------------------

def test_every_tool_parameter_is_required_and_non_null():
    """Groq validates tool calls against a strict schema in which every
    parameter is required; an optional parameter the model left out failed
    the whole turn ("missing properties: 'field'")."""
    from livekit.agents.llm import utils
    from agent.orchestrator import Orchestrator
    from agent.voice.livekit_handler import ContinuityAgent

    agent = ContinuityAgent(Orchestrator(), session_id="schema-test")
    names = set()
    for tool in agent.tools:
        schema = utils.build_strict_openai_schema(tool)
        params = schema["function"]["parameters"] if "function" in schema else schema["parameters"]
        assert set(params["required"]) == set(params["properties"]), tool.info.name
        for prop_name, prop in params["properties"].items():
            assert "null" not in str(prop.get("type", "")) and "anyOf" not in prop, (tool.info.name, prop_name)
        names.add(tool.info.name)

    assert {"stock_quote", "compare_stocks", "get_news", "web_search", "lookup_topic",
            "calculate", "convert_currency", "get_weather_info", "get_time_info"} == names


def test_llm_provider_order(monkeypatch):
    from agent.voice import livekit_handler as h
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("GROQ_API_KEY", "y")
    monkeypatch.setenv("LLM_PROVIDERS", "groq, openai")
    assert h.llm_provider_order() == (["groq", "openai"] if h.HAS_GROQ else ["openai"])

    monkeypatch.delenv("OPENAI_API_KEY")
    assert h.llm_provider_order() == (["groq"] if h.HAS_GROQ else [])


def test_instructions_cover_general_questions():
    from datetime import datetime
    from agent.voice.livekit_handler import build_instructions, ASSISTANT_NAME
    text = build_instructions(datetime(2026, 10, 27))
    assert ASSISTANT_NAME in text
    assert "Tuesday, 27 October 2026" in text
    assert "Answer ANY question" in text
    for tool in ("web_search", "get_news", "calculate", "convert_currency", "lookup_topic"):
        assert tool in text