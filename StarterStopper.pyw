"""Launcher. Run with pythonw.exe for no console window (the Startup shortcut does this)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from starterstopper.__main__ import main  # noqa: E402

sys.exit(main())
