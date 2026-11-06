"""Two ways to turn a LeadIn into a LeadQualification.

heuristic_score() is deterministic, free, and has no external dependency -
it's what the golden eval set is graded against when there's no API key,
and it's the automatic fallback if the LLM call errors. llm_score() is the
real reasoning path: Claude reads the ICP + scoring guide and returns two
scores via client.messages.parse(); agent/rules.py then decides the tier.

score_lead() is the single entry point the graph calls - it picks LLM when
available and healthy, heuristic otherwise, and always returns which one
actually ran so that's visible in the stored record and the dashboard.
"""
from __future__ import annotations

import logging
import os
import time

import httpx

from . import store
from .icp import (
    ALWAYS_ESCALATE_TIERS,
    BUSINESS_NAME,
    ESCALATE_BELOW_CONFIDENCE,
    FREE_EMAIL_DOMAINS,
    HOT_KEYWORDS,
    ICP_DESCRIPTION,
    RESIDENTIAL_KEYWORDS,
    ROUTING_RUBRIC,
    SPAM_KEYWORDS,
)
from .rules import qualify_from_scores
from .schema import LeadIn, LeadQualification, LLMScore

logger = logging.getLogger(__name__)

MODEL_ID = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5")
LOCAL_MODEL = os.environ.get("LOCAL_MODEL", "llama3.1:8b")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
# A local 8B model on a consumer GPU answers in a few seconds. If it takes
# longer than this, it's stuck or still loading, and the lead moves on.
LOCAL_TIMEOUT_S = float(os.environ.get("LOCAL_TIMEOUT_S", "10"))

# Set to a list by eval/run_eval.py to collect Claude's token usage per call
# (that's how the eval prices a lead). None in normal operation.
usage_log: list[dict] | None = None

SYSTEM_PROMPT = f"""You are the lead-qualification agent for {BUSINESS_NAME}.

{ICP_DESCRIPTION}

{ROUTING_RUBRIC}

Read the inbound message exactly as a real sales rep would - don't assume
facts the message doesn't state, and don't round an ambiguous case up to
"hot" just because the sender sounds enthusiastic. Silence on a signal
(no urgency mentioned, no company named) means that score should be low,
not guessed."""


def _format_lead(lead: LeadIn) -> str:
    return (
        f"Name: {lead.name}\n"
        f"Email: {lead.email}\n"
        f"Company: {lead.company or '(not provided)'}\n"
        f"Phone: {lead.phone or '(not provided)'}\n"
        f"Source: {lead.source}\n"
        f"Message:\n{lead.message}"
    )


def llm_score(lead: LeadIn) -> LeadQualification:
    import anthropic  # imported lazily so the module loads without the package during tests

    client = anthropic.Anthropic()
    response = client.messages.parse(
        model=MODEL_ID,
        max_tokens=1024,
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": _format_lead(lead)}],
        output_format=LLMScore,
    )
    # parsed_output is Optional: a refusal or a response that hits max_tokens
    # before finishing its JSON can both leave it unset. Treating that as a
    # hard failure (instead of silently returning None as if it were a real
    # qualification) is what lets score_lead()'s except-chain catch it and
    # fall back to the heuristic scorer, logged, rather than crashing later
    # with a confusing "NoneType has no attribute .tier".
    usage = getattr(response, "usage", None)
    if usage_log is not None and usage is not None:
        usage_log.append({
            "input_tokens": usage.input_tokens or 0,
            "output_tokens": usage.output_tokens or 0,
            "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
            "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
        })
    if response.parsed_output is None:
        raise ValueError(
            f"Claude returned no parsed output (stop_reason={response.stop_reason!r})"
        )
    # The model proposed the scores; the rules in business.yaml decide the tier.
    return qualify_from_scores(response.parsed_output)


def local_score(lead: LeadIn) -> LeadQualification:
    """The low-level decider: same prompt, same LLMScore schema, same rules -
    only the model changes. Ollama's `format` takes a JSON schema and
    constrains decoding to it, so the reply parses like Claude's does.
    temperature 0 keeps it repeatable (Ollama accepts sampling params; only
    the current Claude models reject them)."""
    resp = httpx.post(
        f"{OLLAMA_URL}/api/chat",
        json={
            "model": LOCAL_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _format_lead(lead)},
            ],
            "format": LLMScore.model_json_schema(),
            "stream": False,
            "options": {"temperature": 0},
            # Ollama unloads an idle model after 5 minutes by default; loading it
            # back takes longer than LOCAL_TIMEOUT_S, so the first lead after a
            # quiet spell would time out and escalate for no real reason.
            "keep_alive": os.environ.get("LOCAL_KEEP_ALIVE", "30m"),
        },
        timeout=LOCAL_TIMEOUT_S,
    )
    resp.raise_for_status()
    scores = LLMScore.model_validate_json(resp.json()["message"]["content"])
    return qualify_from_scores(scores)


def needs_escalation(qual: LeadQualification) -> bool:
    """Should Claude double-check the local model's answer? Thresholds live
    in business.yaml's `cascade` block."""
    return qual.confidence < ESCALATE_BELOW_CONFIDENCE or qual.tier in ALWAYS_ESCALATE_TIERS


def heuristic_score(lead: LeadIn) -> LeadQualification:
    """Deterministic, dependency-free fallback. Not as sharp as the LLM on
    ambiguous cases - that gap is exactly what the eval harness (eval/run_eval.py)
    measures when it grades both scorers against the same golden set."""
    text = f"{lead.message} {lead.company or ''}".lower()

    is_spam = any(k in text for k in SPAM_KEYWORDS) or len(lead.message.strip()) < 3
    is_residential = any(k in text for k in RESIDENTIAL_KEYWORDS)
    hot_hits = sum(1 for k in HOT_KEYWORDS if k in text)
    has_company = bool(lead.company and lead.company.strip())
    email_domain = lead.email.split("@")[-1].lower() if "@" in lead.email else ""
    free_email = email_domain in FREE_EMAIL_DOMAINS

    if is_spam:
        return LeadQualification(
            tier="spam", icp_fit_score=0, urgency_score=0,
            suggested_owner="discard", confidence=0.7,
            reasoning="Matched spam/vendor-pitch keyword pattern.",
        )

    if is_residential and not has_company:
        return LeadQualification(
            tier="cold", icp_fit_score=10, urgency_score=10,
            suggested_owner="nurture", confidence=0.6,
            reasoning="Residential language, no company - outside ICP, real person.",
        )

    fit = 30
    if has_company:
        fit += 25
    if not free_email:
        fit += 15
    if lead.source == "referral":
        fit += 15
    fit = min(fit, 95)

    urgency = min(hot_hits * 25, 90)

    if hot_hits >= 1 and has_company:
        tier, owner, conf = "hot", "sales-enterprise", 0.55
    elif has_company:
        tier, owner, conf = "warm", "sales-smb", 0.55
    else:
        tier, owner, conf = "cold", "nurture", 0.5

    return LeadQualification(
        tier=tier, icp_fit_score=fit, urgency_score=urgency,
        suggested_owner=owner, confidence=conf,
        reasoning=(
            f"Heuristic: {hot_hits} urgency keyword(s), "
            f"{'has' if has_company else 'no'} company name, "
            f"{'free' if free_email else 'business'} email domain."
        ),
    )


def _has_claude_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def _try_local(lead: LeadIn) -> LeadQualification | None:
    """local_score(), or None (logged to audit_events) if Ollama is down,
    slow, or returns something that doesn't fit the schema."""
    try:
        return local_score(lead)
    except Exception as exc:  # noqa: BLE001 - any local failure just means "move on"
        logger.warning("Local scoring failed (%s): %s", type(exc).__name__, exc)
        store.log_event(
            "failed", "local_scoring", f"{type(exc).__name__}; moved on without the local model",
            lead_key=store.idempotency_key(lead),
        )
        return None


def score_lead(lead: LeadIn) -> tuple[LeadQualification, str, float]:
    """Returns (qualification, scorer_name, latency_ms).

    SCORER_MODE picks the path (see .env.example):
      claude  - Claude, else the heuristic
      local   - the local model, else the heuristic
      cascade - the local model; Claude only if needs_escalation() says so.
                If Claude then fails, the local answer stands.
    The heuristic is always the last resort, so the webhook never 500s just
    because a model is unavailable.
    """
    start = time.perf_counter()
    mode = os.environ.get("SCORER_MODE", "claude").lower()

    def done(qual: LeadQualification, scorer: str):
        return qual, scorer, (time.perf_counter() - start) * 1000

    if mode in ("local", "cascade"):
        local = _try_local(lead)
        if local is not None and (mode == "local" or not needs_escalation(local)):
            return done(local, "local")
        if mode == "cascade" and _has_claude_key():
            claude = _try_claude(lead)
            if claude is not None:
                return done(claude, "llm")
        if local is not None:
            return done(local, "local")
    elif _has_claude_key():
        claude = _try_claude(lead)
        if claude is not None:
            return done(claude, "llm")
    return done(heuristic_score(lead), "heuristic")


def _try_claude(lead: LeadIn) -> LeadQualification | None:
    """llm_score(), or None if the call failed.

    Falling back on an LLM failure is a deliberate availability choice - a
    scoring outage should degrade, not 500 the webhook n8n is waiting on.
    But degrading silently would hide exactly the kind of problem someone
    needs to know about (an expired key, a billing block, a sustained
    outage), so every failure is logged with the real exception. Anthropic's
    typed exceptions are caught most-specific-first so the log line says
    *why* it fell back, not just that it did.
    """
    import anthropic

    try:
        return llm_score(lead)
    except anthropic.AuthenticationError:
        reason = "invalid/expired API key"
        logger.error("LLM scoring failed: invalid/expired API key - falling back")
    except anthropic.RateLimitError:
        reason = "rate limited"
        logger.warning("LLM scoring rate-limited - falling back for this lead")
    except anthropic.APIStatusError as exc:
        reason = f"API error {exc.status_code}"
        logger.warning(
            "LLM scoring failed with API error %s - falling back", exc.status_code
        )
    except anthropic.APIConnectionError:
        reason = "network error"
        logger.warning("LLM scoring unreachable (network) - falling back")
    except Exception as exc:
        reason = f"unexpected {type(exc).__name__}"
        # Anything else (e.g. a response that fails LeadQualification validation) still
        # shouldn't take the webhook down, but it's unexpected enough to log with a trace.
        logger.exception("LLM scoring failed unexpectedly - falling back")
    # Only reached when the Claude call failed: every branch above falls through.
    store.log_event(
        "failed", "llm_scoring", f"{reason}; fell back to the next scorer",
        lead_key=store.idempotency_key(lead),
    )
    return None
