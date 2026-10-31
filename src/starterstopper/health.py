"""Health checks: run configured commands, normalise their results, attach hints, keep reports.

No LLM is involved. Each configured check is an external command whose output is either a JSON
list of ``{check, status, detail}`` (``format: json``) or just an exit code (``format: exitcode``).
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import proc
from .config import APP_DIR, REPORT_DIR, Config, expand  # noqa: F401  (APP_DIR re-exported for callers)

log = logging.getLogger(__name__)

STATUSES = ("PASS", "WARN", "FAIL", "INFO")
KEEP_REPORTS = 20
BUILTIN_HINTS = Path(__file__).with_name("hints.json")


# ---------------------------------------------------------------------------- model


@dataclass
class CheckItem:
    source: str
    check: str
    status: str
    detail: str
    hint: str = ""


@dataclass
class CheckRun:
    name: str
    ok: bool
    exit_code: int
    duration_s: float
    items: List[CheckItem] = field(default_factory=list)
    error: str = ""


@dataclass
class HealthReport:
    started: str
    finished: str
    verdict: str                      # 'HEALTHY' | 'DEGRADED' | 'ERROR' | 'NO CHECKS'
    runs: List[CheckRun] = field(default_factory=list)
    extra: List[CheckItem] = field(default_factory=list)

    def all_items(self) -> List[CheckItem]:
        return [i for r in self.runs for i in r.items] + list(self.extra)

    def counts(self) -> Dict[str, int]:
        c = {s: 0 for s in STATUSES}
        for i in self.all_items():
            c[i.status if i.status in c else "INFO"] += 1
        return c

    def summary(self) -> str:
        """One line for a toast, e.g. ``DEGRADED: 2 FAIL, 3 WARN -- portal-licence, ...``."""
        c = self.counts()
        if self.verdict == "NO CHECKS":
            return "NO CHECKS: nothing configured"
        if self.verdict == "ERROR":
            errs = [f"{r.name}: {r.error}" for r in self.runs if r.error]
            return "ERROR: " + ("; ".join(errs) if errs else "health checks did not complete")
        parts = [f"{c[s]} {s}" for s in ("FAIL", "WARN") if c[s]]
        if self.verdict == "HEALTHY":
            parts = [f"{c['PASS']} PASS"] + parts
        text = f"{self.verdict}: " + (", ".join(parts) if parts else "no issues")
        bad = [i.check for i in self.all_items() if i.status == "FAIL"] + \
              [i.check for i in self.all_items() if i.status == "WARN"]
        if bad:
            shown = bad[:3]
            more = len(bad) - len(shown)
            text += " -- " + ", ".join(shown) + (f" (+{more} more)" if more else "")
        return text

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["counts"] = self.counts()
        d["summary"] = self.summary()
        return d


def report_from_dict(d: Dict[str, Any]) -> HealthReport:
    def item(x: Dict[str, Any]) -> CheckItem:
        return CheckItem(source=str(x.get("source", "")), check=str(x.get("check", "")),
                         status=str(x.get("status", "INFO")), detail=str(x.get("detail", "")),
                         hint=str(x.get("hint", "")))

    runs = [CheckRun(name=str(r.get("name", "")), ok=bool(r.get("ok", False)),
                     exit_code=int(r.get("exit_code", 0)), duration_s=float(r.get("duration_s", 0.0)),
                     items=[item(i) for i in r.get("items") or []], error=str(r.get("error", "")))
            for r in d.get("runs") or []]
    return HealthReport(started=str(d.get("started", "")), finished=str(d.get("finished", "")),
                        verdict=str(d.get("verdict", "ERROR")), runs=runs,
                        extra=[item(i) for i in d.get("extra") or []])


# ---------------------------------------------------------------------------- hints


def load_hints(cfg: Config) -> list:
    """User hints (``cfg.health.hintsFile``) first, then the built-in ones; first match wins."""
    hints: list = []
    if cfg.health.hintsFile:
        hints += _read_hints(Path(expand(cfg.health.hintsFile)))
    hints += _read_hints(BUILTIN_HINTS)
    return hints


def _read_hints(path: Path) -> list:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        log.warning("hints file %s unreadable: %s", path, e)
        return []
    out = []
    for h in data if isinstance(data, list) else []:
        if isinstance(h, dict) and h.get("match") and h.get("hint"):
            out.append(h)
    return out


def apply_hints(items: List[CheckItem], hints: list) -> None:
    compiled = []
    for h in hints:
        try:
            compiled.append((re.compile(h["match"], re.IGNORECASE), h.get("field", "any"),
                             {s.upper() for s in h.get("status") or ["FAIL", "WARN"]}, h["hint"]))
        except re.error as e:
            log.warning("bad hint regex %r: %s", h.get("match"), e)
    for it in items:
        if it.status not in ("WARN", "FAIL") or it.hint:
            continue
        for rx, fld, statuses, text in compiled:
            if it.status not in statuses:
                continue
            target = {"check": it.check, "detail": it.detail}.get(fld, it.check + "\n" + it.detail)
            if rx.search(target):
                it.hint = text
                break


# ---------------------------------------------------------------------------- parsing


def parse_json_items(text: str, source: str) -> Optional[List[CheckItem]]:
    """Extract check results from noisy stdout. None if nothing usable was found.

    Accepts a list of objects, a single object, or an object with a ``results`` list/object (what
    ``Test-ArcGISEnterprise.ps1 -Json`` prints). Leading noise before the JSON is skipped.
    """
    dec = json.JSONDecoder()
    for m in re.finditer(r"[\[{]", text):
        try:
            data, _ = dec.raw_decode(text[m.start():])
        except ValueError:
            continue
        rows = _rows(data)
        if rows is not None:
            return [_item(r, source) for r in rows]
    return None


def _rows(data: Any) -> Optional[List[Dict[str, Any]]]:
    if isinstance(data, list):
        rows = [r for r in data if isinstance(r, dict)]
        return rows if rows and all("check" in r or "status" in r for r in rows) else ([] if not data else None)
    if isinstance(data, dict):
        if isinstance(data.get("results"), (list, dict)):
            return _rows(data["results"] if isinstance(data["results"], list) else [data["results"]])
        if "check" in data or "status" in data:
            return [data]
    return None


def _item(r: Dict[str, Any], source: str) -> CheckItem:
    status = str(r.get("status", "")).strip().upper()
    return CheckItem(source=source, check=str(r.get("check") or r.get("name") or "(unnamed)"),
                     status=status if status in ("PASS", "WARN", "FAIL") else "INFO",
                     detail=str(r.get("detail", "") if r.get("detail") is not None else ""))


# ---------------------------------------------------------------------------- run


def _run_one(check, progress: Callable[[str], None]) -> CheckRun:
    t0 = time.monotonic()
    res = proc.run(expand(check.cmd), timeout=max(1, check.timeoutMinutes) * 60)
    dur = round(time.monotonic() - t0, 1)
    name = check.name

    if res.timed_out:
        msg = f"timed out after {check.timeoutMinutes} min"
        return CheckRun(name, False, res.code, dur, [CheckItem(name, name, "FAIL", msg)], error=msg)

    if check.format == "exitcode":
        if res.code == 0:
            item = CheckItem(name, name, "PASS", "exit code 0")
        else:
            item = CheckItem(name, name, "FAIL", f"exit code {res.code}: " + (res.tail(5) or "no output"))
        return CheckRun(name, res.code == 0, res.code, dur, [item])

    items = parse_json_items(res.out, name)
    if items is None:
        tail = res.tail(5) or (res.out.strip()[-300:] if res.out.strip() else "no output")
        msg = f"output was not valid check JSON (exit code {res.code}): {tail}"
        return CheckRun(name, False, res.code, dur, [CheckItem(name, name, "FAIL", msg)], error=msg)
    return CheckRun(name, not any(i.status == "FAIL" for i in items), res.code, dur, items)


def run_checks(cfg: Config, on_progress: Optional[Callable[[str], None]] = None,
               cancel: Optional[threading.Event] = None,
               extra: Optional[List[CheckItem]] = None) -> HealthReport:
    def progress(msg: str) -> None:
        log.info(msg)
        if on_progress:
            try:
                on_progress(msg)
            except Exception:                              # a UI callback must never break the run
                log.exception("progress callback failed")

    started = datetime.now().isoformat(timespec="seconds")
    checks = [c for c in cfg.health.checks if c.enabled]
    extra = list(extra or [])
    runs: List[CheckRun] = []
    for n, check in enumerate(checks, 1):
        if cancel is not None and cancel.is_set():
            runs.append(CheckRun("(cancelled)", False, 0, 0.0, [], error="cancelled before all checks ran"))
            break
        progress(f"Health check {n}/{len(checks)}: {check.name}")
        runs.append(_run_one(check, progress))

    hints = load_hints(cfg)
    apply_hints([i for r in runs for i in r.items] + extra, hints)

    items = [i for r in runs for i in r.items] + extra
    if any(i.status == "FAIL" for i in items):
        verdict = "DEGRADED"
    elif any(r.error and not r.items for r in runs):
        verdict = "ERROR"
    elif not checks and not extra:
        verdict = "NO CHECKS"
    else:
        verdict = "HEALTHY"
    return HealthReport(started=started, finished=datetime.now().isoformat(timespec="seconds"),
                        verdict=verdict, runs=runs, extra=extra)


# ---------------------------------------------------------------------------- persistence


def save_report(report: HealthReport) -> Tuple[Path, Path]:
    """Write health-<ts>.json/.html plus last.json/last.html; keep the newest 20 timestamped pairs."""
    from .report import render_html                         # local import: report imports this module

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    jpath = REPORT_DIR / f"health-{stamp}.json"
    hpath = REPORT_DIR / f"health-{stamp}.html"
    jtext = json.dumps(report.to_dict(), indent=2)
    htext = render_html(report)
    for p, text in ((jpath, jtext), (hpath, htext),
                    (REPORT_DIR / "last.json", jtext), (REPORT_DIR / "last.html", htext)):
        p.write_text(text, encoding="utf-8")
    stamps = sorted((p.stem for p in REPORT_DIR.glob("health-????????-??????.json")), reverse=True)
    for old in stamps[KEEP_REPORTS:]:
        for ext in (".json", ".html"):
            try:
                (REPORT_DIR / f"{old}{ext}").unlink()
            except OSError:
                pass
    return jpath, hpath


def load_last() -> Optional[HealthReport]:
    try:
        return report_from_dict(json.loads((REPORT_DIR / "last.json").read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return None
