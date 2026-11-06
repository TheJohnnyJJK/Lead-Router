"""The eval step - the thing the report says almost nobody bothers to build.

Loads data/golden_eval_set.json (hand-labeled, including deliberate trap
cases - negated urgency, vendor pitches with real-looking company names,
keyword-stuffed low-substance messages) and grades every scorer against it:

    heuristic  the keyword checklist (free, always available)
    local      a small model on your own machine via Ollama (free per call)
    claude     Claude via the API (paid per call)
    cascade    local first, Claude only when needs_escalation() says so

The cascade is never run as its own pass. Each model scores each case once,
and the cascade is *simulated* from those stored answers - so the threshold
sweep at the end (how accuracy and Claude spend move as you raise the
escalation bar) costs no extra calls at all.

    python -m eval.run_eval                          # every scorer that's available
    python -m eval.run_eval --scorers heuristic,local    # free: no Claude calls
    python -m eval.run_eval --reuse-claude           # reuse Claude's answers from the last run
    python -m eval.run_eval --local-model llama3.2:3b
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# Read .env before importing agent/ - agent.store reads LEAD_ROUTER_DB at import
# time, and agent.scoring reads the model names.
load_dotenv(ROOT / ".env")

import httpx  # noqa: E402

from agent import scoring  # noqa: E402
from agent.icp import ALWAYS_ESCALATE_TIERS, ESCALATE_BELOW_CONFIDENCE  # noqa: E402
from agent.rules import needs_review  # noqa: E402
from agent.schema import LeadIn, LeadQualification  # noqa: E402

GOLDEN_PATH = ROOT / "data" / "golden_eval_set.json"
RESULTS_PATH = ROOT / "eval" / "results.json"
TIERS = ["hot", "warm", "cold", "spam"]

# $ per million tokens: (input, output, cache read, 5-minute cache write).
# From the Claude API pricing table as of 2026-10-06; cache writes are 1.25x input.
PRICES = {
    "claude-opus-5": (5.00, 25.00, 0.50, 6.25),
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20, 2.50),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
}
SWEEP = [0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 1.01]


def load_golden() -> list[dict]:
    """Each entry is {id, lead, expected_tier, expected_owner, note}. `note`
    names *why* each case is there, so a miss in the report is legible."""
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- running

def run_scorer(fn, cases: list[dict]) -> list[dict]:
    """One pass of one scorer over every case. Errors are recorded per case
    instead of aborting the run - one network blip shouldn't cost you the
    other 20 results."""
    out = []
    for case in cases:
        lead = LeadIn(**case["lead"])
        start = time.perf_counter()
        try:
            qual = fn(lead)
            row = {"qual": qual.model_dump()}
        except Exception as exc:  # noqa: BLE001
            row = {"error": f"{type(exc).__name__}: {exc}"}
        row.update(id=case["id"], expected=case["expected_tier"], note=case.get("note", ""),
                   latency_ms=round((time.perf_counter() - start) * 1000, 1))
        out.append(row)
    return out


def simulate_cascade(
    local: list[dict], claude: list[dict], threshold: float
) -> tuple[list[dict], int]:
    """What score_lead(SCORER_MODE=cascade) would have decided for each case,
    rebuilt from the two models' stored answers. Returns (results, claude_calls).
    Mirrors agent/scoring.py: escalate on low confidence or an always-escalate
    tier; if the local model failed, ask Claude; if Claude failed, keep local."""
    by_id = {r["id"]: r for r in claude}
    results, claude_calls = [], 0
    for lr in local:
        cr = by_id[lr["id"]]
        lq = lr.get("qual")
        escalate = lq is None or lq["confidence"] < threshold or lq["tier"] in ALWAYS_ESCALATE_TIERS
        if escalate:
            claude_calls += 1
            pick = cr if "qual" in cr else lr
            latency = lr["latency_ms"] + cr["latency_ms"]
        else:
            pick, latency = lr, lr["latency_ms"]
        row = {k: v for k, v in pick.items() if k != "latency_ms"}
        row.update(latency_ms=latency, escalated=escalate)
        results.append(row)
    return results, claude_calls


# ---------------------------------------------------------------- grading

def grade(name: str, results: list[dict], claude_calls: int | None = None,
          cost_per_claude_call: float | None = None) -> dict:
    """Overall and per-tier accuracy, a confusion matrix, the review gate's
    effect, Claude calls, cost per 1,000 leads, and every miss with the
    model's own reasoning attached."""
    confusion: defaultdict[str, defaultdict[str, int]] = defaultdict(lambda: defaultdict(int))
    misses, correct, reviewed, auto_total, auto_correct = [], 0, 0, 0, 0
    for r in results:
        if "error" in r:
            misses.append({"id": r["id"], "error": r["error"]})
            continue
        qual = LeadQualification(**r["qual"])
        confusion[r["expected"]][qual.tier] += 1
        if needs_review(qual):
            reviewed += 1
        else:
            auto_total += 1
            auto_correct += qual.tier == r["expected"]
        if qual.tier == r["expected"]:
            correct += 1
        else:
            misses.append({"id": r["id"], "expected": r["expected"], "actual": qual.tier,
                           "confidence": qual.confidence, "note": r["note"],
                           "model_reasoning": qual.reasoning})

    total = len(results)
    per_tier = {}
    for tier in TIERS:
        n = sum(confusion[tier].values())
        if n:
            per_tier[tier] = round(confusion[tier].get(tier, 0) / n, 3)
    cost_1k = None
    if claude_calls is not None and cost_per_claude_call is not None and total:
        cost_1k = round(claude_calls / total * cost_per_claude_call * 1000, 2)
    return {
        "scorer": name,
        "total_cases": total,
        "accuracy": round(correct / total, 3) if total else 0.0,
        "per_tier_accuracy": per_tier,
        "hot_caught": f"{confusion['hot'].get('hot', 0)}/{sum(confusion['hot'].values())}",
        "sent_to_review": reviewed,
        "auto_routed_accuracy": round(auto_correct / auto_total, 3) if auto_total else None,
        "claude_calls": claude_calls,
        "cost_per_1k_leads": cost_1k,
        "avg_latency_ms": round(sum(r["latency_ms"] for r in results) / total) if total else 0,
        "confusion_matrix": confusion,
        "misses": misses,
    }


def claude_cost_per_call(usage: list[dict]) -> float | None:
    price = PRICES.get(scoring.MODEL_ID)
    if not usage or price is None:
        return None
    p_in, p_out, p_read, p_write = price
    dollars = sum(
        u["input_tokens"] * p_in + u["output_tokens"] * p_out
        + u["cache_read_input_tokens"] * p_read + u["cache_creation_input_tokens"] * p_write
        for u in usage
    ) / 1_000_000
    return dollars / len(usage)


# ---------------------------------------------------------------- printing

def print_report(r: dict) -> None:
    print(f"\n=== {r['scorer']} ===")
    print(f"Overall accuracy: {r['accuracy']:.1%}  ({r['total_cases']} cases)   "
          f"hot caught: {r['hot_caught']}")
    auto = r["auto_routed_accuracy"]
    print(f"Sent to human review: {r['sent_to_review']}  |  accuracy on the rest: "
          + (f"{auto:.1%}" if auto is not None else "n/a (all reviewed)"))
    tiers = r["per_tier_accuracy"].items()
    print("Per-tier accuracy: " + "  ".join(f"{t} {a:.0%}" for t, a in tiers))
    print("Confusion matrix (rows=expected, cols=actual):")
    print("         " + "".join(f"{t:>8s}" for t in TIERS))
    for e in TIERS:
        counts = r["confusion_matrix"].get(e, {})
        print(f"  {e:6s} " + "".join(f"{counts.get(t, 0):>8d}" for t in TIERS))
    for m in r["misses"]:
        if "error" in m:
            print(f"  [{m['id']}] ERROR: {m['error']}")
        else:
            print(f"  [{m['id']}] expected={m['expected']} got={m['actual']} "
                  f"(conf {m['confidence']:.2f})")
            print(f"      model said: {m['model_reasoning']}")


def print_summary(reports: list[dict]) -> None:
    print("\n=== side by side ===")
    print(f"{'scorer':<22}{'accuracy':>9}{'hot':>6}{'review':>8}{'auto ok':>9}"
          f"{'claude calls':>14}{'$/1k leads':>12}{'avg ms':>8}")
    for r in reports:
        auto = r["auto_routed_accuracy"]
        calls = "-" if r["claude_calls"] is None else str(r["claude_calls"])
        cost = "-" if r["cost_per_1k_leads"] is None else f"${r['cost_per_1k_leads']:.2f}"
        auto_s = f"{auto:.0%}" if auto is not None else "n/a"
        print(f"{r['scorer']:<22}{r['accuracy']:>9.1%}{r['hot_caught']:>6}{r['sent_to_review']:>8}"
              f"{auto_s:>9}{calls:>14}{cost:>12}{r['avg_latency_ms']:>8}")


def print_sweep(rows: list[dict]) -> None:
    tiers = ", ".join(sorted(ALWAYS_ESCALATE_TIERS)) or "none"
    print(f"\n=== cascade threshold sweep (always escalated: {tiers}) ===")
    print(f"{'escalate below':>15}{'accuracy':>10}{'claude calls':>14}{'$/1k leads':>12}")
    for s in rows:
        cost = "-" if s["cost_per_1k_leads"] is None else f"${s['cost_per_1k_leads']:.2f}"
        label = "always" if s["threshold"] > 1 else f"{s['threshold']:.2f}"
        print(f"{label:>15}{s['accuracy']:>10.1%}{s['claude_calls']:>9}/{s['total']:<4}{cost:>12}")


# ---------------------------------------------------------------- main

def local_model_ready() -> str | None:
    """None if Ollama is up and has the model, else a reason it isn't."""
    try:
        tags = httpx.get(f"{scoring.OLLAMA_URL}/api/tags", timeout=3).json()
    except Exception:  # noqa: BLE001
        return f"Ollama isn't reachable at {scoring.OLLAMA_URL} (run `ollama serve`)"
    names = {m["name"] for m in tags.get("models", [])}
    if scoring.LOCAL_MODEL not in names and f"{scoring.LOCAL_MODEL}:latest" not in names:
        return f"model {scoring.LOCAL_MODEL} isn't pulled (run `ollama pull {scoring.LOCAL_MODEL}`)"
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scorers", default="heuristic,local,claude",
                        help="comma list from heuristic,local,claude "
                             "(cascade is added when local and claude both ran)")
    parser.add_argument("--local-model", help="override LOCAL_MODEL, e.g. llama3.2:3b")
    parser.add_argument("--reuse-claude", action="store_true",
                        help="take Claude's per-case answers from the last results.json "
                             "instead of calling the API")
    args = parser.parse_args()
    if args.local_model:
        scoring.LOCAL_MODEL = args.local_model
    wanted = {s.strip() for s in args.scorers.split(",") if s.strip()}
    cases = load_golden()
    runs: dict[str, list[dict]] = {}
    usage: list[dict] = []

    if "heuristic" in wanted:
        runs["heuristic"] = run_scorer(scoring.heuristic_score, cases)

    if "local" in wanted:
        problem = local_model_ready()
        if problem:
            print(f"(skipping local: {problem})")
        else:
            print(f"(warming up {scoring.LOCAL_MODEL}...)")
            # The first call loads the model into memory, which can take well over
            # LOCAL_TIMEOUT_S. Give it room, and don't count it.
            normal_timeout, scoring.LOCAL_TIMEOUT_S = scoring.LOCAL_TIMEOUT_S, 180.0
            run_scorer(scoring.local_score, cases[:1])
            scoring.LOCAL_TIMEOUT_S = normal_timeout
            runs["local"] = run_scorer(scoring.local_score, cases)

    if "claude" in wanted:
        if args.reuse_claude and RESULTS_PATH.exists():
            prev = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
            runs["claude"] = prev["runs"]["claude"]
            usage = prev.get("claude_usage", [])
            print(f"(reusing {len(runs['claude'])} Claude answers from {RESULTS_PATH.name}"
                  " - no API calls)")
        elif os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            scoring.usage_log = usage
            runs["claude"] = run_scorer(scoring.llm_score, cases)
            scoring.usage_log = None
        else:
            print("(skipping claude: no ANTHROPIC_API_KEY)")

    per_call = claude_cost_per_call(usage)
    n = len(cases)
    names = {"heuristic": "heuristic", "local": f"local ({scoring.LOCAL_MODEL})",
             "claude": f"claude ({scoring.MODEL_ID})"}
    reports = []
    for key, results in runs.items():
        calls = n if key == "claude" else 0
        cost = per_call if key == "claude" else 0.0
        reports.append(grade(names[key], results, calls, cost))

    sweep = []
    if "local" in runs and "claude" in runs:
        results, calls = simulate_cascade(runs["local"], runs["claude"], ESCALATE_BELOW_CONFIDENCE)
        reports.append(grade(f"cascade (<{ESCALATE_BELOW_CONFIDENCE})", results, calls, per_call))
        for t in SWEEP:
            res, c = simulate_cascade(runs["local"], runs["claude"], t)
            g = grade("sweep", res, c, per_call)
            sweep.append({"threshold": t, "accuracy": g["accuracy"], "claude_calls": c,
                          "total": n, "cost_per_1k_leads": g["cost_per_1k_leads"]})

    for r in reports:
        print_report(r)
    print_summary(reports)
    if sweep:
        print_sweep(sweep)
    if per_call is not None:
        print(f"\nClaude cost measured from {len(usage)} calls: ${per_call:.5f} per lead "
              f"({scoring.MODEL_ID}). Local model cost per call: $0 (your own hardware).")

    if "claude" not in runs and RESULTS_PATH.exists():
        # Keep Claude's last answers on disk so --reuse-claude still works later.
        prev = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
        if "claude" in prev.get("runs", {}):
            runs["claude"] = prev["runs"]["claude"]
            usage = prev.get("claude_usage", [])
    out = {"reports": reports, "sweep": sweep, "runs": runs, "claude_usage": usage}
    RESULTS_PATH.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nWrote {RESULTS_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
