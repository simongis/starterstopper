"""Subprocess helper: never flashes a console window, always has a timeout."""
from __future__ import annotations

import logging
import os
import shlex
import subprocess
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Union

log = logging.getLogger(__name__)

# CREATE_NO_WINDOW: a console child of pythonw.exe would otherwise get its own visible window.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


@dataclass
class Result:
    code: int
    out: str
    err: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.code == 0 and not self.timed_out

    def tail(self, lines: int = 5) -> str:
        text = (self.err or self.out or "").strip().splitlines()
        return " | ".join(text[-lines:])


def split(cmd: str) -> List[str]:
    """Split a Windows command line the way users type it (quotes kept around paths with spaces)."""
    parts = shlex.split(cmd, posix=False)
    return [p[1:-1] if len(p) >= 2 and p[0] == p[-1] == '"' else p for p in parts]


def run(cmd: Union[str, Sequence[str]], timeout: float = 120, env: Optional[Dict[str, str]] = None,
        cwd: Optional[str] = None, log_output: bool = False) -> Result:
    """Run hidden, capture output as text (UTF-8, lenient). Never raises for a failed command."""
    args = split(cmd) if isinstance(cmd, str) else list(cmd)
    full_env = None
    if env:
        full_env = os.environ.copy()
        full_env.update(env)
    try:
        cp = subprocess.run(args, capture_output=True, timeout=timeout, env=full_env, cwd=cwd,
                            stdin=subprocess.DEVNULL, creationflags=NO_WINDOW,
                            text=True, encoding="utf-8", errors="replace")
        r = Result(cp.returncode, cp.stdout or "", cp.stderr or "")
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        r = Result(-1, out, f"timed out after {timeout:.0f} s", timed_out=True)
    except FileNotFoundError:
        r = Result(-2, "", f"not found: {args[0]}")
    except OSError as e:
        r = Result(-3, "", f"{type(e).__name__}: {e}")
    if log_output:
        for line in (r.out + r.err).splitlines():
            if line.strip():
                log.info("    | %s", line.rstrip())
    return r
