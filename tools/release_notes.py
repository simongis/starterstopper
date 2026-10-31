"""Release helpers used by the release workflow (and runnable locally).

    python tools/release_notes.py check v1.0.0      # tag matches __version__ and CHANGELOG has the section
    python tools/release_notes.py notes v1.0.0      # print that version's CHANGELOG section
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def version() -> str:
    text = (ROOT / "src" / "starterstopper" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.M)
    if not m:
        raise SystemExit("__version__ not found in src/starterstopper/__init__.py")
    return m.group(1)


def section(ver: str) -> str:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    m = re.search(rf"^## \[{re.escape(ver)}\][^\n]*\n(.*?)(?=^## \[|^\[[^\]]+\]: |\Z)", text, re.M | re.S)
    if not m or not m.group(1).strip():
        raise SystemExit(f"CHANGELOG.md has no (non-empty) section for [{ver}]")
    return m.group(1).strip()


def main(argv: list) -> int:
    if len(argv) != 2 or argv[0] not in ("check", "notes"):
        print(__doc__)
        return 2
    tag = argv[1]
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise SystemExit(f"tag '{tag}' is not vMAJOR.MINOR.PATCH")
    ver = tag[1:]
    if argv[0] == "check":
        if version() != ver:
            raise SystemExit(f"tag {tag} does not match __version__ {version()}")
        section(ver)
        print(f"OK: {tag} matches __version__ and CHANGELOG.md")
        return 0
    print(section(ver))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
