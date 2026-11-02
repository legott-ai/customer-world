"""Aggregate-only, self-contained research reports."""
from __future__ import annotations

from html import escape
from pathlib import Path
from typing import Any


METRICS = (
    ("prepared", "Prepared source contexts"),
    ("generated_profiles", "Generated profiles"),
    ("runtime_contexts", "Customers with generated daily contexts"),
    ("autonomous_decisions", "Recorded autonomous choices"),
    ("primary_product_ui_visits", "Product visits with operator evidence"),
    ("alternative_tool_visits", "Alternative visits with operator evidence"),
    ("voluntary_nonvisits", "Voluntary nonvisits"),
    ("environment_censored", "Environment-censored sessions"),
    ("errors", "Customers with environment errors"),
    ("completed", "Customers with completed sessions"),
    ("pending", "Customers pending completion"),
    ("actor_days", "Recorded customer-days"),
)
DECISIONS = ("continue", "try_again", "keep_incumbent", "stop", "undecided")


def aggregate_view(summary: dict[str, Any]) -> dict[str, Any]:
    """Export only bounded counts and the date, never participant content."""
    result: dict[str, Any] = {}
    for key, _ in METRICS:
        value = summary.get(key, 0)
        if isinstance(value, bool):
            raise ValueError("Report metrics must be non-negative integer counts")
        if isinstance(value, (list, dict)):
            value = len(value)
        if not isinstance(value, int) or value < 0:
            raise ValueError("Report metrics must be non-negative integer counts")
        result[key] = value
    day = summary.get("day")
    result["day"] = str(day) if day is not None else "All observed dates"
    decisions = summary.get("intent_counts", summary.get("decisions", {}))
    if not isinstance(decisions, dict):
        decisions = {}
    result["decisions"] = {}
    for key in DECISIONS:
        count = decisions.get(key, 0)
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError("Decision metrics must be non-negative integer counts")
        result["decisions"][key] = count
    return result


def render_report(summary: dict[str, Any]) -> str:
    data = aggregate_view(summary)
    cards = "".join(
        f'<article><strong>{data[key]}</strong><span>{escape(label)}</span></article>'
        for key, label in METRICS
    )
    rows = "".join(
        f'<tr><td>{escape(key)}</td><td>{count}</td></tr>'
        for key, count in data["decisions"].items()
    )
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>CustomerWorld research report</title>
<style>
*{{box-sizing:border-box}}body{{margin:0;background:#f5f4ef;color:#233631;font:17px/1.6 system-ui,sans-serif}}
main{{max-width:1160px;margin:auto;padding:64px 24px}}h1{{font-size:clamp(34px,6vw,62px);letter-spacing:-.05em;line-height:1.12}}
.eyebrow{{letter-spacing:.12em;font-size:12px;color:#397466}}.grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}}
article{{background:white;border:1px solid #d9e1da;border-radius:12px;padding:24px}}strong{{display:block;font-size:40px}}span{{font-size:15px}}
.note{{padding:20px;border-left:4px solid #397466;background:#e7eee8;margin:28px 0}}table{{border-collapse:collapse;width:100%;max-width:620px}}
td,th{{padding:12px;text-align:left;border-bottom:1px solid #d9e1da}}h2{{margin-top:44px}}.muted{{color:#5d6e66}}
@media(max-width:700px){{.grid{{grid-template-columns:repeat(2,1fr)}}main{{padding:30px 16px}}}}
</style></head><body><main><div class="eyebrow">CUSTOMERWORLD / RESEARCH REPORT</div>
<h1>Customer choices,<br>with an evidence trail.</h1>
<p>{escape(data['day'])}</p>
<div class="note">Separate counts for prepared contexts, completed generation, observed tool use and environment failures. Product visits require operator-supplied UI evidence.</div>
<section class="grid">{cards}</section>
<h2>Declared future intentions</h2><table><thead><tr><th>Decision</th><th>Count</th></tr></thead><tbody>{rows}</tbody></table>
<p class="muted">Intentions are self-reports from synthetic participants. They do not establish human retention or adoption. Reports across dates count distinct identities in each category. Categories can overlap across dates; customer-days count repeated sessions. Use dated reports when comparing intentions.</p>
<h2>Export scope</h2><p>This report contains aggregate counts only. This export excludes participant sources, memories, writing, screenshots and model credentials.</p>
</main></body></html>'''


def write_report(summary: dict[str, Any], output: Path, *, force: bool = False) -> Path:
    output = Path(output)
    html = render_report(summary)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w" if force else "x", encoding="utf-8") as handle:
        handle.write(html)
    return output
