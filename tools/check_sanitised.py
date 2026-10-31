#!/usr/bin/env python3
"""Leak-prevention check for a public repo.

Modes:
  (default)   scan files staged for commit (staged content, via `git show :path`)
  --all       scan all tracked files
  paths...    scan the given files

Blocks: RFC1918 IPv4 addresses, likely secrets, and any term from the optional
deny-list `.sanitise-deny.txt` (repo root, gitignored). Deny-list terms are never
printed in full.

Add `# sanitise: ignore` (any comment style) to a line to skip it.
Stdlib only, Python 3.9+.
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

MAX_BYTES = 2 * 1024 * 1024
IGNORE_MARK = "sanitise: ignore"
DENY_FILE = ".sanitise-deny.txt"

IPV4 = re.compile(r"(?<![\d.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\d])")
PLACEHOLDER = re.compile(
    r"^[\"']?(<|\$\{.*\}|\$env:|\$[A-Za-z_]\w*[;,)]?$|%.*%|\{\{.*\}\}|os\.environ|getenv|\*+|x+|\.\.\.)",
    re.IGNORECASE,
)
PASSWORD = re.compile(r"(?i)password\w*[\"']?\s*[:=]\s*[\"']?(\S{4,})")  # sanitise: ignore
TOKEN = re.compile(r"(?i)token[\"']?\s*=\s*[\"']?([A-Za-z0-9_\-\.]{20,})")
OTHER_RULES = [
    ("aws-access-key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),  # sanitise: ignore
    ("github-token", re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}")),
]


def mask(text, keep=2):
    if len(text) <= keep * 2 + 1:
        return text[:1] + "*" * (len(text) - 1)
    return text[:keep] + "*" * (len(text) - keep * 2) + text[-keep:]


def mask_deny(term):
    # Reveal at most the first character; never the whole term.
    return term[:1] + "*" * (len(term) - 1) if len(term) > 1 else "*"


def git(*args):
    r = subprocess.run(["git", *args], capture_output=True)
    if r.returncode != 0:
        raise RuntimeError("git %s failed: %s" % (" ".join(args), r.stderr.decode(errors="replace").strip()))
    return r.stdout


def repo_root():
    return Path(git("rev-parse", "--show-toplevel").decode().strip())


def load_deny(root):
    p = root / DENY_FILE
    terms = []
    if p.is_file():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                terms.append(line.lower())
    return terms


def is_private_ip(m):
    o = [int(x) for x in m.groups()]
    if any(x > 255 for x in o):
        return False
    a, b, c, _ = o
    if (a, b, c) in ((192, 0, 2), (198, 51, 100), (203, 0, 113)):
        return False
    return a == 10 or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168)


def scan_text(name, text, deny):
    findings = []
    for n, line in enumerate(text.splitlines(), 1):
        if IGNORE_MARK in line:
            continue
        for m in IPV4.finditer(line):
            if is_private_ip(m):
                findings.append((n, "rfc1918-ip", mask(m.group(0), 3)))
        for m in PASSWORD.finditer(line):
            val = m.group(1)
            if not PLACEHOLDER.match(val) and not re.match(r"^[\"']?[A-Z][A-Z0-9_]*[\"',]?$", val):
                findings.append((n, "password", mask(m.group(0).rstrip(",;"), 4)))
        for m in TOKEN.finditer(line):
            findings.append((n, "token", mask(m.group(0), 4)))
        for rule, rx in OTHER_RULES:
            for m in rx.finditer(line):
                findings.append((n, rule, mask(m.group(0), 4)))
        low = line.lower()
        for term in deny:
            if term in low:
                findings.append((n, "deny-list", mask_deny(term)))
    return findings


def looks_binary(data):
    return b"\x00" in data[:8192]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true", help="scan all tracked files")
    ap.add_argument("paths", nargs="*", help="scan these files instead")
    args = ap.parse_args()

    root = repo_root()
    deny = load_deny(root)
    sources = []  # (display name, loader)
    if args.paths:
        for p in args.paths:
            sources.append((p, lambda p=p: Path(p).read_bytes()))
    else:
        if args.all:
            names = git("ls-files", "-z").decode("utf-8", "replace").split("\0")
            loader = lambda n: (root / n).read_bytes()
        else:
            names = git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").decode("utf-8", "replace").split("\0")
            loader = lambda n: git("show", ":" + n)
        for n in filter(None, names):
            sources.append((n, lambda n=n, loader=loader: loader(n)))

    total = 0
    scanned = 0
    for name, load in sources:
        try:
            data = load()
        except (OSError, RuntimeError):
            continue
        if len(data) > MAX_BYTES or looks_binary(data):
            continue
        scanned += 1
        for n, rule, shown in scan_text(name, data.decode("utf-8", "replace"), deny):
            print("%s:%d: %s: %s" % (name, n, rule, shown))
            total += 1

    if total:
        print("\nFAILED: %d potential leak(s) in %d file(s) scanned." % (total, scanned), file=sys.stderr)
        return 1
    print("OK: sanitise check passed (%d file(s) scanned, %d deny-list term(s))." % (scanned, len(deny)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
