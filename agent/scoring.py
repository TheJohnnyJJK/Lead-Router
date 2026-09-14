"""Two ways to turn a LeadIn into a LeadQualification.

heuristic_score() is deterministic, free, and has no external dependency -
it's what the golden eval set is graded against when there's no API key,
and it's the automatic fallback if the LLM call errors. llm_score() is the
real reasoning path: Claude reads the ICP + routing rubric and returns a
structured LeadQualification via client.messages.parse().

score_lead() is the single entry point the graph calls - it picks LLM when
available and healthy, heuristic otherwise, and always returns which one
actually ran so that's visible in the stored record and the dashboard.
"""
from __future__ import annotations

import logging
import os
import time

from .icp import (
    BUSINESS_NAME,
    FREE_EMAIL_DOMAINS,
    HOT_KEYWORDS,
    ICP_DESCRIPTION,
    RESIDENTIAL_KEYWORDS,
    ROUTING_RUBRIC,
    SPAM_KEYWORDS,
)
from .schema import LeadIn, LeadQualification

logger = logging.getLogger(__name__)

MODEL_ID = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5")

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
        output_format=LeadQualification,
    )
    # parsed_output is Optional: a refusal or a response that hits max_tokens
    # before finishing its JSON can both leave it unset. Treating that as a
    # hard failure (instead of silently returning None as if it were a real
    # qualification) is what lets score_lead()'s except-chain catch it and
    # fall back to the heuristic scorer, logged, rather than crashing later
    # with a confusing "NoneType has no attribute .tier".
    if response.parsed_output is None:
        raise ValueError(
            f"Claude returned no parsed output (stop_reason={response.stop_reason!r})"
        )
    return response.parsed_output


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


def score_lead(lead: LeadIn) -> tuple[LeadQualification, str, float]:
    """Returns (qualification, scorer_name, latency_ms).

    Falling back to the heuristic scorer on an LLM failure is a deliberate
    availability choice - a scoring outage should degrade, not 500 the
    webhook n8n is waiting on. But degrading silently would hide exactly
    the kind of problem someone needs to know about (an expired key, a
    billing block, a sustained outage), so every fallback is logged with
    the real exception before we ever call heuristic_score(). Anthropic's
    typed exceptions are caught most-specific-first so the log line says
    *why* it fell back, not just that it did.
    """
    import anthropic

    start = time.perf_counter()
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        try:
            qual = llm_score(lead)
            return qual, "llm", (time.perf_counter() - start) * 1000
        except anthropic.AuthenticationError:
            logger.error("LLM scoring failed: invalid/expired API key - falling back to heuristic")
        except anthropic.RateLimitError:
            logger.warning("LLM scoring rate-limited - falling back to heuristic for this lead")
        except anthropic.APIStatusError as exc:
            logger.warning(
                "LLM scoring failed with API error %s - falling back to heuristic", exc.status_code
            )
        except anthropic.APIConnectionError:
            logger.warning("LLM scoring unreachable (network) - falling back to heuristic")
        except Exception:
            # Anything else (e.g. a response that fails LeadQualification validation) still
            # shouldn't take the webhook down, but it's unexpected enough to log with a trace.
            logger.exception("LLM scoring failed unexpectedly - falling back to heuristic")
    qual = heuristic_score(lead)
    return qual, "heuristic", (time.perf_counter() - start) * 1000
