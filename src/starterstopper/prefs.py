"""Preferences window (tkinter). Runs in its own short-lived process: ``pythonw -m starterstopper --prefs``.

The resident tray never imports this module, so tkinter is only loaded here. ``main()`` owns the
Tk mainloop and returns 0 if the config was saved, 1 if the window was cancelled.
"""
from __future__ import annotations

import copy
import json
import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import config
from .config import (APP_DIR, CONFIG_PATH, HEALTH_FORMATS, HOOK_PHASES, SUSPENDED_ACTIONS, SYSTEM_TYPES, Config,
                     ConfigError, HealthCheck, Hook, Link, Probe, System)

PAD = 4
# from_dict can raise plain ValueError/TypeError/AttributeError on wrongly-typed values.
LOAD_ERRORS = (ConfigError, ValueError, TypeError, AttributeError, KeyError)


# ---------------------------------------------------------------------------- helpers


def _int(text: str, label: str, minimum: Optional[int] = None) -> int:
    """Parse a whole number from a form field; ValueError carries a user-facing message."""
    try:
        v = int(str(text).strip())
    except ValueError:
        raise ValueError(f"{label}: '{text}' is not a whole number") from None
    if minimum is not None and v < minimum:
        raise ValueError(f"{label}: must be {minimum} or more")
    return v


def _browse_file(var: tk.StringVar, title: str, types: Sequence[Tuple[str, str]]) -> None:
    current = var.get().strip()
    initial = os.path.dirname(os.path.expanduser(current)) if current else None
    chosen = filedialog.askopenfilename(title=title, filetypes=list(types) + [("All files", "*.*")],
                                        initialdir=initial or None)
    if chosen:
        var.set(os.path.normpath(chosen))


def _spin(parent: tk.Widget, var: tk.StringVar, lo: int = 0, hi: int = 100000, width: int = 8) -> ttk.Spinbox:
    return ttk.Spinbox(parent, from_=lo, to=hi, textvariable=var, width=width)


class PlaceholderEntry(ttk.Entry):
    """Entry that shows grey hint text while empty and unfocused. Read with ``value()``."""

    def __init__(self, parent: tk.Widget, placeholder: str, **kw: Any) -> None:
        super().__init__(parent, **kw)
        self._ph = placeholder
        self._showing = False
        self.bind("<FocusIn>", lambda e: self._hide())
        self.bind("<FocusOut>", lambda e: self._show())
        self._show()

    def _show(self) -> None:
        if not self.get():
            self.insert(0, self._ph)
            self.configure(foreground="grey")
            self._showing = True

    def _hide(self) -> None:
        if self._showing:
            self.delete(0, "end")
            self.configure(foreground="")
            self._showing = False

    def value(self) -> str:
        return "" if self._showing else self.get().strip()

    def set_value(self, text: str) -> None:
        self._hide()
        self.delete(0, "end")
        self.insert(0, text)
        if self.focus_get() is not self:
            self._show()


# ---------------------------------------------------------------------------- table editor


class ListEditor(ttk.Frame):
    """Treeview over a Python list (mutated in place) with Add/Edit/Remove/Up/Down buttons.

    ``edit(item_or_None)`` opens a dialog and returns the new item, or None if cancelled.
    """

    def __init__(self, parent: tk.Widget, columns: Sequence[Tuple[str, str, int]],
                 row: Callable[[Any], Sequence[Any]], edit: Callable[[Any], Any],
                 on_change: Optional[Callable[[], None]] = None,
                 extra: Sequence[Tuple[str, Callable[[], None]]] = (), noun: str = "item",
                 height: int = 6, updown: bool = True) -> None:
        super().__init__(parent)
        self.items: List[Any] = []
        self._row, self._edit, self._on_change, self._noun = row, edit, on_change, noun
        self.tree = ttk.Treeview(self, columns=[c[0] for c in columns], show="headings",
                                 selectmode="browse", height=height)
        for key, heading, width in columns:
            self.tree.heading(key, text=heading, anchor="w")
            self.tree.column(key, width=width, stretch=True, anchor="w")
        sb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        btns = ttk.Frame(self)
        btns.grid(row=0, column=2, sticky="n", padx=(PAD, 0))
        spec = [("Add...", self.add), ("Edit...", self.edit_selected), ("Remove", self.remove)]
        if updown:
            spec += [("Up", lambda: self.move(-1)), ("Down", lambda: self.move(1))]
        for text, cmd in list(spec) + list(extra):
            ttk.Button(btns, text=text, command=cmd, width=10).pack(fill="x", pady=1)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.tree.bind("<Double-1>", lambda e: self.edit_selected())

    def set_items(self, items: List[Any]) -> None:
        self.items = items
        self.refresh()

    def refresh(self, select: Optional[int] = None) -> None:
        self.tree.delete(*self.tree.get_children())
        for i, item in enumerate(self.items):
            self.tree.insert("", "end", iid=str(i), values=[str(v) for v in self._row(item)])
        if select is not None and 0 <= select < len(self.items):
            self.tree.selection_set(str(select))
            self.tree.see(str(select))

    def selected_index(self) -> Optional[int]:
        sel = self.tree.selection()
        return int(sel[0]) if sel else None

    def selected(self) -> Any:
        i = self.selected_index()
        return None if i is None else self.items[i]

    def _changed(self, select: Optional[int]) -> None:
        self.refresh(select)
        if self._on_change:
            self._on_change()

    def add(self) -> None:
        item = self._edit(None)
        if item is not None:
            self.items.append(item)
            self._changed(len(self.items) - 1)

    def edit_selected(self) -> None:
        i = self.selected_index()
        if i is None:
            return
        item = self._edit(copy.deepcopy(self.items[i]))
        if item is not None:
            self.items[i] = item
            self._changed(i)

    def remove(self) -> None:
        i = self.selected_index()
        if i is not None and messagebox.askyesno("Remove", f"Remove the selected {self._noun}?", parent=self):
            del self.items[i]
            self._changed(min(i, len(self.items) - 1))

    def move(self, delta: int) -> None:
        i = self.selected_index()
        j = None if i is None else i + delta
        if i is None or j is None or not 0 <= j < len(self.items):
            return
        self.items[i], self.items[j] = self.items[j], self.items[i]
        self._changed(j)


# ---------------------------------------------------------------------------- dialogs


class _Dialog(tk.Toplevel):
    """Modal OK/Cancel dialog. Subclasses add rows in ``__init__`` and implement ``build()``."""

    def __init__(self, parent: tk.Widget, title: str) -> None:
        super().__init__(parent)
        self.title(title)
        self.transient(parent.winfo_toplevel())
        self.result: Any = None
        self.body = ttk.Frame(self, padding=10)
        self.body.pack(fill="both", expand=True)
        self.body.columnconfigure(1, weight=1)
        bar = ttk.Frame(self, padding=(10, 0, 10, 10))
        bar.pack(fill="x")
        ttk.Button(bar, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(bar, text="OK", command=self._ok).pack(side="right", padx=PAD)
        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self.destroy())
        self._row = 0
        self.minsize(460, 0)

    def add_row(self, label: str, widget: tk.Widget, extra: Optional[tk.Widget] = None) -> None:
        ttk.Label(self.body, text=label).grid(row=self._row, column=0, sticky="w", padx=(0, PAD), pady=2)
        widget.grid(row=self._row, column=1, sticky="ew", pady=2)
        if extra is not None:
            extra.grid(row=self._row, column=2, sticky="w", padx=(PAD, 0))
        self._row += 1

    def build(self) -> Any:  # pragma: no cover - overridden
        raise NotImplementedError

    def _ok(self) -> None:
        try:
            self.result = self.build()
        except ValueError as e:
            messagebox.showerror("Invalid value", str(e), parent=self)
            return
        self.destroy()

    def show(self) -> Any:
        self.wait_visibility()
        self.grab_set()
        self.wait_window()
        return self.result


class ProbeDialog(_Dialog):
    def __init__(self, parent: tk.Widget, probe: Optional[Probe]) -> None:
        super().__init__(parent, "Probe")
        self._orig = probe or Probe()
        p = self._orig
        self.kind = tk.StringVar(value=p.kind if probe else "http")
        self.value = tk.StringVar(value=p.http or p.ssh or "")
        self.label = tk.StringVar(value=p.label or "")
        self.expect = tk.StringVar(value=", ".join(str(c) for c in p.expect))
        self.verify = tk.BooleanVar(value=p.verifyTls)
        self.timeout = tk.StringVar(value=str(p.timeoutSeconds))
        kinds = ttk.Frame(self.body)
        ttk.Radiobutton(kinds, text="HTTP(S) URL", value="http", variable=self.kind,
                        command=self._kind_changed).pack(side="left")
        ttk.Radiobutton(kinds, text="SSH command (pass = exit 0)", value="ssh", variable=self.kind,
                        command=self._kind_changed).pack(side="left", padx=(10, 0))
        self.add_row("Kind", kinds)
        self.add_row("URL / command", ttk.Entry(self.body, textvariable=self.value, width=60))
        self.add_row("Label", ttk.Entry(self.body, textvariable=self.label))
        self._expect_e = ttk.Entry(self.body, textvariable=self.expect)
        self.add_row("Expect codes", self._expect_e, ttk.Label(self.body, text="comma-separated"))
        self._verify_c = ttk.Checkbutton(self.body, text="Verify TLS certificate", variable=self.verify)
        self.add_row("", self._verify_c)
        self.add_row("Timeout (s)", _spin(self.body, self.timeout, 1, 600))
        ttk.Label(self.body, text="Placeholders: {host} {name} {app} {config_dir}", foreground="grey") \
            .grid(row=self._row, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self._kind_changed()

    def _kind_changed(self) -> None:
        state = "normal" if self.kind.get() == "http" else "disabled"
        self._expect_e.configure(state=state)
        self._verify_c.configure(state=state)

    def build(self) -> Probe:
        value = self.value.get().strip()
        if not value:
            raise ValueError("URL / command is required")
        timeout = _int(self.timeout.get(), "Timeout", 1)
        if self.kind.get() == "http":
            parts = [x for x in self.expect.get().replace(";", ",").split(",") if x.strip()]
            expect = [_int(x, "Expect codes", 100) for x in parts] or [200]
            return Probe(http=value, label=self.label.get().strip() or None, expect=expect,
                         verifyTls=self.verify.get(), timeoutSeconds=timeout)
        return Probe(ssh=value, label=self.label.get().strip() or None, expect=list(self._orig.expect),
                     verifyTls=self._orig.verifyTls, timeoutSeconds=timeout)


class HookDialog(_Dialog):
    def __init__(self, parent: tk.Widget, item: Optional[Tuple[str, Hook]]) -> None:
        super().__init__(parent, "Hook")
        phase, h = item or ("afterReady", Hook())
        self.phase = tk.StringVar(value=phase)
        self.cmd = tk.StringVar(value=h.cmd)
        self.required = tk.BooleanVar(value=h.required)
        self.timeout = tk.StringVar(value=str(h.timeoutMinutes))
        self.add_row("Phase", ttk.Combobox(self.body, textvariable=self.phase, values=HOOK_PHASES, state="readonly"))
        self.add_row("Command", ttk.Entry(self.body, textvariable=self.cmd, width=70))
        self.add_row("", ttk.Checkbutton(self.body, text="Required (non-zero exit fails the system; "
                                                         "otherwise warn)", variable=self.required))
        self.add_row("Timeout (min)", _spin(self.body, self.timeout, 1, 1440))

    def build(self) -> Tuple[str, Hook]:
        if not self.cmd.get().strip():
            raise ValueError("Command is required")
        return self.phase.get(), Hook(cmd=self.cmd.get().strip(), required=self.required.get(),
                                      timeoutMinutes=_int(self.timeout.get(), "Timeout", 1))


class CheckDialog(_Dialog):
    def __init__(self, parent: tk.Widget, check: Optional[HealthCheck]) -> None:
        super().__init__(parent, "Health check")
        c = check or HealthCheck()
        self.name = tk.StringVar(value=c.name)
        self.cmd = tk.StringVar(value=c.cmd)
        self.format = tk.StringVar(value=c.format)
        self.timeout = tk.StringVar(value=str(c.timeoutMinutes))
        self.enabled = tk.BooleanVar(value=c.enabled)
        self.add_row("Name", ttk.Entry(self.body, textvariable=self.name))
        self.add_row("Command", ttk.Entry(self.body, textvariable=self.cmd, width=90))
        self.add_row("Format", ttk.Combobox(self.body, textvariable=self.format, values=HEALTH_FORMATS,
                                            state="readonly"))
        self.add_row("Timeout (min)", _spin(self.body, self.timeout, 1, 1440))
        self.add_row("", ttk.Checkbutton(self.body, text="Enabled", variable=self.enabled))

    def build(self) -> HealthCheck:
        if not self.name.get().strip() or not self.cmd.get().strip():
            raise ValueError("Name and command are required")
        return HealthCheck(name=self.name.get().strip(), cmd=self.cmd.get().strip(), format=self.format.get(),
                           timeoutMinutes=_int(self.timeout.get(), "Timeout", 1), enabled=self.enabled.get())


class LinkDialog(_Dialog):
    def __init__(self, parent: tk.Widget, link: Optional[Link]) -> None:
        super().__init__(parent, "Link")
        lk = link or Link()
        self.label = tk.StringVar(value=lk.label)
        self.url = tk.StringVar(value=lk.url)
        self.add_row("Label", ttk.Entry(self.body, textvariable=self.label))
        self.add_row("URL", ttk.Entry(self.body, textvariable=self.url, width=60))

    def build(self) -> Link:
        if not self.url.get().strip():
            raise ValueError("URL is required")
        return Link(label=self.label.get().strip(), url=self.url.get().strip())


class _ChoiceDialog(tk.Toplevel):
    """Message plus a row of buttons; ``show()`` returns the chosen key (None if closed)."""

    def __init__(self, parent: tk.Misc, title: str, message: str, choices: Sequence[Tuple[str, str]]) -> None:
        super().__init__(parent)
        self.title(title)
        self.choice: Optional[str] = None
        ttk.Label(self, text=message, wraplength=520, justify="left", padding=12).pack(fill="both")
        bar = ttk.Frame(self, padding=(12, 0, 12, 12))
        bar.pack(fill="x")
        for key, text in reversed(list(choices)):
            ttk.Button(bar, text=text, command=lambda k=key: self._pick(k)).pack(side="right", padx=2)

    def _pick(self, key: str) -> None:
        self.choice = key
        self.destroy()

    def show(self) -> Optional[str]:
        self.lift()
        self.focus_force()
        self.grab_set()
        self.wait_window()
        return self.choice


# ---------------------------------------------------------------------------- main window


def _probe_row(p: Probe) -> Tuple[str, str, str]:
    return p.label or "", p.kind, p.http or p.ssh or ""


class PrefsApp:
    """The Preferences window. Build with an existing Tk root; ``saved`` is True after a successful Save."""

    def __init__(self, root: tk.Tk, path: Path, cfg: Config, missing: bool = False) -> None:
        self.root, self.path, self.cfg = root, path, cfg
        self.saved = False
        self._cur: Optional[int] = None          # index of the system shown in the form
        self._loading = False
        self._q: "queue.Queue[Tuple[str, str]]" = queue.Queue()
        root.title("StarterStopper Preferences")
        root.minsize(900, 600)
        root.geometry("1000x680")
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        root.protocol("WM_DELETE_WINDOW", self.cancel)

        outer = ttk.Frame(root, padding=8)
        outer.pack(fill="both", expand=True)
        self.banner = ttk.Frame(outer)
        if missing:
            ttk.Label(self.banner, text=f"No config file yet at {path}. Start from scratch or load the example.") \
                .pack(side="left")
            ttk.Button(self.banner, text="Load example", command=self.load_example).pack(side="left", padx=PAD)
            self.banner.pack(fill="x", pady=(0, PAD))
        self.nb = ttk.Notebook(outer)
        self.nb.pack(fill="both", expand=True)
        self._build_systems_tab()
        self._build_health_tab()
        self._build_general_tab()
        self._build_links_tab()
        bar = ttk.Frame(outer)
        bar.pack(fill="x", pady=(8, 0))
        ttk.Button(bar, text="Open JSON", command=self.open_json).pack(side="left")
        ttk.Button(bar, text="Import...", command=self.import_json).pack(side="left", padx=PAD)
        ttk.Button(bar, text="Export...", command=self.export_json).pack(side="left")
        ttk.Button(bar, text="Cancel", command=self.cancel).pack(side="right")
        ttk.Button(bar, text="Save", command=self.save).pack(side="right", padx=PAD)

        self.populate()
        self._snapshot = self.cfg.to_dict()

    # ------------------------------------------------------------------ Systems tab

    def _build_systems_tab(self) -> None:
        tab = ttk.Frame(self.nb, padding=8)
        self.nb.add(tab, text="Systems")
        left = ttk.Frame(tab)
        left.pack(side="left", fill="y")
        ttk.Label(left, text="Start order (stop is reverse)").pack(anchor="w")
        self.sys_list = tk.Listbox(left, width=32, exportselection=False, activestyle="none")
        self.sys_list.pack(fill="y", expand=True, pady=(2, PAD))
        self.sys_list.bind("<<ListboxSelect>>", self._on_select)
        lb = ttk.Frame(left)
        lb.pack(fill="x")
        for i, (text, cmd) in enumerate([("Add", self.add_system), ("Remove", self.remove_system),
                                         ("Duplicate", self.duplicate_system), ("Up", lambda: self.move_system(-1)),
                                         ("Down", lambda: self.move_system(1))]):
            ttk.Button(lb, text=text, command=cmd, width=9).grid(row=i // 3, column=i % 3, padx=1, pady=1)

        self.right = ttk.Frame(tab)
        self.right.pack(side="left", fill="both", expand=True, padx=(10, 0))
        self.empty_lbl = ttk.Label(tab, text="No systems yet. Click Add to create one.")
        v = self.v = {k: tk.StringVar() for k in ("name", "type", "host", "vmx", "stop", "suspended", "ssh_user", "ssh_key",
                                                  "ssh_port", "settle", "timeout", "interval")}
        b = self.b = {k: tk.BooleanVar() for k in ("enabled", "critical", "utc")}
        for var in (v["name"], v["type"], b["enabled"]):
            var.trace_add("write", lambda *a: self._form_label_changed())

        snb = ttk.Notebook(self.right)
        snb.pack(fill="both", expand=True)
        # -- Basics
        f = ttk.Frame(snb, padding=8)
        snb.add(f, text="Basics")
        f.columnconfigure(1, weight=1)

        def row(r: int, label: str, w: tk.Widget, extra: Optional[tk.Widget] = None) -> None:
            ttk.Label(w.master, text=label).grid(row=r, column=0, sticky="w", padx=(0, PAD), pady=2)
            w.grid(row=r, column=1, sticky="ew", pady=2)
            if extra is not None:
                extra.grid(row=r, column=2, sticky="w", padx=(PAD, 0))

        row(0, "Name", ttk.Entry(f, textvariable=v["name"]))
        type_cb = ttk.Combobox(f, textvariable=v["type"], values=SYSTEM_TYPES, state="readonly", width=20)
        type_cb.bind("<<ComboboxSelected>>", lambda e: self._show_type_fields())
        row(1, "Type", type_cb)
        flags = ttk.Frame(f)
        ttk.Checkbutton(flags, text="Enabled (included in Start all / Stop all)", variable=b["enabled"]).pack(anchor="w")
        ttk.Checkbutton(flags, text="Critical (abort the start chain if not ready)",
                        variable=b["critical"]).pack(anchor="w")
        row(2, "", flags)
        row(3, "Host", ttk.Entry(f, textvariable=v["host"]), ttk.Label(f, text="IP / DNS name ({host})"))

        self.vm_frame = ttk.LabelFrame(f, text="VMware", padding=6)
        self.vm_frame.columnconfigure(1, weight=1)
        row(0, "VMX file", ttk.Entry(self.vm_frame, textvariable=v["vmx"]),
            ttk.Button(self.vm_frame, text="Browse...",
                       command=lambda: _browse_file(v["vmx"], "VMX file", [("VMware VM", "*.vmx")])))
        row(1, "", ttk.Checkbutton(self.vm_frame, text="Ensure guest RTC is UTC (Linux guests)", variable=b["utc"]))
        row(2, "Stop timeout (min)", _spin(self.vm_frame, v["stop"], 1, 1440))
        susp = ttk.Frame(self.vm_frame)
        ttk.Combobox(susp, textvariable=v["suspended"], values=SUSPENDED_ACTIONS, state="readonly",
                     width=12).pack(side="left")
        ttk.Label(susp, text="coldBoot: clean shutdown, then fresh boot").pack(side="left", padx=(PAD, 0))
        row(3, "If suspended", susp)
        susp.grid_configure(columnspan=2)
        self.svc_frame = ttk.LabelFrame(f, text="Windows services (internal names, one per line, start order)",
                                        padding=6)
        self.services_txt = tk.Text(self.svc_frame, height=5, width=40, undo=True)
        self.services_txt.pack(fill="both", expand=True)
        self.vm_frame.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        self.svc_frame.grid(row=5, column=0, columnspan=3, sticky="nsew", pady=(6, 0))

        ssh = ttk.LabelFrame(f, text="SSH (for ssh probes)", padding=6)
        ssh.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        ssh.columnconfigure(1, weight=1)
        row(0, "User", ttk.Entry(ssh, textvariable=v["ssh_user"]))
        row(1, "Key path", ttk.Entry(ssh, textvariable=v["ssh_key"]),
            ttk.Button(ssh, text="Browse...", command=lambda: _browse_file(v["ssh_key"], "SSH private key", [])))
        row(2, "Port", _spin(ssh, v["ssh_port"], 1, 65535))

        # -- Readiness
        f = ttk.Frame(snb, padding=8)
        snb.add(f, text="Readiness")
        timing = ttk.Frame(f)
        timing.pack(fill="x", pady=(0, 6))
        for label, key, lo in (("Settle (s)", "settle", 0), ("Timeout (min)", "timeout", 1),
                               ("Interval (s)", "interval", 1)):
            ttk.Label(timing, text=label).pack(side="left")
            _spin(timing, v[key], lo, 100000, 6).pack(side="left", padx=(PAD, 14))
        ttk.Label(f, text="Probes (all must pass and stay green for the settle time)").pack(anchor="w")
        self.probes_ed = self._probe_editor(f)
        self.probes_ed.pack(fill="both", expand=True)
        # -- Info probes
        f = ttk.Frame(snb, padding=8)
        snb.add(f, text="Info probes")
        ttk.Label(f, text="Reported only; never block start (e.g. reboot required)").pack(anchor="w")
        self.info_ed = self._probe_editor(f)
        self.info_ed.pack(fill="both", expand=True)
        # -- Hooks
        f = ttk.Frame(snb, padding=8)
        snb.add(f, text="Hooks")
        ttk.Label(f, text="Commands run at each phase. Placeholders: {host} {name} {app} {config_dir}").pack(anchor="w")
        self.hooks_ed = ListEditor(f, [("phase", "Phase", 90), ("cmd", "Command", 420), ("req", "Required", 70),
                                       ("timeout", "Timeout (min)", 90)],
                                   row=lambda it: (it[0], it[1].cmd, "yes" if it[1].required else "no",
                                                   it[1].timeoutMinutes),
                                   edit=lambda it: HookDialog(self.root, it).show(),
                                   on_change=self._hooks_changed, noun="hook")
        self.hooks_ed.pack(fill="both", expand=True)
        self.probe_status = ttk.Label(self.right, text="", wraplength=600)
        self.probe_status.pack(fill="x", pady=(PAD, 0))

    def _probe_editor(self, parent: tk.Widget) -> ListEditor:
        ed: ListEditor
        ed = ListEditor(parent, [("label", "Label", 150), ("kind", "Kind", 50), ("target", "Target", 420)],
                        row=_probe_row, edit=lambda p: ProbeDialog(self.root, p).show(), noun="probe",
                        extra=[("Test", lambda: self.test_probe(ed))])
        return ed

    def _show_type_fields(self) -> None:
        vmware = self.v["type"].get() == "vmware"
        (self.vm_frame.grid if vmware else self.vm_frame.grid_remove)()
        (self.svc_frame.grid_remove if vmware else self.svc_frame.grid)()

    def _list_label(self, s: System) -> str:
        return f"{s.name or '(unnamed)'}  [{s.type}]" + ("" if s.enabled else "  (disabled)")

    def _refresh_list(self) -> None:
        self.sys_list.delete(0, "end")
        for i, s in enumerate(self.cfg.systems):
            self.sys_list.insert("end", self._list_label(s))
            if not s.enabled:
                self.sys_list.itemconfig(i, foreground="grey")
        if self._cur is not None:
            self.sys_list.selection_set(self._cur)
            self.sys_list.see(self._cur)

    def _form_label_changed(self) -> None:
        if self._loading or self._cur is None:
            return
        i = self._cur
        tmp = System(name=self.v["name"].get().strip(), type=self.v["type"].get(), enabled=self.b["enabled"].get())
        self.sys_list.delete(i)
        self.sys_list.insert(i, self._list_label(tmp))
        self.sys_list.itemconfig(i, foreground="" if tmp.enabled else "grey")
        self.sys_list.selection_set(i)

    def _load_form(self, i: Optional[int]) -> None:
        self._cur = i
        self.probe_status.configure(text="")
        if i is None:
            self.right.pack_forget()
            self.empty_lbl.pack(side="left", padx=20)
            for ed in (self.probes_ed, self.info_ed, self.hooks_ed):
                ed.set_items([])
            return
        self.empty_lbl.pack_forget()
        self.right.pack(side="left", fill="both", expand=True, padx=(10, 0))
        s = self.cfg.systems[i]
        self._loading = True
        try:
            v, b = self.v, self.b
            for key, val in (("name", s.name), ("type", s.type), ("host", s.host), ("vmx", s.vmx),
                             ("stop", s.stopTimeoutMinutes), ("ssh_user", s.ssh.user), ("ssh_key", s.ssh.keyPath),
                             ("ssh_port", s.ssh.port), ("settle", s.ready.settleSeconds),
                             ("timeout", s.ready.timeoutMinutes), ("interval", s.ready.intervalSeconds)):
                v[key].set(str(val))
            b["enabled"].set(s.enabled)
            b["critical"].set(s.critical)
            b["utc"].set(s.ensureUtcRtc)
            v["suspended"].set(s.onSuspended)
            self.services_txt.delete("1.0", "end")
            self.services_txt.insert("1.0", "\n".join(s.services))
            self.probes_ed.set_items(s.ready.probes)
            self.info_ed.set_items(s.infoProbes)
            self.hooks_ed.set_items([(ph, h) for ph in HOOK_PHASES for h in s.hooks.get(ph, [])])
            self._show_type_fields()
        finally:
            self._loading = False
        self.sys_list.selection_clear(0, "end")
        self.sys_list.selection_set(i)
        self.sys_list.see(i)

    def _hooks_changed(self) -> None:
        if self._cur is None:
            return
        hooks: Dict[str, List[Hook]] = {}
        for phase, h in self.hooks_ed.items:
            hooks.setdefault(phase, []).append(h)
        self.cfg.systems[self._cur].hooks = {ph: hooks[ph] for ph in HOOK_PHASES if ph in hooks}

    def commit_system(self, show: bool = True) -> bool:
        """Write the form into the selected System. Returns False (and optionally explains) on bad input."""
        if self._cur is None:
            return True
        v = self.v
        try:
            stop = _int(v["stop"].get(), "Stop timeout", 1)
            port = _int(v["ssh_port"].get(), "SSH port", 1)
            settle = _int(v["settle"].get(), "Settle seconds", 0)
            timeout = _int(v["timeout"].get(), "Readiness timeout", 1)
            interval = _int(v["interval"].get(), "Probe interval", 1)
        except ValueError as e:
            if show:
                messagebox.showerror("Invalid value", str(e), parent=self.root)
            return False
        s = self.cfg.systems[self._cur]
        s.name = v["name"].get().strip()
        s.type = v["type"].get()
        s.enabled, s.critical, s.ensureUtcRtc = self.b["enabled"].get(), self.b["critical"].get(), self.b["utc"].get()
        s.host, s.vmx, s.stopTimeoutMinutes = v["host"].get().strip(), v["vmx"].get().strip(), stop
        s.onSuspended = v["suspended"].get() or "resume"
        s.services = [ln.strip() for ln in self.services_txt.get("1.0", "end").splitlines() if ln.strip()]
        s.ssh.user, s.ssh.keyPath, s.ssh.port = v["ssh_user"].get().strip(), v["ssh_key"].get().strip(), port
        s.ready.settleSeconds, s.ready.timeoutMinutes, s.ready.intervalSeconds = settle, timeout, interval
        return True

    def _on_select(self, _event: Any = None) -> None:
        sel = self.sys_list.curselection()
        if not sel or sel[0] == self._cur:
            return
        if not self.commit_system():
            self.sys_list.selection_clear(0, "end")
            if self._cur is not None:
                self.sys_list.selection_set(self._cur)
            return
        self._refresh_list()
        self._load_form(sel[0])

    def select_system(self, i: int) -> bool:
        """Programmatic selection (commits the current form first)."""
        if self._cur is not None and not self.commit_system():
            return False
        self._load_form(i)
        self._refresh_list()
        return True

    def _unique_name(self, base: str) -> str:
        names = {s.name for s in self.cfg.systems}
        name, n = base, 2
        while name in names:
            name, n = f"{base} {n}", n + 1
        return name

    def add_system(self) -> None:
        if not self.commit_system():
            return
        self.cfg.systems.append(System(name=self._unique_name("new system")))
        self._load_form(len(self.cfg.systems) - 1)
        self._refresh_list()

    def duplicate_system(self) -> None:
        if self._cur is None or not self.commit_system():
            return
        dup = copy.deepcopy(self.cfg.systems[self._cur])
        dup.name = self._unique_name(f"{dup.name} copy")
        self.cfg.systems.insert(self._cur + 1, dup)
        self._load_form(self._cur + 1)
        self._refresh_list()

    def remove_system(self) -> None:
        if self._cur is None:
            return
        name = self.cfg.systems[self._cur].name
        if not messagebox.askyesno("Remove system", f"Remove '{name}'?", parent=self.root):
            return
        del self.cfg.systems[self._cur]
        n = len(self.cfg.systems)
        self._load_form(min(self._cur, n - 1) if n else None)
        self._refresh_list()

    def move_system(self, delta: int) -> None:
        i = self._cur
        if i is None or not 0 <= i + delta < len(self.cfg.systems) or not self.commit_system():
            return
        sy = self.cfg.systems
        sy[i], sy[i + delta] = sy[i + delta], sy[i]
        self._cur = i + delta
        self._refresh_list()

    def test_probe(self, ed: ListEditor) -> None:
        probe = ed.selected()
        if probe is None:
            self.probe_status.configure(text="Select a probe to test.", foreground="")
            return
        if not self.commit_system():
            return
        system = copy.deepcopy(self.cfg.systems[self._cur])  # type: ignore[index]
        probe = copy.deepcopy(probe)
        self.probe_status.configure(text=f"Testing {probe.display} ...", foreground="")

        def work() -> None:
            try:
                from .probes import run_probe
                r = run_probe(probe, system)
                self._q.put(("PASS" if r.ok else "FAIL", f"{probe.display}: {r.detail}"))
            except Exception as e:  # never let the worker die silently
                self._q.put(("FAIL", f"{probe.display}: {type(e).__name__}: {e}"))

        threading.Thread(target=work, daemon=True).start()
        self.root.after(150, self._poll_probe)

    def _poll_probe(self) -> None:
        try:
            verdict, detail = self._q.get_nowait()
        except queue.Empty:
            try:
                self.root.after(150, self._poll_probe)
            except tk.TclError:
                pass
            return
        colour = "dark green" if verdict == "PASS" else "red"
        self.probe_status.configure(text=f"{verdict}  {detail}", foreground=colour)

    # ------------------------------------------------------------------ Health tab

    def _build_health_tab(self) -> None:
        tab = ttk.Frame(self.nb, padding=8)
        self.nb.add(tab, text="Health")
        self.h_run = tk.BooleanVar()
        self.h_hints = tk.StringVar()
        ttk.Checkbutton(tab, text="Run health checks after Start all", variable=self.h_run).pack(anchor="w")
        hf = ttk.Frame(tab)
        hf.pack(fill="x", pady=6)
        ttk.Label(hf, text="Hints file (optional)").pack(side="left")
        ttk.Entry(hf, textvariable=self.h_hints).pack(side="left", fill="x", expand=True, padx=PAD)
        ttk.Button(hf, text="Browse...", command=lambda: _browse_file(self.h_hints, "Hints file",
                                                                      [("JSON", "*.json")])).pack(side="left")
        ttk.Label(tab, text="Checks").pack(anchor="w")
        self.checks_ed = ListEditor(tab, [("name", "Name", 260), ("format", "Format", 80), ("enabled", "Enabled", 70)],
                                    row=lambda c: (c.name, c.format, "yes" if c.enabled else "no"),
                                    edit=lambda c: CheckDialog(self.root, c).show(), noun="check")
        self.checks_ed.pack(fill="both", expand=True)
        ttk.Label(tab, foreground="grey", justify="left", wraplength=820, text=(
            "Placeholders in commands: {app} = StarterStopper install folder, {config_dir} = folder holding "
            "config.json, {host} / {name} = the system's values. %ENV% variables and ~ are expanded.\n"
            "Format 'json': stdout is a JSON array of {check, status, detail} with status PASS, WARN or FAIL. "
            "Format 'exitcode': exit 0 = pass. Keep secrets out of commands; reference environment "
            "variables or an .env file instead.")).pack(anchor="w", pady=(6, 0))

    # ------------------------------------------------------------------ General tab

    _GEN_BOOLS = (("launchAtLogin", "Launch StarterStopper at login"),
                  ("startAllAtLogin", "Start all systems at login"),
                  ("checkHypervisor", "Warn if the Windows hypervisor is loaded (slows VMware)"),
                  ("notifications", "Show notifications"))
    _GEN_INTS = (("idlePollSeconds", "Idle poll (s)", 5, "how often to refresh state when nothing is running"),
                 ("busyPollSeconds", "Busy poll (s)", 1, "refresh interval while starting / stopping"),
                 ("maxGuestGB", "Max guest RAM (GB)", 0, "0 = no ceiling"),
                 ("maxVcpu", "Max guest vCPUs", 0, "0 = no ceiling"))

    def _build_general_tab(self) -> None:
        tab = ttk.Frame(self.nb, padding=12)
        self.nb.add(tab, text="General")
        tab.columnconfigure(1, weight=1)
        self.g_bools = {k: tk.BooleanVar() for k, _ in self._GEN_BOOLS}
        self.g_ints = {k: tk.StringVar() for k, *_ in self._GEN_INTS}
        r = 0
        for key, text in self._GEN_BOOLS:
            ttk.Checkbutton(tab, text=text, variable=self.g_bools[key]).grid(row=r, column=0, columnspan=3,
                                                                              sticky="w", pady=2)
            r += 1
        for key, text, lo, hint in self._GEN_INTS:
            ttk.Label(tab, text=text).grid(row=r, column=0, sticky="w", pady=2, padx=(0, PAD))
            _spin(tab, self.g_ints[key], lo, 100000).grid(row=r, column=1, sticky="w")
            ttk.Label(tab, text=hint, foreground="grey").grid(row=r, column=2, sticky="w", padx=PAD)
            r += 1
        ttk.Label(tab, text="vmrun.exe path").grid(row=r, column=0, sticky="w", pady=2, padx=(0, PAD))
        self.g_vmrun = PlaceholderEntry(tab, "auto-detect")
        self.g_vmrun.grid(row=r, column=1, sticky="ew")
        ttk.Button(tab, text="Browse...", command=self._browse_vmrun).grid(row=r, column=2, sticky="w", padx=PAD)

    def _browse_vmrun(self) -> None:
        var = tk.StringVar(value=self.g_vmrun.value())
        _browse_file(var, "vmrun.exe", [("vmrun", "vmrun.exe")])
        self.g_vmrun.set_value(var.get())

    # ------------------------------------------------------------------ Links tab

    def _build_links_tab(self) -> None:
        tab = ttk.Frame(self.nb, padding=8)
        self.nb.add(tab, text="Links")
        ttk.Label(tab, text="Shortcuts shown in the tray menu").pack(anchor="w")
        self.links_ed = ListEditor(tab, [("label", "Label", 200), ("url", "URL", 520)],
                                   row=lambda lk: (lk.label, lk.url), edit=lambda lk: LinkDialog(self.root, lk).show(),
                                   noun="link")
        self.links_ed.pack(fill="both", expand=True, pady=(2, 0))

    # ------------------------------------------------------------------ model <-> form

    def populate(self) -> None:
        """Push ``self.cfg`` into every widget (after load, import or Load example)."""
        c = self.cfg
        for k, _ in self._GEN_BOOLS:
            self.g_bools[k].set(getattr(c.general, k))
        for k, *_ in self._GEN_INTS:
            self.g_ints[k].set(str(getattr(c.general, k)))
        self.g_vmrun.set_value(c.general.vmrunPath)
        self.h_run.set(c.health.runAfterStart)
        self.h_hints.set(c.health.hintsFile)
        self.checks_ed.set_items(c.health.checks)
        self.links_ed.set_items(c.links)
        self._cur = None
        self._load_form(0 if c.systems else None)
        self._refresh_list()

    def commit(self, show: bool = True) -> bool:
        """Commit every tab into ``self.cfg``. Returns False on invalid input."""
        if not self.commit_system(show):
            return False
        try:
            ints = {k: _int(self.g_ints[k].get(), text, lo) for k, text, lo, _ in self._GEN_INTS}
        except ValueError as e:
            if show:
                self.nb.select(2)
                messagebox.showerror("Invalid value", str(e), parent=self.root)
            return False
        g = self.cfg.general
        for k, val in ints.items():
            setattr(g, k, val)
        for k, _ in self._GEN_BOOLS:
            setattr(g, k, self.g_bools[k].get())
        g.vmrunPath = self.g_vmrun.value()
        self.cfg.health.runAfterStart = self.h_run.get()
        self.cfg.health.hintsFile = self.h_hints.get().strip()
        self._refresh_list()
        return True

    def is_dirty(self) -> bool:
        return not self.commit(show=False) or self.cfg.to_dict() != self._snapshot

    def _replace(self, cfg: Config) -> None:
        self.cfg = cfg
        self.populate()
        self.banner.pack_forget()

    # ------------------------------------------------------------------ actions

    def load_example(self) -> None:
        example = APP_DIR / "config.example.json"
        try:
            cfg = config.from_dict(json.loads(example.read_text(encoding="utf-8-sig")))
        except (OSError, json.JSONDecodeError, *LOAD_ERRORS) as e:
            messagebox.showerror("Load example", f"Could not load {example}:\n{e}", parent=self.root)
            return
        self._replace(cfg)

    def import_json(self) -> None:
        name = filedialog.askopenfilename(parent=self.root, title="Import config",
                                          filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        if not name:
            return
        try:
            cfg = config.from_dict(json.loads(Path(name).read_text(encoding="utf-8-sig")))
        except (OSError, json.JSONDecodeError, *LOAD_ERRORS) as e:
            messagebox.showerror("Import", f"Could not import {name}:\n{e}", parent=self.root)
            return
        self._replace(cfg)

    def export_json(self) -> None:
        if not self.commit():
            return
        name = filedialog.asksaveasfilename(parent=self.root, title="Export config", defaultextension=".json",
                                            initialfile="starterstopper-config.json",
                                            filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        if not name:
            return
        try:
            Path(name).write_text(json.dumps(config._prune(self.cfg.to_dict()), indent=2) + "\n", encoding="utf-8")
        except OSError as e:
            messagebox.showerror("Export", str(e), parent=self.root)

    def open_json(self) -> None:
        if not self.path.exists():
            messagebox.showinfo("Open JSON", f"{self.path} does not exist yet. Save first.", parent=self.root)
            return
        try:
            os.startfile(str(self.path))  # type: ignore[attr-defined]
        except OSError as e:
            messagebox.showerror("Open JSON", str(e), parent=self.root)

    def save(self, close: bool = True) -> bool:
        if not self.commit():
            return False
        try:
            config.save(self.cfg, self.path)
        except (OSError, *LOAD_ERRORS) as e:
            messagebox.showerror("Cannot save", str(e), parent=self.root)
            return False
        self.saved = True
        self._snapshot = self.cfg.to_dict()
        if close:
            self.root.destroy()
        return True

    def cancel(self) -> None:
        if self.is_dirty() and not messagebox.askyesno(
                "Discard changes?", "You have unsaved changes. Close without saving?", parent=self.root):
            return
        self.root.destroy()


# ---------------------------------------------------------------------------- entry point


def _dpi_aware() -> None:
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
    except Exception:
        pass


def main(path: Path = CONFIG_PATH) -> int:
    """Show the Preferences window. Returns 0 if the config was saved, 1 if cancelled."""
    _dpi_aware()
    root = tk.Tk()
    root.withdraw()
    missing = not path.exists()
    cfg = Config()
    if not missing:
        try:
            cfg = config.load(path)
        except (OSError, *LOAD_ERRORS) as e:
            choice = _ChoiceDialog(root, "StarterStopper Preferences",
                                   f"The config file could not be loaded:\n\n{path}\n\n{e}\n\n"
                                   "Fix it in a text editor, or start with an empty configuration "
                                   "(saving will replace the file; the old one is kept as .json.bak).",
                                   [("open", "Open JSON in editor"), ("empty", "Start empty"),
                                    ("cancel", "Cancel")]).show()
            if choice == "open":
                try:
                    os.startfile(str(path))  # type: ignore[attr-defined]
                except OSError as err:
                    messagebox.showerror("Open JSON", str(err), parent=root)
            if choice != "empty":
                root.destroy()
                return 1
    app = PrefsApp(root, path, cfg, missing=missing)
    root.deiconify()
    root.mainloop()
    return 0 if app.saved else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
