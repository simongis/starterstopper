"""Render a HealthReport as one self-contained HTML page (inline CSS, no JS, no external resources)."""
from __future__ import annotations

from html import escape
from typing import List

from .health import CheckItem, HealthReport

_CSS = """
:root{--bg:#f6f7f9;--panel:#fff;--text:#1d2329;--muted:#5d6874;--line:#d9dee4;
--pass:#1b7f3b;--pass-bg:#e3f4e8;--warn:#8a5a00;--warn-bg:#fff1cf;--fail:#b3261e;--fail-bg:#fde6e4;
--info:#2a5d9f;--info-bg:#e4eefb;--hint-bg:#eef2f6}
@media (prefers-color-scheme:dark){:root{--bg:#14181c;--panel:#1d2329;--text:#e6e9ed;--muted:#9aa5b1;--line:#333c45;
--pass:#6fd08c;--pass-bg:#16301f;--warn:#f0c15a;--warn-bg:#3a2e10;--fail:#ff8b82;--fail-bg:#3d1b19;
--info:#8db8f0;--info-bg:#1a2b42;--hint-bg:#262e36}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:900px;margin:0 auto;padding:16px}
h1{font-size:1.35rem;margin:0 0 8px}
h2{font-size:1.05rem;margin:24px 0 8px}
.head{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:14px}
.badge{display:inline-block;padding:3px 12px;border-radius:999px;font-weight:700;letter-spacing:.03em}
.b-HEALTHY{background:var(--pass-bg);color:var(--pass)}
.b-DEGRADED,.b-ERROR{background:var(--fail-bg);color:var(--fail)}
.b-NOCHECKS{background:var(--info-bg);color:var(--info)}
.muted{color:var(--muted);font-size:.9rem}
.counts{margin-top:8px;display:flex;flex-wrap:wrap;gap:6px}
.pill{padding:2px 10px;border-radius:6px;font-weight:600;font-size:.9rem}
.s-PASS{background:var(--pass-bg);color:var(--pass)}
.s-WARN{background:var(--warn-bg);color:var(--warn)}
.s-FAIL{background:var(--fail-bg);color:var(--fail)}
.s-INFO{background:var(--info-bg);color:var(--info)}
.item{background:var(--panel);border:1px solid var(--line);border-left-width:5px;border-radius:6px;
padding:8px 10px;margin:6px 0;overflow-wrap:anywhere}
.item.i-FAIL{border-left-color:var(--fail)}.item.i-WARN{border-left-color:var(--warn)}
.item.i-PASS{border-left-color:var(--pass)}.item.i-INFO{border-left-color:var(--info)}
.item .t{display:flex;flex-wrap:wrap;gap:8px;align-items:baseline}
.item .n{font-weight:600}
.item .d{margin-top:4px;font-family:ui-monospace,Consolas,monospace;font-size:.85rem;white-space:pre-wrap}
.hint{margin-top:6px;background:var(--hint-bg);border-radius:6px;padding:6px 8px;font-size:.9rem}
details{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:6px 10px;margin:6px 0}
summary{cursor:pointer;font-weight:600}
table{width:100%;border-collapse:collapse;font-size:.9rem}
td,th{text-align:left;padding:4px 6px;border-bottom:1px solid var(--line);vertical-align:top}
.err{color:var(--fail)}
"""


def _item_html(i: CheckItem) -> str:
    st = escape(i.status)
    hint = f'<div class="hint"><b>Hint:</b> {escape(i.hint)}</div>' if i.hint else ""
    detail = f'<div class="d">{escape(i.detail)}</div>' if i.detail else ""
    return (f'<div class="item i-{st}"><div class="t"><span class="pill s-{st}">{st}</span>'
            f'<span class="n">{escape(i.check)}</span><span class="muted">{escape(i.source)}</span></div>'
            f'{detail}{hint}</div>')


def _section(title: str, items: List[CheckItem]) -> str:
    if not items:
        return ""
    return f"<h2>{escape(title)} ({len(items)})</h2>" + "".join(_item_html(i) for i in items)


def render_html(report: HealthReport) -> str:
    items = report.all_items()
    counts = report.counts()
    fails = [i for i in items if i.status == "FAIL"]
    warns = [i for i in items if i.status == "WARN"]
    infos = [i for i in items if i.status == "INFO"]
    passes = [i for i in items if i.status == "PASS"]

    badge_cls = "b-" + report.verdict.replace(" ", "")
    pills = "".join(f'<span class="pill s-{s}">{counts[s]} {s}</span>' for s in ("FAIL", "WARN", "PASS", "INFO"))
    parts = [
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        "<title>StarterStopper health report</title>",
        f"<style>{_CSS}</style></head><body><main>",
        '<div class="head"><h1>StarterStopper health report</h1>',
        f'<span class="badge {escape(badge_cls)}">{escape(report.verdict)}</span> ',
        f'<span class="muted">{escape(report.started)} to {escape(report.finished)}</span>',
        f'<div class="counts">{pills}</div></div>',
        _section("Failures", fails),
        _section("Warnings", warns),
        _section("Information", infos),
    ]
    if passes:
        parts.append(f"<h2>Passed</h2><details><summary>{len(passes)} passed checks</summary>"
                     + "".join(_item_html(i) for i in passes) + "</details>")

    if report.runs:
        parts.append("<h2>Check runs</h2>")
        for r in report.runs:
            rc = {s: sum(1 for i in r.items if i.status == s) for s in ("FAIL", "WARN", "PASS", "INFO")}
            err = f'<div class="err">{escape(r.error)}</div>' if r.error else ""
            parts.append(
                f"<details><summary>{escape(r.name)}</summary>"
                f'<table><tr><th>Exit code</th><td>{r.exit_code}</td></tr>'
                f"<tr><th>Duration</th><td>{r.duration_s:.1f} s</td></tr>"
                f"<tr><th>Results</th><td>{rc['FAIL']} FAIL, {rc['WARN']} WARN, {rc['PASS']} PASS, "
                f"{rc['INFO']} INFO</td></tr></table>{err}</details>")
    parts.append("</main></body></html>")
    return "".join(parts)
