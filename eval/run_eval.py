"""The eval step - the thing the report says almost nobody bothers to build.

Loads data/golden_eval_set.json (hand-labeled, including deliberate trap
cases - negated urgency, vendor pitches with real-looking company names,
keyword-stuffed low-substance messages) and grades both scorers against it:
the heuristic fallback (always available) and the LLM scorer (if
ANTHROPIC_API_KEY is set). Reports per-tier accuracy, a confusion matrix,
and every miss with the reasoning that produced it - not just a pass rate.

    python -m eval.run_eval            # grades whichever scorer(s) are available
    python -m eval.run_eval --both     # forces both, skips LLM gracefully if no key
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.schema import LeadIn  # noqa: E402
from agent.scoring import heuristic_score, llm_score  # noqa: E402

GOLDEN_PATH = ROOT / "data" / "golden_eval_set.json"
RESULTS_PATH = ROOT / "eval" / "results.json"
TIERS = ["hot", "warm", "cold", "spam"]


def load_golden() -> list[dict]:
    """Each entry is {id, lead, expected_tier, expected_owner, note} - see
    data/golden_eval_set.json. `note` is what makes this more than a plain
    accuracy check: it names *why* each case is there, especially the
    deliberate traps, so a miss in the report below is immediately legible."""
    return json.loads(GOLDEN_PATH.read_text())


def grade(scorer_name: str, scorer_fn) -> dict:
    """Runs every golden case through `scorer_fn` (heuristic_score or
    llm_score) and builds a full report: overall accuracy, per-tier
    accuracy, a confusion matrix, and the list of misses with the model's
    own reasoning attached - so a wrong answer is debuggable, not just counted."""
    cases = load_golden()
    # confusion[expected_tier][actual_tier] = count. Explicitly annotated because
    # a bare `defaultdict(lambda: defaultdict(int))` doesn't give a type checker
    # enough to infer the nested value type on its own.
    confusion: defaultdict[str, defaultdict[str, int]] = defaultdict(lambda: defaultdict(int))
    misses = []
    correct = 0

    for case in cases:
        lead = LeadIn(**case["lead"])
        try:
            qual = scorer_fn(lead)
        except Exception as exc:  # noqa: BLE001 - a broken scorer on one case shouldn't abort the whole eval run
            # Recorded as a miss with its error message rather than crashing
            # the whole run - one bad case (e.g. a network blip on the LLM
            # path) shouldn't cost you the results for the other 20.
            misses.append({"id": case["id"], "error": str(exc)})
            continue

        expected = case["expected_tier"]
        actual = qual.tier
        confusion[expected][actual] += 1
        if actual == expected:
            correct += 1
        else:
            misses.append({
                "id": case["id"],
                "expected": expected,
                "actual": actual,
                "note": case.get("note", ""),
                "model_reasoning": qual.reasoning,
            })

    total = len(cases)
    # Per-tier accuracy answers a different question than the overall
    # number: "hot" cases might be much harder than "spam" cases, and that
    # would be invisible in a single blended accuracy figure.
    per_tier_accuracy = {}
    for tier in TIERS:
        tier_total = sum(confusion[tier].values())
        if not tier_total:
            continue  # no golden cases for this tier - nothing to report
        tier_correct = confusion[tier].get(tier, 0)
        per_tier_accuracy[tier] = round(tier_correct / tier_total, 3)

    return {
        "scorer": scorer_name,
        "total_cases": total,
        "accuracy": round(correct / total, 3) if total else 0.0,
        "per_tier_accuracy": per_tier_accuracy,
        # `confusion` is a defaultdict of defaultdicts, but json.dumps()
        # serializes it exactly like a plain nested dict (it's a dict
        # subclass) - no conversion needed before this goes into results.json.
        "confusion_matrix": confusion,
        "misses": misses,
    }


def print_report(report: dict) -> None:
    print(f"\n=== {report['scorer']} scorer ===")
    print(f"Overall accuracy: {report['accuracy']:.1%}  ({report['total_cases']} cases)")
    print("Per-tier accuracy:")
    for tier, acc in report["per_tier_accuracy"].items():
        print(f"  {tier:6s} {acc:.1%}")
    print("Confusion matrix (rows=expected, cols=actual):")
    header = "         " + "".join(f"{t:>8s}" for t in TIERS)
    print(header)
    for expected in TIERS:
        counts = report["confusion_matrix"].get(expected, {})
        row = "".join(f"{counts.get(t, 0):>8d}" for t in TIERS)
        print(f"  {expected:6s} {row}")
    if report["misses"]:
        print(f"\nMisses ({len(report['misses'])}):")
        for m in report["misses"]:
            if "error" in m:
                print(f"  [{m['id']}] ERROR: {m['error']}")
                continue
            print(f"  [{m['id']}] expected={m['expected']} got={m['actual']}")
            print(f"      trap: {m['note']}")
            print(f"      model said: {m['model_reasoning']}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--both", action="store_true", help="force grading both scorers")
    args = parser.parse_args()

    reports = [grade("heuristic", heuristic_score)]

    has_key = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    if has_key or args.both:
        if has_key:
            reports.append(grade("llm", llm_score))
        else:
            print(
                "\n(--both requested but no ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN set "
                "- skipping LLM grading)"
            )

    for r in reports:
        print_report(r)

    RESULTS_PATH.write_text(json.dumps({"reports": reports}, indent=2))
    print(f"\nWrote {RESULTS_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
