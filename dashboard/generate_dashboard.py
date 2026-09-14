"""Builds dashboard/roi_dashboard.html from the SQLite system of record.

This is the actual deliverable for project #01 in the portfolio report -
the one-page document a CFO asks an automation team for, and the thing
you screen-share in an interview. Regenerate any time the underlying data
changes:

    python -m dashboard.generate_dashboard

Every number on the page is computed from the database, not hand-typed -
including the ROI figure, which is openly built on one stated assumption
(MANUAL_MINUTES_PER_LEAD) so a skeptical reader can challenge it instead
of having to trust it.
"""
from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import store  # noqa: E402
from agent.icp import BUSINESS_NAME  # noqa: E402

OUT_PATH = ROOT / "dashboard" / "roi_dashboard.html"
EVAL_RESULTS_PATH = ROOT / "eval" / "results.json"

# Stated, editable assumption: how long a human rep spends reading, judging,
# and routing one inbound lead by hand. Swap this for a client's real number
# once you're timing their actual process - don't ship the default as fact.
MANUAL_MINUTES_PER_LEAD = 6

TIER_ORDER = ["hot", "warm", "cold", "spam"]
TIER_LABEL = {"hot": "Hot", "warm": "Warm", "cold": "Cold", "spam": "Spam"}
# Sequential ramp, single hue (brand teal), light -> dark = signal magnitude.
# hot carries the most business value, spam carries none - the ramp says so
# before you've read a single label.
TIER_COLOR = {"hot": "#0f4a42", "warm": "#1c6e63", "cold": "#7fada3", "spam": "#c9d0cb"}


def _daily_series(daily_rows: list[dict], days: int) -> list[dict]:
    """Turns get_stats()'s sparse `daily` rows (only days that actually had
    leads) into a dense, gap-free series covering every one of the last
    `days` calendar days - a day with zero leads becomes {"day": ..., "n": 0}
    instead of just not appearing. Without this, the bar chart below would
    silently compress quiet days out of the picture instead of showing them
    as short bars, which would make the volume trend look smoother (and the
    business look busier) than it really was.
    """
    by_day = {r["day"]: r["n"] for r in daily_rows}
    today = datetime.date.today()
    series = []
    for offset in range(days, 0, -1):
        day = today - datetime.timedelta(days=offset)
        key = day.isoformat()
        series.append({"day": key, "n": by_day.get(key, 0)})
    return series


def _bar_chart_svg(series: list[dict]) -> str:
    """Hand-built SVG bar chart, no charting library - the whole chart is a
    few dozen <rect> and <line> elements computed directly from `series`.
    Follows the dataviz skill's mark spec: thin bars, gently rounded ends,
    recessive hairline gridlines, and a data-day/data-n attribute on every
    bar so the page's own <script> (further down in build()) can drive a
    hover tooltip without needing a JS charting dependency either.
    """
    width, height, pad_bottom, pad_top = 720, 150, 22, 10
    plot_h = height - pad_bottom - pad_top  # the vertical space bars are actually allowed to fill
    n = len(series)
    slot = width / n  # horizontal space allotted to each day, bar + gap together
    # Cap bar width at 8px (thin marks, per the dataviz spec) but never let
    # it go below 2px even if `n` is huge - a 0-width bar would be invisible
    # and un-hoverable.
    bar_w = max(2.0, min(8.0, slot - 2))
    max_n = max((d["n"] for d in series), default=1) or 1  # the `or 1` avoids a divide-by-zero if every day is empty

    # Three gridlines - 0, the midpoint, and the max - are enough to read
    # the scale without cluttering a chart this size with more of them.
    gridlines = []
    for frac, label in [(0, "0"), (0.5, str(round(max_n / 2))), (1, str(max_n))]:
        y = pad_top + plot_h * (1 - frac)  # SVG y grows downward, so "1 - frac" flips 0-at-bottom into a y-coordinate
        gridlines.append(
            f'<line x1="0" y1="{y:.1f}" x2="{width}" y2="{y:.1f}" '
            f'stroke="var(--grid)" stroke-width="1"/>'
            f'<text x="-8" y="{y + 3:.1f}" text-anchor="end" class="axis-label">{label}</text>'
        )

    bars = []
    for i, d in enumerate(series):
        bar_h = (d["n"] / max_n) * plot_h if max_n else 0  # bar height scaled proportionally to the day's count
        x = i * slot + (slot - bar_w) / 2  # centers the (narrower) bar within its (wider) slot
        y = pad_top + plot_h - bar_h  # bars grow upward from the baseline, so y is the *top* of the bar
        bars.append(
            f'<rect class="bar" x="{x:.2f}" y="{y:.2f}" width="{bar_w:.2f}" '
            # max(bar_h, 0.6) keeps a zero-lead day visible as a hairline
            # sliver rather than vanishing completely - a day that's really
            # there but had no traffic should still be a hoverable mark.
            f'height="{max(bar_h, 0.6):.2f}" rx="2" ry="2" '
            f'data-day="{d["day"]}" data-n="{d["n"]}"/>'
        )

    return f"""
<svg viewBox="0 0 {width + 40} {height}" class="chart" role="img" aria-label="Daily inbound leads, last {n} days">
  <g transform="translate(40,0)">
    {''.join(gridlines)}
    {''.join(bars)}
  </g>
</svg>"""


def _tier_bar_svg(by_tier: dict) -> str:
    """A single 100%-stacked horizontal bar showing what fraction of leads
    landed in each tier - hot/warm/cold/spam, left to right, each segment's
    width proportional to its share of the total. TIER_COLOR (module level)
    is a sequential single-hue ramp, so the segment order alone communicates
    "most valuable to least valuable" before anyone reads a label.
    """
    width, height, gap = 720, 28, 2
    total = sum(by_tier.get(t, 0) for t in TIER_ORDER) or 1  # `or 1` avoids a divide-by-zero with no data yet
    x = 0.0  # left edge of the next segment - advances as we place each one
    segments = []
    for t in TIER_ORDER:
        n = by_tier.get(t, 0)
        seg_w = (n / total) * width  # this tier's exact proportional width
        # The visible rect is drawn slightly narrower than its proportional
        # share - that's the "2px surface gap" spec: a thin sliver of
        # background between segments so adjacent tiers read as distinct
        # without needing a border stroke around each one.
        draw_w = max(seg_w - gap, 0)
        pct = n / total * 100
        # A direct label only gets drawn if the segment is wide enough for
        # it to fit without being clipped - a narrow "spam" sliver still
        # carries its number via the legend below the chart and the hover
        # tooltip instead.
        label_fits = draw_w > 70
        segments.append(
            f'<g class="tier-seg" data-tier="{TIER_LABEL[t]}" data-n="{n}" data-pct="{pct:.1f}">'
            f'<rect x="{x:.2f}" y="0" width="{draw_w:.2f}" height="{height}" rx="2" ry="2" fill="{TIER_COLOR[t]}"/>'
            + (
                f'<text x="{x + draw_w / 2:.2f}" y="{height / 2 + 4}" text-anchor="middle" class="seg-label">'
                f'{TIER_LABEL[t]} {pct:.0f}%</text>'
                if label_fits else ""
            )
            + "</g>"
        )
        # Advances by the *proportional* width (seg_w), not the narrower
        # draw_w, so the gap comes out of each segment's own width rather
        # than accumulating as drift across the whole bar.
        x += seg_w
    return (
        f'<svg viewBox="0 0 {width} {height}" class="tier-chart" role="img" '
        f'aria-label="Lead tier distribution">{"".join(segments)}</svg>'
    )


def build() -> str:
    """Assembles the whole report as one HTML string: pulls every number
    from the database via store.get_stats(), computes the few derived
    figures (hours saved, per-week rate), and renders the two charts plus
    the stat tiles around them. Called by main() below, which just writes
    the result to disk."""
    stats = store.get_stats()
    days = 60  # the window this report covers - matches scripts/seed_history.py's own default
    series = _daily_series(stats["daily"], days)

    total = stats["total"] or 0
    by_tier = stats["by_tier"]
    hot = by_tier.get("hot", 0)
    # The one ROI number on the page, and the one built on a stated
    # assumption rather than a measurement - see MANUAL_MINUTES_PER_LEAD's
    # own comment above for why that's called out explicitly in the UI too.
    hours_saved = round(total * MANUAL_MINUTES_PER_LEAD / 60, 1)
    weeks = days / 7
    hours_per_week = round(hours_saved / weeks, 1) if weeks else 0
    avg_latency = stats["avg_latency_ms"]
    # Prefer the LLM's latency figure when any leads were LLM-scored (it's
    # the number that matters for a production deployment); fall back to
    # the heuristic's otherwise, since that's the only figure that exists yet.
    latency_val = avg_latency.get("llm", avg_latency.get("heuristic", 0))
    # Heuristic scoring runs in a fraction of a millisecond - rounding it to
    # the nearest whole ms would display as a misleading "0ms" instead of
    # the sub-millisecond truth.
    latency_display = "&lt;1" if 0 < latency_val < 1 else f"{latency_val:.0f}"
    scorer_note = (
        "Scored entirely by the heuristic fallback - no ANTHROPIC_API_KEY was set "
        "during this run. Add one to route ambiguous cases to Claude; see eval "
        "results below for exactly how much that's expected to matter."
        if "llm" not in stats["by_scorer"]
        else f"{stats['by_scorer'].get('llm', 0)} of {total} leads scored by Claude, "
             f"the rest by the heuristic fallback."
    )

    eval_html = ""
    if EVAL_RESULTS_PATH.exists():
        results = json.loads(EVAL_RESULTS_PATH.read_text())["reports"]
        rows = []
        for r in results:
            rows.append(
                f'<tr><td>{r["scorer"]}</td><td class="num">{r["accuracy"] * 100:.1f}%</td>'
                f'<td class="num">{len(r["misses"])} of {r["total_cases"]}</td></tr>'
            )
        eval_html = f"""
    <div class="card">
      <h2>Eval harness result</h2>
      <p class="muted">Graded against data/golden_eval_set.json - see eval/run_eval.py for the full miss-by-miss report.</p>
      <table class="eval-table">
        <thead><tr><th>Scorer</th><th>Accuracy</th><th>Misses</th></tr></thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
    </div>"""

    generated_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{BUSINESS_NAME} — Lead Routing ROI</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Source+Serif+4:opsz,wght@8..60,600;8..60,700&family=Public+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap">
<style>
  :root{{
    --paper:#eef1ee; --ink:#1b211f; --ink-soft:#4b544f; --ink-faint:#7c847e;
    --line:#c9d1cb; --line-soft:#dde3de; --accent:#1c6e63; --accent-ink:#0f3f38;
    --tile:#e2e8e3; --grid:#d6ddd8;
  }}
  @media (prefers-color-scheme: dark){{
    :root{{
      --paper:#12181a; --ink:#e9ede9; --ink-soft:#a3ada6; --ink-faint:#6f7973;
      --line:#2b3533; --line-soft:#232d2b; --accent:#57bfae; --accent-ink:#8fd9cc;
      --tile:#1a2220; --grid:#26312e;
    }}
  }}
  *{{box-sizing:border-box}}
  body{{background:var(--paper);color:var(--ink);font-family:"Public Sans",-apple-system,sans-serif;
       line-height:1.55;margin:0;padding:48px 24px 80px}}
  .wrap{{max-width:800px;margin:0 auto}}
  h1{{font-family:"Source Serif 4",Georgia,serif;font-size:28px;margin:0 0 4px}}
  h2{{font-family:"Source Serif 4",Georgia,serif;font-size:18px;margin:0 0 10px}}
  .eyebrow{{font-family:"IBM Plex Mono",monospace;font-size:12px;letter-spacing:.08em;
           text-transform:uppercase;color:var(--ink-faint)}}
  .muted{{color:var(--ink-soft);font-size:14px;margin:0 0 16px}}
  .card{{border:1px solid var(--line);background:var(--tile);padding:20px 24px;margin:20px 0;border-radius:2px}}

  .hero-row{{display:grid;grid-template-columns:1.2fr 1fr 1fr 1fr;gap:1px;background:var(--line);
            border:1px solid var(--line);margin:24px 0}}
  .hero-row .tile{{background:var(--tile);padding:18px 16px}}
  .hero-row .tile.primary{{background:var(--paper)}}
  .tile .label{{font-family:"IBM Plex Mono",monospace;font-size:11px;text-transform:uppercase;
               letter-spacing:.06em;color:var(--ink-faint);margin-bottom:8px}}
  .tile .value{{font-family:"Public Sans",sans-serif;font-weight:700;color:var(--accent-ink)}}
  .tile.primary .value{{font-size:40px}}
  .tile:not(.primary) .value{{font-size:26px}}
  .tile .sub{{font-size:12px;color:var(--ink-soft);margin-top:6px}}

  .chart{{width:100%;height:auto;overflow:visible}}
  .bar{{fill:var(--accent)}}
  .bar:hover{{fill:var(--accent-ink)}}
  .axis-label{{font-family:"IBM Plex Mono",monospace;font-size:10px;fill:var(--ink-faint)}}

  .tier-chart{{width:100%;height:auto;margin-top:6px}}
  .seg-label{{font-family:"IBM Plex Mono",monospace;font-size:11px;fill:#fff;font-weight:600}}
  .tier-legend{{display:flex;gap:18px;flex-wrap:wrap;margin-top:12px;font-size:13px;color:var(--ink-soft)}}
  .tier-legend span.sw{{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:middle}}

  .tooltip{{position:fixed;pointer-events:none;background:var(--ink);color:var(--paper);
           font-family:"IBM Plex Mono",monospace;font-size:12px;padding:6px 10px;border-radius:3px;
           opacity:0;transform:translate(-50%,-120%);transition:opacity .1s;z-index:10;white-space:nowrap}}
  .tooltip.show{{opacity:1}}
  .tooltip b{{color:#fff}}

  .eval-table{{width:100%;border-collapse:collapse;font-size:14px;margin-top:8px}}
  .eval-table th, .eval-table td{{padding:8px 10px;border-bottom:1px solid var(--line-soft);text-align:left}}
  .eval-table th{{font-family:"IBM Plex Mono",monospace;font-size:11px;text-transform:uppercase;
                  color:var(--ink-faint)}}
  .eval-table td.num{{font-family:"IBM Plex Mono",monospace;font-variant-numeric:tabular-nums}}

  footer{{margin-top:36px;color:var(--ink-faint);font-size:12px;border-top:1px solid var(--line);padding-top:16px}}
  .assumption{{font-size:12px;color:var(--ink-faint);margin-top:6px}}
</style>
</head>
<body>
<div class="wrap">

  <span class="eyebrow">Lead routing agent — ROI report</span>
  <h1>{BUSINESS_NAME}</h1>
  <p class="muted">Automated inbound lead qualification, {days} days of live-shaped traffic. Generated {generated_at}.</p>

  <div class="hero-row">
    <div class="tile primary">
      <div class="label">Hours saved</div>
      <div class="value">{hours_saved:,.1f}</div>
      <div class="sub">across {days} days, vs. manual triage — ~{hours_per_week:.1f} hrs/week</div>
    </div>
    <div class="tile">
      <div class="label">Leads processed</div>
      <div class="value">{total:,}</div>
      <div class="sub">{total / days:.1f} / day average</div>
    </div>
    <div class="tile">
      <div class="label">Hot leads routed</div>
      <div class="value">{hot:,}</div>
      <div class="sub">to sales-enterprise, instantly</div>
    </div>
    <div class="tile">
      <div class="label">Avg decision time</div>
      <div class="value">{latency_display}<span style="font-size:16px">ms</span></div>
      <div class="sub">vs. ~{MANUAL_MINUTES_PER_LEAD} min manual</div>
    </div>
  </div>
  <p class="assumption">Hours-saved assumes {MANUAL_MINUTES_PER_LEAD} minutes of human triage per inbound lead — edit MANUAL_MINUTES_PER_LEAD in dashboard/generate_dashboard.py to match a real client's actual process before presenting this number as fact.</p>

  <div class="card">
    <h2>Daily inbound volume — last {days} days</h2>
    <p class="muted">Every bar is one calendar day. Weekend trickle is real, not a gap in the data.</p>
    {_bar_chart_svg(series)}
  </div>

  <div class="card">
    <h2>Where leads landed</h2>
    <p class="muted">{scorer_note}</p>
    {_tier_bar_svg(by_tier)}
    <div class="tier-legend">
      {''.join(f'<span><span class="sw" style="background:{TIER_COLOR[t]}"></span>{TIER_LABEL[t]} — {by_tier.get(t,0):,} ({by_tier.get(t,0)/total*100 if total else 0:.1f}%)</span>' for t in TIER_ORDER)}
    </div>
  </div>
{eval_html}

  <footer>
    Generated by dashboard/generate_dashboard.py from lead_router.db. Every figure above is computed from stored records — none are hand-typed.
  </footer>
</div>

<div class="tooltip" id="tooltip"></div>
<script>
  const tip = document.getElementById('tooltip');
  const tipBold = document.createElement('b');
  const tipRest = document.createTextNode('');
  tip.append(tipBold, tipRest);

  // Every value below (day, count, tier name, percentage) is generated
  // server-side from fixed labels and numbers - never raw lead text - so
  // there's nothing unsafe here today. It's still built with textContent
  // rather than innerHTML on principle: a tooltip is exactly the kind of
  // place someone later adds a real field (a company name, a message
  // snippet) without thinking about it, and textContent means that change
  // can never become an HTML-injection bug by accident.
  function showTip(x, y, bold, rest) {{
    tipBold.textContent = bold;
    tipRest.textContent = rest;
    tip.style.left = x + 'px';
    tip.style.top = y + 'px';
    tip.classList.add('show');
  }}
  function hideTip() {{ tip.classList.remove('show'); }}
  function followPointer(e) {{ tip.style.left = e.clientX + 'px'; tip.style.top = e.clientY + 'px'; }}

  document.querySelectorAll('.bar').forEach(bar => {{
    bar.addEventListener('pointerenter', e => {{
      showTip(e.clientX, e.clientY, bar.dataset.n, ` leads · ${{bar.dataset.day}}`);
    }});
    bar.addEventListener('pointermove', followPointer);
    bar.addEventListener('pointerleave', hideTip);
  }});
  document.querySelectorAll('.tier-seg').forEach(seg => {{
    seg.addEventListener('pointerenter', e => {{
      showTip(e.clientX, e.clientY, seg.dataset.tier, `: ${{seg.dataset.n}} leads (${{seg.dataset.pct}}%)`);
    }});
    seg.addEventListener('pointermove', followPointer);
    seg.addEventListener('pointerleave', hideTip);
  }});
</script>
</body>
</html>"""


def main() -> None:
    OUT_PATH.write_text(build(), encoding="utf-8")
    print(f"Wrote {OUT_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
