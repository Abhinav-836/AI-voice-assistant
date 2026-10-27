# AI Voice Assistant

A real-time personal voice assistant you can talk to about anything, and interrupt mid-answer. Ask a question, change your mind while it's still looking something up, and it answers the new question, never the old one.

Speech goes over WebRTC (LiveKit) to an agent that transcribes it (Deepgram), reasons and calls tools (Groq and/or OpenAI, in the order you choose), and speaks back (Rime TTS). Sessions, transcripts and tool calls are stored in SQLite.

**What it can do**, by voice or by typing:

- **Answer anything:** general knowledge, science, history, maths, coding, explanations, advice, planning and small talk, from the model's own knowledge.
- **Look things up live:** web search for anything recent or uncertain, and encyclopedia lookups for people, places, organisations and concepts.
- **News on any topic:** a company, a person, sports, tech or markets, or just the top stories (Indian or US edition).
- **Markets:** live stock prices and trading volume (US and NSE/BSE tickers), plus side-by-side comparisons.
- **Everyday utilities:** exact calculations (percentages, EMIs, powers, roots), currency conversion at today's rates, weather, and the time in any city or country.
- **Context:** it remembers the conversation, so follow-ups like *"and what about Tesla?"* or *"what's its latest news?"* work. It knows today's date, your default location, and optionally your name.
---

## The problem it solves: interruptions

Tool calls take time. If you ask *"What's Apple's price?"* and then say *"wait, check Nvidia instead"* while the lookup is still running, a naive agent finishes the Apple lookup and speaks it anyway, then answers about Nvidia.

This agent uses **turn-version fencing**:

1. Every new request bumps a monotonic turn version, and every tool call is tagged with the version it started under.
2. A new request cancels running tool calls before the version changes.
3. When a tool finishes, its version is checked against the current one (`state.is_current(version)`). A stale result is discarded and never spoken.

The check is deterministic: it doesn't depend on timing, delays or network jitter. Typed messages go through exactly the same path as speech.

---

## Architecture

```
Browser (WebRTC audio + chat)  ──►  LiveKit room room_{session_id}  ──►  Voice agent worker
        │                                                                  ├─ Deepgram STT + Silero VAD
        │ POST /api/session                                                ├─ Turn versioning + fencing (agent/state.py)
        ▼                                                                  ├─ LLM (OpenAI → Groq fallback) with tools
HTTP server (frontend/serve.py)                                            ├─ Tools: Finnhub/yfinance, weather, time, news, search
  ├─ creates private sessions + signed session keys                        └─ Rime TTS (WebSocket streaming)
  ├─ issues 30-minute LiveKit tokens                                              │
  └─ serves the web app                                                           ▼
                         SQLite (WAL): sessions · turns · tool_executions · settings
```

More detail in [`docs/architecture.md`](docs/architecture.md).

---

## Quick start

**Requirements:** Python 3.11–3.13, and API keys for LiveKit Cloud, Deepgram, Rime, and OpenAI and/or Groq. Finnhub is optional (yfinance is used without it).

```bash
git clone https://github.com/Abhinav-836/AI-voice-assistant.git
cd AI-voice-assistant

# Setup (creates venv, installs requirements, copies .env.example to .env)
./scripts/setup.sh            # Windows: .\scripts\setup.ps1

# Fill in .env, then:
python frontend/serve.py      # web app + API on http://localhost:5500
python -m agent.main dev      # voice agent worker (second terminal)
```

`python -m agent.main console` runs the agent with your local mic and speakers, without the browser.

### Docker

```bash
docker-compose up --build -d     # server on :5500 + agent worker
docker-compose logs -f
```

---

## Security

- **Private sessions.** `POST /api/session` creates a session in its own LiveKit room and returns a secret session key (an HMAC of the session id) to that browser only. Getting a new token, reading the transcript and changing settings all require the key. Knowing a session id or room name is not enough.
- **No public session list.** Listing all sessions needs `Authorization: Bearer <ADMIN_TOKEN>` and is disabled while `ADMIN_TOKEN` is unset. The app's Session History only shows sessions created in your own browser.
- **Short-lived tokens.** LiveKit tokens expire after 30 minutes and only grant access to one room.
- **Validated input.** Voice settings are allow-listed before they reach the TTS engine, ticker symbols are validated, typed messages are length-limited, and request bodies are capped at 64 KB.
- **Headers and CORS.** Strict CSP, `nosniff`, `X-Frame-Options: DENY`. Only origins in `ALLOWED_ORIGINS` get CORS headers.
- **Rate limiting.** Per-IP sliding window. `X-Forwarded-For` is trusted only with `TRUST_PROXY=1`, so the limit can't be dodged with a fake header.
- **No secrets in the repo.** Keys are read from `.env` and masked in logs. CI fails if anything that looks like a real key is committed.

---

## Configuration

See [`.env.example`](.env.example) for every option. The important ones:

| Variable | Purpose |
|---|---|
| `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | WebRTC transport |
| `DEEPGRAM_API_KEY` | Speech-to-text |
| `RIME_API_KEY`, `RIME_MODEL_ID`, `RIME_SPEAKER` | Text-to-speech (default `coda` / `celeste`) |
| `OPENAI_API_KEY`, `GROQ_API_KEY` | LLM (at least one) |
| `LLM_PROVIDERS` | Order to try them in, e.g. `groq,openai` (put the one with credits first) |
| `GROQ_MODEL` | Default `openai/gpt-oss-120b` |
| `ASSISTANT_NAME`, `USER_NAME` | What the assistant calls itself and you |
| `DEFAULT_LOCATION` | Used for time and weather when no place is named (default `Asia/Kolkata`) |
| `NEWS_REGION` | News edition: `IN`, `US` or `GB` |
| `STT_LANGUAGE` | `en-US`, or `multi` for speech that mixes English and Hindi |
| `FINNHUB_API_KEY` | Real-time quotes (optional) |
| `SESSION_SIGNING_KEY` | Signs session keys (defaults to `LIVEKIT_API_SECRET`) |
| `ADMIN_TOKEN` | Enables the admin session list |
| `TRUST_PROXY` | Set to `1` behind Render/Railway/Nginx |

---

## Tests

```bash
pytest -v
```

The suite runs without any API keys or network access:

- `test_fencing.py`, `test_interruption.py`, `test_state_versioning.py`: stale results are fenced, rapid repeated barge-ins, cancellation and exception safety.
- `test_router.py`: company-name and `$TICKER` resolution, price ↔ volume switches, comparisons.
- `test_stocks.py`: Finnhub parsing, yfinance fallback, spoken price/volume sentences, partial comparisons.
- `test_persistence.py`: SQLite schema, sessions, turns, tool executions, settings validation.
- `test_security_api.py`: session keys, token and transcript access control, admin-only listing, settings, CORS, rate-limit IP handling.
- `test_knowledge.py`: safe calculator (including rejecting code and huge numbers), currency conversion, news feeds, web search and Wikipedia fallbacks, time in any city, strict tool schemas, LLM provider order.

---

## Credits

Built with Amar Jyoti Hota, Harsh Raj and Suraj Kumar, Abhinav Ashutosh.

**License:** MIT