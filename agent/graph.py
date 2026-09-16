"""The orchestration layer, built with LangGraph.

Why a graph and not a straight function call: the prefilter node lets
obviously-empty or obviously-spam submissions short-circuit before ever
reaching the LLM, which is the same cost-router instinct as project #07 in
the portfolio - don't pay frontier-model prices to learn what a two-line
heuristic already knows. Everything else routes through the real scorer.

    enrich -> prefilter -+-> (spam) -> shortcut -> persist
                          '-> (else) -> score --------> persist

Every node function below takes the current LeadState and returns a new
one - LangGraph merges each node's return value into the running state
before handing it to the next node, so a node only ever needs to return
the keys it's adding or changing, not the whole state back.
"""
from __future__ import annotations

from typing import TypedDict

from langgraph.graph import END, StateGraph

from . import store
from .icp import SPAM_KEYWORDS
from .schema import LeadIn, LeadQualification, LeadRecord
from .scoring import score_lead


class LeadState(TypedDict, total=False):
    """The graph's shared state - every node reads/writes a slice of this.
    `total=False` means no key is required upfront, since the graph fills
    them in one at a time as the lead moves through enrich -> ... -> persist."""

    lead: LeadIn
    is_free_email: bool
    message_length: int
    prefiltered: str | None
    qualification: LeadQualification
    scorer: str
    latency_ms: float
    created_at: str | None
    record: LeadRecord


def enrich_node(state: LeadState) -> LeadState:
    """First stop for every lead: derive a couple of cheap signals from the
    raw input that later nodes need (prefilter's emptiness check, and a
    free-vs-business email flag that isn't currently used downstream but is
    left here as the obvious place to add ICP-fit signals later)."""
    lead = state["lead"]
    # "user@gmail.com" -> "gmail.com"; falls back to "" for a malformed
    # address so this never raises even if upstream validation is ever loosened.
    domain = lead.email.split("@")[-1].lower() if "@" in lead.email else ""
    return {
        **state,  # keep everything already in state - only add the two new keys
        "message_length": len(lead.message.strip()),
        "is_free_email": domain in {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com"},
    }


def prefilter_node(state: LeadState) -> LeadState:
    """The free cost-saving check: catches empty messages and obvious spam
    keyword matches before anything reaches the paid scorer. Anything that
    doesn't match here is genuinely ambiguous and needs real judgment, so
    it's left for score_node instead of guessed at here.

    Scans message + company, matching heuristic_score()'s field scope
    (agent/scoring.py) - a vendor pitch that names itself in `company`
    but keeps `message` clean would otherwise reach the LLM unfiltered
    even though the cheaper heuristic path would have caught it.
    """
    lead = state["lead"]
    text = f"{lead.message} {lead.company or ''}".lower()
    if state["message_length"] == 0 or any(k in text for k in SPAM_KEYWORDS):
        return {**state, "prefiltered": "spam"}
    return {**state, "prefiltered": None}


def route_after_prefilter(state: LeadState) -> str:
    """The conditional-edge function LangGraph calls after `prefilter` to
    decide which node runs next. Must return one of the keys in the
    mapping passed to add_conditional_edges() in build_graph() below."""
    return "shortcut" if state["prefiltered"] == "spam" else "score"


def shortcut_node(state: LeadState) -> LeadState:
    """The spam branch: a deterministic, hardcoded verdict. Runs instead of
    score_node - so it never spends a model call - because prefilter_node
    already did the only reasoning this case needs."""
    qual = LeadQualification(
        tier="spam", icp_fit_score=0, urgency_score=0,
        suggested_owner="discard", confidence=0.9,
        reasoning="Prefiltered before scoring: empty message or spam keyword match.",
    )
    return {**state, "qualification": qual, "scorer": "heuristic", "latency_ms": 0.0}


def score_node(state: LeadState) -> LeadState:
    """The real-judgment branch: hands the lead to score_lead(), which
    itself decides LLM vs. heuristic and times the call - this node just
    threads that result into the graph's state."""
    qual, scorer, latency_ms = score_lead(state["lead"])
    return {**state, "qualification": qual, "scorer": scorer, "latency_ms": latency_ms}


def persist_node(state: LeadState) -> LeadState:
    """Last stop for every branch: write the decision to the system of
    record (agent/store.py) and attach the resulting LeadRecord - complete
    with its new database id - back onto the state so run_lead() can
    return it to the caller."""
    lead, qual = state["lead"], state["qualification"]
    record = store.insert_lead(
        lead, qual, state["scorer"], state["latency_ms"], created_at=state.get("created_at")
    )
    return {**state, "record": record}


def build_graph():
    """Wires the five nodes above into the graph pictured in the module
    docstring and compiles it into a runnable object. Called once - see
    the module-level cache in run_lead() below."""
    graph = StateGraph(LeadState)
    graph.add_node("enrich", enrich_node)
    graph.add_node("prefilter", prefilter_node)
    graph.add_node("shortcut", shortcut_node)
    graph.add_node("score", score_node)
    graph.add_node("persist", persist_node)

    graph.set_entry_point("enrich")
    graph.add_edge("enrich", "prefilter")
    # The one conditional edge in this graph: prefilter's output decides
    # whether the lead goes to the free shortcut or the real scorer.
    graph.add_conditional_edges(
        "prefilter", route_after_prefilter, {"shortcut": "shortcut", "score": "score"}
    )
    # Both branches converge on the same persist step, then the graph ends.
    graph.add_edge("shortcut", "persist")
    graph.add_edge("score", "persist")
    graph.add_edge("persist", END)
    return graph.compile()


# Built once per process, not once per request: compiling a LangGraph graph
# does real work (validating nodes/edges), so re-doing it on every lead
# would waste time for no benefit - the compiled graph is stateless and
# safe to reuse across calls.
_compiled_graph = None


def run_lead(lead: LeadIn, created_at: str | None = None) -> LeadRecord:
    """The public entry point everything else in the project calls:
    agent/main.py's /qualify route, scripts/seed_history.py, and
    eval/run_eval.py all go through this one function.

    `created_at` is normally left as None (persist_node/store.insert_lead
    then stamp "now") - seed_history.py is the one caller that passes an
    explicit backdated timestamp, to build 60 days of realistic history.
    """
    global _compiled_graph
    if _compiled_graph is None:
        _compiled_graph = build_graph()
    result: LeadState = _compiled_graph.invoke({"lead": lead, "created_at": created_at})
    return result["record"]
