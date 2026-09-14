# Lead Router

Project #01 from the [Proof of Work](https://claude.ai/code/artifact/02b489c1-763f-4138-bbd0-020e7d0a544e) portfolio: automate one back-office task end to end, then measure what it was actually worth.

This one qualifies and routes inbound leads for a commercial cleaning and facilities contractor. It's split into two halves: n8n handles the plumbing (the webhook, notifications, the CRM handoff), and a Python/LangGraph service does the actual thinking. There's also an eval harness and an ROI dashboard, since a demo GIF doesn't tell you if the thing works or if it's worth the money.

If you wanted to point this at a different business, most of the work is already done. The business profile (who the ideal customer is, the routing rules, the fallback keywords) lives in one plain-language file, [`business.yaml`](business.yaml), instead of being buried in Python. The Slack notifications in the n8n workflow are also real HTTP calls to a webhook URL, not placeholders. See the [go-live checklist](#go-live-checklist) below for what it actually takes to point this at a real client.

```
inbound lead ─▶ n8n webhook ─▶ FastAPI /qualify ─▶ LangGraph agent ─▶ SQLite ─▶ n8n routes by tier
                                                        │
                                                        ├─ prefilter (free, catches obvious spam)
                                                        └─ score: Claude if ANTHROPIC_API_KEY set,
                                                                  deterministic heuristic otherwise
```

## Why it's built this way

If there's no API key, the heuristic scorer takes over instead of the request just failing. That's the real fallback path this thing runs in production, not a stub, and the eval harness grades it honestly (71.4% overall, worse on the `hot` tier) instead of pretending it's fine.

The graph isn't just a straight chain either. `agent/graph.py` has a real conditional edge: messages that are obviously empty or full of spam keywords never make it to the LLM at all. Same idea as project #07 in the portfolio — don't pay for a frontier model to answer something two lines of Python already knows.

The eval set has a couple of traps in it on purpose. One entry in `data/golden_eval_set.json` says "nothing urgent though" — the heuristic scorer keyword-matches on "urgent" and gets it wrong. That's not a bug I quietly fixed; it's the exact kind of mistake the eval harness is supposed to catch, and it's the clearest argument for why the LLM-scored path earns its cost.

## Quickstart

```bash
python -m venv .venv
source .venv/Scripts/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env                 # optional: add ANTHROPIC_API_KEY / AGENT_API_KEY

# 1. Run the tests, linter, and type checker (all offline, no API key needed)
pytest -q
ruff check .
mypy agent data eval scripts dashboard

# 2. Start the agent service
uvicorn agent.main:app --reload

# 3. In another terminal, seed 60 days of realistic history
python -m scripts.seed_history

# 4. Grade the scorer(s) against the golden eval set
python -m eval.run_eval

# 5. Build the ROI dashboard
python -m dashboard.generate_dashboard
# → open dashboard/roi_dashboard.html
```

With no `ANTHROPIC_API_KEY` set, all of this runs fully offline on the heuristic scorer. That's on purpose, so you can walk through the whole pipeline without paying for a single API call. Add a key to `.env` and every new `/qualify` call routes through Claude instead (`agent/scoring.py::llm_score`, using `client.messages.parse` against the `LeadQualification` schema in `agent/schema.py`).

## The n8n side

`n8n/lead-intake-workflow.json` is the plumbing: webhook in, call the agent service, route by `tier`, respond to the caller. The hot and warm branches post to real Slack Incoming Webhooks (an actual `POST` with a formatted message, not a placeholder). Cold stays a labeled placeholder, since nurture tooling is different for every client and there's no point faking one. Spam is a dead end on purpose, already fully handled by the audit log.

Bring it up with:

```bash
docker compose up -d n8n     # http://localhost:5678
```

Then import `n8n/lead-intake-workflow.json` (Workflows → Import from File) and point the HTTP Request node at wherever `agent/main.py` is actually running (`host.docker.internal:8000` by default for local dev).

One honest note: this workflow JSON was hand-written against n8n's documented node shapes and is valid JSON, but it wasn't live-imported in this session. Docker Desktop wasn't running here, and a broken local `npx` environment variable blocked the fallback check too. Import it yourself and fix whatever n8n's UI flags on first open (most likely node `typeVersion` drift against whatever version you're running) before you treat it as production-ready.

## Security

This got an actual pass, not just a glance. Here's what came up and what I did about it:

- **Every SQL query is parameterized** (`agent/store.py`). `tier` and `limit` come from a caller-supplied query parameter, but they're always passed as bound values, never string-formatted into SQL.
- **Every field on `LeadIn` has bounds** (`agent/schema.py`). `email` is validated as a real address (`EmailStr`), and `message` is capped at 5,000 characters. `POST /qualify` has no auth by default, so without that cap someone could submit a huge message and force every LLM-scored request to burn way more tokens, and money, than a real lead ever would.
- **Shared-secret auth is optional** (`agent/security.py`). It's off by default, since local dev and the test suite need it off, and you turn it on by setting `AGENT_API_KEY` and passing a matching `X-API-Key` header. The check uses `secrets.compare_digest()` instead of `==`, so it doesn't leak timing information about a partially-correct key.
- **`GET /leads`'s `limit` is now capped at 500.** It used to be unbounded, which meant any caller could force a much bigger response or table scan than a dashboard, or a person, would ever need.
- **LLM failures are logged instead of silently swallowed.** The original code had a bare `except Exception: pass` in `agent/scoring.py`, which hid real problems (an expired key, a billing block, an outage) behind what looked like normal heuristic-mode behavior. Now there's a most-specific-first exception chain — auth error, rate limit, API error, connection error, other — and each one gets logged with what actually happened before the code falls back.
- **Dashboard tooltips build DOM nodes with `textContent`, not `innerHTML`.** Every value in them is server-generated and safe right now, but a tooltip is exactly the kind of place someone adds a raw field later (a company name, a message snippet) without thinking about XSS, so it's written the safe way from the start.
- **What's intentionally missing:** per-user auth, audit trails beyond the SQLite log, and approval gates before a write action. That's project #09's job in the portfolio, not this one's. The shared secret above is a floor, not a ceiling.

## Go-live checklist

Here's the actual short list to point the agent at a real business. None of it touches Python:

1. **Edit `business.yaml`.** Swap in the real business name, the ideal-customer description, the routing rules, and the fallback keyword lists. It's plain YAML with comments explaining each field. `agent/icp.py` loads it at startup, and everything downstream (the LLM prompt, the offline fallback, the dashboard title) picks it up automatically.
2. **Create two Slack Incoming Webhook URLs** (or one, reused). Takes under 2 minutes at [api.slack.com/apps](https://api.slack.com/apps) → your app → Incoming Webhooks → Add New Webhook. Put them in `.env` as `SLACK_WEBHOOK_HOT` / `SLACK_WEBHOOK_WARM` (see `.env.example`).
3. **Point the real form at the webhook.** Swap out `data/synthetic_leads.py` for whatever the client's actual site or CRM sends, posting to n8n's `/webhook/lead-intake` endpoint.
4. **Add `ANTHROPIC_API_KEY`** if you want Claude doing the scoring instead of the 71%-accurate heuristic fallback. Real client revenue decisions deserve the real scorer.

Two more things worth doing before this handles real money, even though they won't stop it from running:

- **Re-time `MANUAL_MINUTES_PER_LEAD`** in `dashboard/generate_dashboard.py` against how long the client's team actually takes today. Don't present the default 6-minute guess as a measured fact.
- **Re-run `eval/run_eval.py`** against a golden set built from the client's real historical leads, not `data/golden_eval_set.json` (which is written for the example business shipped here), before trusting the tier assignments with real accounts.

## Layout

```
business.yaml   the business profile — edit this, not Python, to go live for a new client
agent/          FastAPI service + LangGraph orchestration + scoring + auth + SQLite store
data/           synthetic lead generator + hand-labeled golden eval set
eval/           the eval harness (project #02's philosophy, applied here)
scripts/        seed_history.py — backfills realistic history for the dashboard
dashboard/      generate_dashboard.py — the actual ROI deliverable
n8n/            the importable workflow (webhook → agent → real Slack notifications → route)
tests/          offline pytest suite (no network calls)
pyproject.toml  ruff + mypy config — both run clean; see the Security section for what they caught
```
