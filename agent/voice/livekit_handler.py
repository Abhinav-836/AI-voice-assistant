"""
LiveKit voice agent: configurable LLM chain (OpenAI / Groq), Rime TTS, and SQLite telemetry.

Turn version fencing and tool execution tracking are integrated directly with SQLite
persistence and real-time LiveKit room data channel broadcasts.
"""

import asyncio
import os
import json
import time
from datetime import datetime
from typing import Literal, Optional, Dict, Any
from dotenv import load_dotenv

from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    JobProcess,
    RunContext,
    function_tool,
    llm,
)
from livekit.agents import llm as llm_module
from livekit.plugins import silero
from livekit.plugins import deepgram
from livekit.plugins import openai

try:
    from livekit.plugins import groq
    HAS_GROQ = True
except ImportError:
    HAS_GROQ = False
    groq = None

from agent.voice.rime_tts import build_rime_tts
from agent.orchestrator import Orchestrator
from agent.tools.stocks import get_stock_quote, compare_stocks
from agent.tools.search import get_weather, get_time
from agent.tools import knowledge
from agent.db.repository import repo
from agent.utils.logger import get_logger

load_dotenv()
logger = get_logger(__name__)

server = AgentServer()

# How many past messages the LLM sees. Long enough to follow a real
# conversation ("and what about its revenue?"), short enough to stay fast.
MAX_CHAT_CONTEXT_ITEMS = 40

# Persona. ASSISTANT_NAME is how it introduces itself; USER_NAME (optional)
# is how it addresses you; DEFAULT_LOCATION is used for "what time is it" /
# "what's the weather" when no place is mentioned.
ASSISTANT_NAME = os.getenv("ASSISTANT_NAME", "Nova").strip() or "Nova"
USER_NAME = os.getenv("USER_NAME", "").strip()
DEFAULT_LOCATION = os.getenv("DEFAULT_LOCATION", "Asia/Kolkata").strip() or "Asia/Kolkata"


def build_instructions(now: Optional[datetime] = None) -> str:
    """System prompt: a general assistant that uses tools only for live data."""
    now = now or datetime.now()
    who = f"The user's name is {USER_NAME}; address them by name now and then. " if USER_NAME else ""
    return (
        f"You are {ASSISTANT_NAME}, a sharp, friendly personal voice assistant, like a "
        f"capable human aide. {who}"
        f"Today is {now.strftime('%A, %d %B %Y')}. The user's default location is {DEFAULT_LOCATION}.\n\n"
        "Answer ANY question: general knowledge, science, history, maths, coding, "
        "language and grammar, definitions, advice, planning, recommendations, "
        "explanations, jokes, small talk. Answer from your own knowledge whenever "
        "the answer doesn't depend on live or recent information.\n\n"
        "Use tools only when they make the answer correct or current:\n"
        "- stock_quote / compare_stocks: live stock prices or volume. Never invent a ticker; "
        "Indian stocks end in .NS (e.g. RELIANCE.NS).\n"
        "- get_news: recent headlines on any topic (empty topic = top stories).\n"
        "- web_search: anything recent, local, or that you are not sure about "
        "(results, schedules, prices of products, current office-holders, events after your training).\n"
        "- lookup_topic: encyclopedia facts about people, places, organisations, concepts.\n"
        "- get_weather_info, get_time_info: weather and time anywhere.\n"
        "- calculate: any arithmetic beyond trivial; never do multi-step maths in your head.\n"
        "- convert_currency: money conversions at today's rates.\n"
        "Combine tools when a question needs several (e.g. a stock price plus its news).\n\n"
        "Speaking style: you are heard, not read. Reply in one to three short, natural "
        "sentences unless the user asks for detail; no markdown, lists, URLs or emoji; say "
        "numbers the way a person would. Summarise tool results in your own words instead of "
        "reading them out verbatim. Remember what was said earlier in the conversation and "
        "resolve follow-ups like \"what about Tesla?\" or \"and its news?\" from context. "
        "If a request is ambiguous, ask one short clarifying question. If a tool fails, say so "
        "briefly and offer what you do know. Be honest when you don't know something."
    )

# Typed chat messages longer than this are cut, so one paste can't blow up
# the LLM context (and the bill).
MAX_CHAT_MESSAGE_CHARS = 500


def llm_provider_order() -> list:
    """Provider order from LLM_PROVIDERS (e.g. "groq,openai"), keeping only
    providers that have an API key.

    Order matters: FallbackAdapter tries the first provider on EVERY turn
    and only then fails over. With an OpenAI key that has no credits left,
    "openai,groq" spent 1-9 seconds per reply on doomed OpenAI retries
    before Groq answered (seen in the logs), so put the provider you
    actually pay for, or the free one you rely on, first.
    """
    raw = os.getenv("LLM_PROVIDERS", "openai,groq")
    order = []
    for name in (p.strip().lower() for p in raw.split(",")):
        if name == "openai" and os.getenv("OPENAI_API_KEY") and "openai" not in order:
            order.append("openai")
        elif name == "groq" and os.getenv("GROQ_API_KEY") and HAS_GROQ and "groq" not in order:
            order.append("groq")
    return order


def _build_llm():
    """Build the LLM chain in LLM_PROVIDERS order, failing over on errors."""
    llms = []
    for name in llm_provider_order():
        if name == "openai":
            model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
            logger.info(f"LLM #{len(llms) + 1}: OpenAI (model={model})")
            llms.append(openai.LLM(model=model))
        elif name == "groq":
            model = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
            logger.info(f"LLM #{len(llms) + 1}: Groq (model={model})")
            llms.append(groq.LLM(model=model))

    if not llms:
        logger.warning("No LLM API key found (need OPENAI_API_KEY or GROQ_API_KEY)")
        return None
    if len(llms) == 1:
        return llms[0]
    return llm_module.FallbackAdapter(llms)


def prewarm(proc: JobProcess) -> None:
    """Load VAD once per worker process to minimize connection latency."""
    proc.userdata["vad"] = silero.VAD.load()


server.setup_fnc = prewarm


class ContinuityAgent(Agent):
    """
    The LLM-facing Agent. Tool methods are exposed via @function_tool.
    Handles turn version fencing, database recording, and real-time telemetry broadcasts.
    """

    def __init__(self, orchestrator: Orchestrator, session_id: str, room=None):
        super().__init__(instructions=build_instructions())
        self.orchestrator = orchestrator
        self.session_id = session_id
        self.room = room
        self.last_broadcast_agent_text = ""
        self.turn_number = 0
        self.current_user_text = ""
        self._has_greeted = False

    async def broadcast(self, event_type: str, data: dict) -> None:
        """Broadcast real-time JSON events across the LiveKit Room DataChannel."""
        if not self.room or not getattr(self.room, "local_participant", None):
            return
        try:
            payload = json.dumps({"type": event_type, "timestamp": time.time(), **data})
            await self.room.local_participant.publish_data(payload, reliable=True)
        except Exception as e:
            logger.debug(f"Could not broadcast data channel event {event_type}: {e}")

    def _truncate_chat_ctx(self, turn_ctx: llm.ChatContext) -> None:
        """
        Bound the chat context before each LLM turn.

        ChatContext.truncate() is the supported API in livekit-agents 1.7/1.8 —
        the AgentSession constructor does not accept max_chat_history, so
        truncation is done here instead. The turn_ctx passed to
        on_user_turn_completed is a per-turn working copy.
        """
        try:
            if len(turn_ctx.items) > MAX_CHAT_CONTEXT_ITEMS:
                turn_ctx.truncate(max_items=MAX_CHAT_CONTEXT_ITEMS)
                logger.debug(
                    f"Truncated chat context to {MAX_CHAT_CONTEXT_ITEMS} items"
                )
        except Exception as e:
            logger.debug(f"Chat context truncate skipped: {e}")

    async def on_user_turn_completed(
        self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage
    ) -> None:
        """Called when user speech ends, right before LLM reasoning."""
        self._truncate_chat_ctx(turn_ctx)
        text = new_message.text_content
        logger.info(f"User turn completed: '{text}'")
        if text:
            await self.process_user_text(text)

    async def process_user_text(self, text: str) -> bool:
        """
        Run one user message through turn versioning, persistence and telemetry.

        Shared by spoken turns (on_user_turn_completed) and typed chat
        messages. LiveKit only calls on_user_turn_completed for speech, so
        typed messages used to skip this entirely: a typed "no, Tesla
        instead" never cancelled the running Apple lookup, and the stale
        Apple answer could still be spoken.

        Returns True when the message is a new request (the version was bumped).
        """
        self.current_user_text = text
        self.turn_number += 1
        self.orchestrator.metrics.mark("utterance_received")

        version, is_new, intent = await self.orchestrator.on_user_utterance(
            text, is_final=True
        )

        try:
            repo.record_turn(
                session_id=self.session_id,
                turn_number=self.turn_number,
                turn_version=version,
                sender="user",
                text=text,
                is_interrupted=is_new,
            )
        except Exception as e:
            logger.warning(f"Failed to record user turn to DB: {e}")

        if is_new:
            await self.broadcast("interruption", {
                "version": version,
                "text": text,
                "intent": intent,
            })
        return is_new

    async def on_enter(self) -> None:
        """Called when the agent enters the session. Only greet once."""
        if not self._has_greeted:
            self._has_greeted = True
            await self.session.generate_reply(
                instructions=(
                    f"Greet the user in one short sentence as {ASSISTANT_NAME}"
                    + (f", addressing them as {USER_NAME}" if USER_NAME else "")
                    + ", and offer to help with anything. Don't list your features."
                )
            )

    # =============================================
    # Shared tool wrapper
    # =============================================
    async def _run_tool(
        self,
        tool_name: str,
        params: dict,
        coro,
        spoken_formatter,
        broadcast_extra: Optional[dict] = None,
    ) -> str:
        version = self.orchestrator.state.turn_version
        t0 = time.time()

        self.orchestrator.mark_tool_started()

        tool_db_id = repo.record_tool_execution(
            session_id=self.session_id,
            turn_version=version,
            tool_name=tool_name,
            parameters=params,
            status="running",
        )

        await self.broadcast("tool_start", {
            "name": tool_name,
            "version": version,
            **(broadcast_extra or {}),
        })

        result = await self.orchestrator.tool_mgr.run(version, coro)
        latency_ms = round((time.time() - t0) * 1000, 1)

        self.orchestrator.mark_tool_resolved()

        if result is None:
            repo.update_tool_execution(
                tool_db_id, status="cancelled",
                latency_ms=latency_ms,
                error="Superseded by user interruption",
            )
            await self.broadcast("tool_cancel", {
                "name": tool_name,
                "stale_version": version,
                "reason": "User interruption / superseded",
                **(broadcast_extra or {}),
            })
            return "That request was superseded by a newer one — no result to report."

        if isinstance(result, dict) and "error" in result:
            repo.update_tool_execution(
                tool_db_id, status="error",
                latency_ms=latency_ms, error=result["error"],
            )
            await self.broadcast("tool_complete", {
                "name": tool_name, "version": version,
                "error": result["error"], "latency_ms": latency_ms,
            })
            return f"Couldn't complete {tool_name}: {result['error']}"

        repo.update_tool_execution(
            tool_db_id, status="success",
            latency_ms=latency_ms, result=result,
        )

        # The formatted text goes back to the LLM as the tool result; the LLM
        # then answers in its own words. Only that final answer is shown in
        # the transcript (via conversation_item_added) - showing the raw tool
        # text too would print search dumps and duplicate every reply.
        tool_text = spoken_formatter(result)

        self.orchestrator.mark_response_spoken()

        await self.broadcast("tool_complete", {
            "name": tool_name, "version": version,
            "data": result, "latency_ms": latency_ms,
        })
        return tool_text

    # =============================================
    # STOCK TOOLS
    # =============================================
    # Every parameter is required (no defaults). Groq validates tool calls
    # against a strict schema in which every property is required, and it
    # rejected calls like stock_quote(symbol="AAPL") with "missing
    # properties: 'field'", which failed the whole turn.

    @function_tool()
    async def stock_quote(
        self,
        context: RunContext,
        symbol: str,
        field: Literal["price", "volume"],
    ) -> str:
        """
        Live stock quote for ONE ticker (US like AAPL, or Indian like RELIANCE.NS).

        Args:
            symbol: Ticker symbol, e.g. AAPL, TSLA, NVDA, TCS.NS.
            field: "price" for price and daily change, "volume" for trading volume.
        """
        sym = symbol.strip().upper()
        field_value = field if field in ("price", "volume") else "price"
        intent = {"symbols": [sym], "field": field_value}

        def fmt(result: dict) -> str:
            # The stock tool builds the sentence for the requested field.
            return result.get("spoken_text") or (
                f"{result['symbol']} is at ${result['current_price']}."
            )

        return await self._run_tool(
            tool_name="stock_quote",
            params={"symbol": sym, "field": field_value},
            coro=get_stock_quote(intent),
            spoken_formatter=fmt,
            broadcast_extra={"symbol": sym, "field": field_value},
        )

    @function_tool()
    async def compare_stocks(
        self,
        context: RunContext,
        symbols: str,
        field: Literal["price", "volume"],
    ) -> str:
        """
        Compare two or more tickers side by side. Use only when the user asks to compare.

        Args:
            symbols: Comma-separated tickers, e.g. "AAPL,TSLA".
            field: "price" or "volume".
        """
        symbol_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        if len(symbol_list) < 2:
            return "I need at least two ticker symbols to compare. For a single stock, use stock_quote."

        field_value = field if field in ("price", "volume") else "price"
        intent = {"symbols": symbol_list, "field": field_value, "is_comparison": True}

        def fmt(result: dict) -> str:
            # Includes volume comparisons and names any ticker that failed.
            return result.get("spoken_text") or "No comparison data available."

        return await self._run_tool(
            tool_name="compare_stocks",
            params={"symbols": symbol_list, "field": field_value},
            coro=compare_stocks(intent),
            spoken_formatter=fmt,
            broadcast_extra={"symbols": symbol_list},
        )

    # =============================================
    # WORLD TOOLS: weather, time, news, search
    # =============================================

    @function_tool()
    async def get_weather_info(self, context: RunContext, city: str) -> str:
        """
        Current weather for a city.

        Args:
            city: City name, e.g. "Delhi", "London", "Cape Town".
        """
        clean_city = city.strip()

        def fmt(data: dict) -> str:
            return (
                f"Weather in {data['location']}: {data['temperature_c']}°C, "
                f"{data['condition']}, humidity {data['humidity']}%, "
                f"wind {data['wind_speed_kmh']} km/h."
            )

        return await self._run_tool(
            tool_name="get_weather",
            params={"city": clean_city},
            coro=get_weather(clean_city),
            spoken_formatter=fmt,
            broadcast_extra={"city": clean_city},
        )

    @function_tool()
    async def get_time_info(self, context: RunContext, location: str) -> str:
        """
        Current local time and date somewhere.

        Args:
            location: City, country or IANA timezone, e.g. "Mumbai", "Japan", "America/New_York".
                Use the user's own city if they just ask "what time is it".
        """
        clean_location = location.strip() or DEFAULT_LOCATION

        def fmt(data: dict) -> str:
            date = data["datetime"][:10]
            return f"{data.get('spoken_time', data['datetime'])} on {data['day_of_week']} {date} ({data['timezone']})"

        return await self._run_tool(
            tool_name="get_time",
            params={"location": clean_location},
            coro=get_time(clean_location),
            spoken_formatter=fmt,
            broadcast_extra={"timezone": clean_location},
        )

    @function_tool()
    async def get_news(self, context: RunContext, topic: str) -> str:
        """
        Latest news headlines about any topic: a company or ticker, a person, a
        country, sports, technology, markets... Pass an empty string for top stories.

        Args:
            topic: What the news should be about, e.g. "Tesla", "stock market",
                "India cricket", "AI". Empty string for general top stories.
        """
        clean_topic = topic.strip()

        def fmt(data: dict) -> str:
            items = data.get("news") or []
            if not items:
                return f"I couldn't find recent news about {clean_topic or 'that'}."
            headlines = "; ".join(f"{item['title']} ({item['source']})" for item in items)
            return f"Latest on {data.get('topic', clean_topic)}: {headlines}"

        return await self._run_tool(
            tool_name="get_news",
            params={"topic": clean_topic},
            coro=knowledge.get_news(clean_topic, limit=3),
            spoken_formatter=fmt,
            broadcast_extra={"topic": clean_topic},
        )

    @function_tool()
    async def web_search(self, context: RunContext, query: str) -> str:
        """
        Search the web for current or specific facts you don't reliably know:
        recent events, prices, schedules, results, local info, anything after
        your training data.

        Args:
            query: A focused search query, e.g. "IPL 2026 final result".
        """
        clean_query = query.strip()

        def fmt(data: dict) -> str:
            parts = []
            if data.get("answer"):
                parts.append(f"Answer: {data['answer']}")
            for item in (data.get("results") or [])[:4]:
                snippet = f" - {item['snippet']}" if item.get("snippet") else ""
                parts.append(f"{item['title']}{snippet}")
            return " | ".join(parts) if parts else f"No results found for '{clean_query}'."

        return await self._run_tool(
            tool_name="web_search",
            params={"query": clean_query},
            coro=knowledge.web_search(clean_query),
            spoken_formatter=fmt,
            broadcast_extra={"query": clean_query},
        )

    @function_tool()
    async def lookup_topic(self, context: RunContext, topic: str) -> str:
        """
        Encyclopedia summary (Wikipedia) of a person, place, organisation,
        event or concept. Good for "who is", "what is", "tell me about".

        Args:
            topic: The subject, e.g. "APJ Abdul Kalam", "black holes", "Infosys".
        """
        clean_topic = topic.strip()

        def fmt(data: dict) -> str:
            if not data.get("found"):
                return f"I couldn't find an encyclopedia entry for {clean_topic}."
            return f"{data['title']}: {data['summary']}"

        return await self._run_tool(
            tool_name="lookup_topic",
            params={"topic": clean_topic},
            coro=knowledge.lookup_topic(clean_topic),
            spoken_formatter=fmt,
            broadcast_extra={"topic": clean_topic},
        )

    # =============================================
    # UTILITY TOOLS: maths and money
    # =============================================

    @function_tool()
    async def calculate(self, context: RunContext, expression: str) -> str:
        """
        Exact arithmetic. Use for any calculation instead of doing it in your head:
        percentages, tips, EMIs, unit maths, powers, roots.

        Args:
            expression: e.g. "18% of 2450", "(1200*12)/7", "sqrt(2)^2", "5 factorial" as "factorial(5)".
        """
        clean_expr = expression.strip()

        async def run():
            return knowledge.calculate(clean_expr)

        def fmt(data: dict) -> str:
            return f"{clean_expr} = {data['result']}"

        return await self._run_tool(
            tool_name="calculate",
            params={"expression": clean_expr},
            coro=run(),
            spoken_formatter=fmt,
            broadcast_extra={"expression": clean_expr},
        )

    @function_tool()
    async def convert_currency(
        self,
        context: RunContext,
        amount: float,
        from_currency: str,
        to_currency: str,
    ) -> str:
        """
        Convert money between currencies at today's exchange rate.

        Args:
            amount: The amount to convert, e.g. 100.
            from_currency: 3-letter code of the source currency, e.g. "USD".
            to_currency: 3-letter code of the target currency, e.g. "INR".
        """
        def fmt(data: dict) -> str:
            return (
                f"{data['amount']:,.2f} {data['from']} is about {data['result']:,.2f} {data['to']} "
                f"(rate {data['rate']})."
            )

        return await self._run_tool(
            tool_name="convert_currency",
            params={"amount": amount, "from": from_currency, "to": to_currency},
            coro=knowledge.convert_currency(amount, from_currency, to_currency),
            spoken_formatter=fmt,
            broadcast_extra={"from": from_currency, "to": to_currency},
        )


@server.rtc_session()
async def entrypoint(ctx: JobContext) -> None:
    room_name = ctx.room.name
    ctx.log_context_fields = {"room": room_name}

    session_id = room_name[5:] if room_name.startswith("room_") else room_name

    if not repo.get_session(session_id):
        repo.create_session(session_id=session_id, room_name=room_name)

    session_settings = repo.get_settings(session_id)

    orchestrator = Orchestrator()

    llm = _build_llm()
    if llm is None:
        raise RuntimeError(
            "No LLM provider configured. Set GROQ_API_KEY and/or OPENAI_API_KEY "
            "in your .env (and LLM_PROVIDERS to choose the order) before running."
        )

    tts_plugin = build_rime_tts(
        model=session_settings.get("voice_model"),
        speaker=session_settings.get("voice_speaker"),
        speed=session_settings.get("voice_speed"),
    )

    # NOTE: do NOT pass max_chat_history — it is not a valid AgentSession
    # kwarg in livekit-agents 1.7/1.8, and passing it raises TypeError
    # before the session is constructed. Chat history is bounded inside
    # ContinuityAgent.on_user_turn_completed via ChatContext.truncate().
    #
    # preemptive_generation=False halves LLM request volume. With preemptive
    # on, every user utterance triggers two LLM calls: one speculative call
    # as the transcript settles, and one real call after the turn commits.
    # That doubling is what pushed the earlier Groq-only runs into 429s.
    session = AgentSession(
        # STT_LANGUAGE=multi lets Deepgram Nova-3 follow speech that mixes
        # English with Hindi and other languages; the default is en-US.
        stt=deepgram.STT(
            model=os.getenv("DEEPGRAM_MODEL", "nova-3"),
            language=os.getenv("STT_LANGUAGE", "en-US"),
            smart_format=True,
        ),
        llm=llm,
        tts=tts_plugin,
        vad=ctx.proc.userdata["vad"],
        allow_interruptions=True,
        preemptive_generation=False,
    )

    agent = ContinuityAgent(orchestrator, session_id=session_id, room=ctx.room)

    @session.on("user_input_transcribed")
    def on_transcript(transcript) -> None:
        if transcript.is_final:
            logger.info(f"Final transcript: {transcript.transcript}")
        else:
            logger.debug(f"Interim: {transcript.transcript}")

        async def _handle_transcript():
            await agent.broadcast("transcript", {
                "sender": "You",
                "text": transcript.transcript,
                "is_final": transcript.is_final,
            })

        asyncio.create_task(_handle_transcript())

    @session.on("conversation_item_added")
    def on_item_added(ev) -> None:
        try:
            item = ev.item
            if getattr(item, "role", None) == "assistant":
                text = getattr(item, "text_content", None) or "".join(getattr(item, "content", []))
                if text and text.strip() and text.strip() != agent.last_broadcast_agent_text.strip():
                    agent.last_broadcast_agent_text = text.strip()
                    agent.turn_number += 1
                    try:
                        repo.record_turn(
                            session_id=agent.session_id,
                            turn_number=agent.turn_number,
                            turn_version=orchestrator.state.turn_version,
                            sender="agent",
                            text=text.strip(),
                        )
                    except Exception as e:
                        logger.warning(f"Error persisting conversational agent turn: {e}")

                    logger.info(f"Broadcasting assistant response: {text}")
                    asyncio.create_task(agent.broadcast("agent_response", {
                        "text": text,
                        "version": orchestrator.state.turn_version,
                    }))
        except Exception as e:
            logger.warning(f"Error handling conversation item: {e}")

    @ctx.room.on("data_received")
    def on_data_received(data_packet) -> None:
        try:
            raw = data_packet.data
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            data = json.loads(raw)
            if data.get("type") == "user_chat":
                text = data.get("text")
                if isinstance(text, str) and text.strip():
                    text = text.strip()[:MAX_CHAT_MESSAGE_CHARS]
                    logger.info(f"Received text chat from frontend: {text}")
                    asyncio.create_task(handle_text_chat(session, text, agent))
        except Exception as e:
            logger.warning(f"Error handling data channel packet: {e}")

    async def handle_text_chat(session: AgentSession, text: str, agent: ContinuityAgent):
        try:
            # Same versioning/fencing as speech: a new request cancels any
            # running tool call and makes its result stale.
            is_new = await agent.process_user_text(text)
            if is_new:
                try:
                    # Stop the answer currently being spoken, as a spoken
                    # barge-in would.
                    await session.interrupt()
                except RuntimeError as e:
                    logger.debug(f"Nothing to interrupt before text reply: {e}")
            await session.generate_reply(user_input=text)
        except Exception as e:
            logger.error(f"Error processing text chat: {e}")

    await session.start(agent=agent, room=ctx.room)
    await ctx.connect()


if __name__ == "__main__":
    from livekit.agents import cli
    cli.run_app(server)