#!/usr/bin/env python3
"""treeconv desktop GUI (Tkinter, standard library only).

Same layout, settings and shortcuts as the terminal UI:

    python treeconv.py --gui
    python gui.py -i ~/Shows -o ~/out        # pre-filled, same options as treeconv.py
"""
from __future__ import annotations

import queue
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from core import (CODECS, CONTAINERS, PRIORITIES, RESOLUTIONS, Callbacks, Converter, Job, Settings,
                  scan, summary_text, tool_problems, validate)

PAD = 4
LABEL_W = 14


class _QueueCallbacks(Callbacks):
    """Worker-thread callbacks -> queue, drained by the Tk main loop."""

    def __init__(self, q: "queue.Queue[tuple]") -> None:
        self.q = q

    def log(self, msg: str, level: str = "info") -> None:
        self.q.put(("log", msg, level))

    def file_start(self, index: int, total: int, job: Job) -> None:
        self.q.put(("start", index, total, job))

    def file_progress(self, fraction: float, speed: str) -> None:
        self.q.put(("file", fraction, speed))

    def overall(self, fraction: float) -> None:
        self.q.put(("overall", fraction))

    def finished(self, summary: Dict[str, int], cancelled: bool) -> None:
        self.q.put(("done", summary, cancelled))


class TreeConvGui:
    def __init__(self, root: tk.Tk, settings: Settings) -> None:
        self.root = root
        self.s = settings
        self.inputs: List[Path] = [Path(p).expanduser().resolve() for p in settings.inputs]
        self.converter: Optional[Converter] = None
        self.thread: Optional[threading.Thread] = None
        self.q: "queue.Queue[tuple]" = queue.Queue()
        self.started_at = 0.0
        self._current = ""
        self._closing = False

        root.title("treeconv")
        self._set_icon()
        root.minsize(900, 560)
        root.geometry("1060x720")
        self._style()
        self._build()
        self._bind_keys()
        root.protocol("WM_DELETE_WINDOW", self.quit)
        if sys.platform == "darwin":
            root.createcommand("tk::mac::Quit", self.quit)  # Cmd+Q / app menu
        self.log_line("Add input folders, choose an output folder, then press Start (Ctrl+S).")
        root.after(100, self._drain)

    # ---- look ------------------------------------------------------------- #
    def _set_icon(self) -> None:
        base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
        icon = base / "assets" / "icon-256.png"
        if icon.is_file():
            try:
                self._icon = tk.PhotoImage(file=str(icon))
                self.root.iconphoto(True, self._icon)
            except tk.TclError:
                pass

    def _style(self) -> None:
        st = ttk.Style(self.root)
        if sys.platform.startswith("linux") and "clam" in st.theme_names():
            st.theme_use("clam")  # the X11 default theme looks dated; native themes elsewhere
        st.configure("Muted.TLabel", foreground="#666")
        if st.theme_use() == "clam":  # clam's bar colour is almost the trough colour
            st.configure("Horizontal.TProgressbar", background="#3b82f6",
                         lightcolor="#60a5fa", darkcolor="#2563eb", bordercolor="#9ca3af")
        st.configure("Accent.TButton", font=("TkDefaultFont", 10, "bold"))

    # ---- layout (mirrors the TUI row by row) ------------------------------ #
    def _row(self, parent: ttk.Frame, r: int, text: str) -> ttk.Frame:
        ttk.Label(parent, text=text, style="Muted.TLabel", width=LABEL_W).grid(
            row=r, column=0, sticky="nw", padx=PAD, pady=PAD + 2)
        f = ttk.Frame(parent)
        f.grid(row=r, column=1, sticky="ew", pady=PAD)
        return f

    def _build(self) -> None:
        codec = CODECS[self.s.codec]
        cfg = ttk.Frame(self.root, padding=(8, 8, 8, 0))
        cfg.grid(row=0, column=0, sticky="ew")
        cfg.columnconfigure(1, weight=1)

        # Input folder: [entry] [Browse…] [Add]
        f = self._row(cfg, 0, "Input folder")
        f.columnconfigure(0, weight=1)
        self.in_path = tk.StringVar()
        e = ttk.Entry(f, textvariable=self.in_path)
        e.grid(row=0, column=0, sticky="ew")
        e.bind("<Return>", lambda _e: self._add_input(self.in_path.get()))
        self.in_browse = ttk.Button(f, text="Browse…", command=self._in_browse)
        self.in_browse.grid(row=0, column=1, padx=(PAD, 0))
        self.in_add = ttk.Button(f, text="Add", command=lambda: self._add_input(self.in_path.get()))
        self.in_add.grid(row=0, column=2, padx=(PAD, 0))
        self.in_entry = e

        # Inputs: [list] [Remove]
        f = self._row(cfg, 1, "Inputs")
        f.columnconfigure(0, weight=1)
        lf = ttk.Frame(f)
        lf.grid(row=0, column=0, sticky="ew")
        lf.columnconfigure(0, weight=1)
        self.in_list = tk.Listbox(lf, height=5, selectmode=tk.EXTENDED, activestyle="dotbox",
                                  exportselection=False)
        self.in_list.grid(row=0, column=0, sticky="ew")
        sb = ttk.Scrollbar(lf, orient="vertical", command=self.in_list.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.in_list.configure(yscrollcommand=sb.set)
        self.in_list.bind("<Delete>", lambda _e: self.remove_input())
        self.in_list.bind("<BackSpace>", lambda _e: self.remove_input())
        self.in_remove = ttk.Button(f, text="Remove", command=self.remove_input)
        self.in_remove.grid(row=0, column=1, sticky="n", padx=(PAD, 0))
        self._refresh_inputs()

        # Output folder: [entry] [Browse…]
        f = self._row(cfg, 2, "Output folder")
        f.columnconfigure(0, weight=1)
        out = str(self.s.output) if str(self.s.output) not in ("", ".") else ""
        self.out_path = tk.StringVar(value=out)
        self.out_entry = ttk.Entry(f, textvariable=self.out_path)
        self.out_entry.grid(row=0, column=0, sticky="ew")
        self.out_browse = ttk.Button(f, text="Browse…", command=self._out_browse)
        self.out_browse.grid(row=0, column=1, padx=(PAD, 0))

        # Target: [codec] CRF [n] [preset] [container]
        f = self._row(cfg, 3, "Target")
        self._codec_keys = list(CODECS)
        self.codec = ttk.Combobox(f, state="readonly", width=30,
                                  values=[c.label for c in CODECS.values()])
        self.codec.current(self._codec_keys.index(self.s.codec))
        self.codec.bind("<<ComboboxSelected>>", self._codec_changed)
        self.codec.grid(row=0, column=0)
        self.crf_lbl = ttk.Label(f, text="CRF", style="Muted.TLabel", width=4)
        self.crf_lbl.grid(row=0, column=1, padx=(PAD * 3, PAD))
        self.crf = tk.StringVar(value=str(self.s.crf))
        self.crf_spin = ttk.Spinbox(f, from_=0, to=63, width=5, textvariable=self.crf)
        self.crf_spin.grid(row=0, column=2)
        self.preset = ttk.Combobox(f, state="readonly", width=12, values=list(codec.presets))
        self.preset.set(self.s.preset if self.s.preset in codec.presets else codec.default_preset)
        self.preset.grid(row=0, column=3, padx=(PAD * 2, 0))
        self._container_keys = list(CONTAINERS)
        self.container = ttk.Combobox(f, state="readonly", width=28, values=list(CONTAINERS.values()))
        self.container.current(self._container_keys.index(self.s.container))
        self.container.grid(row=0, column=4, padx=(PAD * 2, 0))

        # Picture (video only): [max resolution] [x] 8-bit
        f = self._row(cfg, 4, "Picture")
        self._res_keys = list(RESOLUTIONS)
        self.resolution = ttk.Combobox(f, state="readonly", width=20, values=list(RESOLUTIONS.values()))
        self.resolution.current(self._res_keys.index(self.s.resolution))
        self.resolution.grid(row=0, column=0, padx=(0, PAD * 3))
        self.opt_8bit = tk.BooleanVar(value=self.s.eight_bit)
        self.chk_8bit = ttk.Checkbutton(f, text="8-bit 4:2:0 output", variable=self.opt_8bit)
        self.chk_8bit.grid(row=0, column=1)

        # Options: [x] [x] [priority]
        f = self._row(cfg, 5, "Options")
        self.opt_same = tk.BooleanVar(value=self.s.copy_if_same_codec)
        self.opt_skip = tk.BooleanVar(value=self.s.skip_existing)
        self._checks = [self.chk_8bit]
        for i, (txt, var) in enumerate([("Copy if already in target format", self.opt_same),
                                        ("Skip existing (resume)", self.opt_skip)]):
            cb = ttk.Checkbutton(f, text=txt, variable=var)
            cb.grid(row=0, column=i, padx=(0, PAD * 3))
            self._checks.append(cb)
        self._prio_keys = list(PRIORITIES)
        self.priority = ttk.Combobox(f, state="readonly", width=16, values=list(PRIORITIES.values()))
        self.priority.current(self._prio_keys.index(self.s.priority))
        self.priority.grid(row=0, column=2)

        ttk.Separator(self.root).grid(row=1, column=0, sticky="ew", pady=(PAD * 2, 0))

        # Status area: actions, current file, bars, log
        st = ttk.Frame(self.root, padding=8)
        st.grid(row=2, column=0, sticky="nsew")
        st.columnconfigure(0, weight=1)
        st.rowconfigure(5, weight=1)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)

        acts = ttk.Frame(st)
        acts.grid(row=0, column=0, sticky="w", pady=(0, PAD * 2))
        self.btn_scan = ttk.Button(acts, text="Scan", command=self.scan)
        self.btn_scan.grid(row=0, column=0, padx=(0, PAD * 2))
        self.btn_start = ttk.Button(acts, text="Start", style="Accent.TButton", command=self.start)
        self.btn_start.grid(row=0, column=1, padx=(0, PAD * 2))
        self.btn_cancel = ttk.Button(acts, text="Cancel", command=self.cancel_run, state="disabled")
        self.btn_cancel.grid(row=0, column=2)

        self.current = ttk.Label(st, text="Idle")
        self.current.grid(row=1, column=0, sticky="ew")
        self.file_bar = ttk.Progressbar(st, maximum=100)
        self.file_bar.grid(row=2, column=0, sticky="ew", pady=(2, PAD * 2))
        self.overall_lbl = ttk.Label(st, text="Overall")
        self.overall_lbl.grid(row=3, column=0, sticky="ew")
        self.all_bar = ttk.Progressbar(st, maximum=100)
        self.all_bar.grid(row=4, column=0, sticky="ew", pady=(2, PAD * 2))

        lf = ttk.Frame(st)
        lf.grid(row=5, column=0, sticky="nsew")
        lf.columnconfigure(0, weight=1)
        lf.rowconfigure(0, weight=1)
        self.log = tk.Text(lf, wrap="word", height=10, state="disabled", relief="flat",
                           borderwidth=1, highlightthickness=1)
        self.log.grid(row=0, column=0, sticky="nsew")
        lsb = ttk.Scrollbar(lf, orient="vertical", command=self.log.yview)
        lsb.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=lsb.set)
        self.log.tag_configure("error", foreground="#c62828", font=("TkDefaultFont", 9, "bold"))
        self.log.tag_configure("warn", foreground="#b26a00")
        self.log.tag_configure("dim", foreground="#888")
        self.log.tag_configure("ok", foreground="#2e7d32")
        self._log_menu()

        # Footer with shortcuts, like Textual's footer
        mod = "Cmd" if sys.platform == "darwin" else "Ctrl"
        ttk.Label(self.root, style="Muted.TLabel", padding=(8, 2, 8, 4),
                  text=f"{mod}+S Start   {mod}+R Scan   {mod}+X Cancel run   "
                       f"Del Remove input   {mod}+Q Quit").grid(row=3, column=0, sticky="w")

        self._apply_kind(CODECS[self.s.codec])
        self._config_widgets = [self.in_entry, self.in_browse, self.in_add, self.in_remove,
                                self.out_entry, self.out_browse, self.codec, self.crf_spin,
                                self.preset, self.container, self.resolution, self.priority, *self._checks]

    def _log_menu(self) -> None:
        m = tk.Menu(self.log, tearoff=0)
        m.add_command(label="Copy", command=lambda: self.log.event_generate("<<Copy>>"))
        m.add_command(label="Select all", command=lambda: self.log.tag_add("sel", "1.0", "end"))
        m.add_separator()
        m.add_command(label="Clear log", command=self._clear_log)
        btn = "<Button-2>" if sys.platform == "darwin" else "<Button-3>"
        self.log.bind(btn, lambda e: m.tk_popup(e.x_root, e.y_root))

    def _bind_keys(self) -> None:
        mod = "Command" if sys.platform == "darwin" else "Control"
        for key, fn in (("s", self.start), ("r", self.scan), ("q", self.quit)):
            self.root.bind_all(f"<{mod}-{key}>", lambda _e, fn=fn: (fn(), "break")[1])
        # Ctrl+X in an entry is "cut": only cancel when a run is active
        self.root.bind_all(f"<{mod}-x>",
                           lambda _e: (self.cancel_run(), "break")[1] if self.converter else None)

    # ---- helpers ---------------------------------------------------------- #
    def log_line(self, msg: str, level: str = "info") -> None:
        at_end = self.log.yview()[1] >= 0.999
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n", () if level == "info" else (level,))
        self.log.configure(state="disabled")
        if at_end:
            self.log.see("end")

    def _clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _refresh_inputs(self) -> None:
        self.in_list.delete(0, "end")
        for p in self.inputs:
            self.in_list.insert("end", str(p))

    def _add_input(self, raw: str) -> None:
        raw = raw.strip()
        if not raw or self.converter:
            return
        p = Path(raw).expanduser().resolve()
        if not p.is_dir():
            messagebox.showerror("treeconv", f"Not a folder:\n{p}", parent=self.root)
            return
        if p in self.inputs:
            self.log_line(f"Already added: {p}", "warn")
            return
        self.inputs.append(p)
        self._refresh_inputs()
        self.in_path.set("")

    def remove_input(self) -> None:
        if self.converter:
            return
        for idx in sorted(self.in_list.curselection(), reverse=True):
            self.inputs.pop(idx)
        self._refresh_inputs()

    def _in_browse(self) -> None:
        start = self.inputs[-1].parent if self.inputs else Path.home()
        p = filedialog.askdirectory(parent=self.root, title="Choose an input folder",
                                    initialdir=str(start), mustexist=True)
        if p:
            self._add_input(p)

    def _out_browse(self) -> None:
        cur = self.out_path.get().strip()
        start = Path(cur).expanduser() if cur and Path(cur).expanduser().is_dir() else Path.home()
        p = filedialog.askdirectory(parent=self.root, title="Choose the output folder",
                                    initialdir=str(start))
        if p:
            self.out_path.set(p)

    def _codec_changed(self, _e=None) -> None:
        codec = CODECS[self._codec_keys[self.codec.current()]]
        self.preset.configure(values=list(codec.presets))
        self.preset.set(codec.default_preset)
        if not codec.is_audio:
            self.crf.set(str(codec.default_crf))
        self._apply_kind(codec)

    def _apply_kind(self, codec) -> None:
        """Video-only settings are greyed out for audio targets."""
        off = codec.is_audio
        self.crf_spin.configure(state="disabled" if off else "normal")
        self.container.configure(state="disabled" if off else "readonly")
        self.chk_8bit.configure(state="disabled" if off else "normal")
        self.resolution.configure(state="disabled" if off else "readonly")
        self.crf_lbl.configure(text="" if off else "CRF")

    def _collect(self) -> Optional[Settings]:
        out_raw = self.out_path.get().strip()
        try:
            crf = int(self.crf.get())
        except ValueError:
            messagebox.showerror("treeconv", "CRF must be an integer", parent=self.root)
            return None
        return Settings(
            inputs=list(self.inputs),
            output=Path(out_raw).expanduser() if out_raw else Path(""),
            codec=self._codec_keys[self.codec.current()],
            crf=crf,
            preset=self.preset.get(),
            container=self._container_keys[self.container.current()],
            copy_if_same_codec=self.opt_same.get(),
            skip_existing=self.opt_skip.get(),
            eight_bit=self.opt_8bit.get(),
            resolution=self._res_keys[self.resolution.current()],
            priority=self._prio_keys[self.priority.current()],
        )

    def _check(self, s: Settings) -> bool:
        errors = validate(s.inputs, s.output if str(s.output) else None) + tool_problems(s.codec)
        for e in errors:
            self.log_line(e, "error")
        if errors:
            messagebox.showerror("treeconv", "\n\n".join(errors), parent=self.root)
        return not errors

    def _set_running(self, running: bool) -> None:
        for w in self._config_widgets:
            if isinstance(w, ttk.Combobox):
                w.configure(state="disabled" if running else "readonly")
            else:
                w.configure(state="disabled" if running else "normal")
        self.in_list.configure(state="disabled" if running else "normal")
        self.btn_scan.configure(state="disabled" if running else "normal")
        self.btn_start.configure(state="disabled" if running else "normal")
        self.btn_cancel.configure(state="normal" if running else "disabled")
        if not running:
            self._apply_kind(CODECS[self._codec_keys[self.codec.current()]])

    # ---- actions ---------------------------------------------------------- #
    def scan(self) -> None:
        if self.converter:
            return
        s = self._collect()
        if not s or not self._check(s):
            return
        plan = scan(s.inputs, s.output, s.codec)
        self.log_line(f"Scan: {plan.describe()}.", "ok")
        for inp in s.inputs:
            self.log_line(f"  {inp}  ->  {Path(s.output).expanduser().resolve() / inp.name}", "dim")

    def start(self) -> None:
        if self.converter:
            return
        s = self._collect()
        if not s or not self._check(s):
            return
        s.output = s.output.expanduser().resolve()
        plan = scan(s.inputs, s.output, s.codec)
        if not plan.jobs and not plan.dirs:
            messagebox.showinfo("treeconv", "Nothing to do.", parent=self.root)
            return
        self.log_line(f"Starting: {plan.describe()} -> {s.output}", "ok")
        self.converter = Converter(s, plan, _QueueCallbacks(self.q))
        self.started_at = time.monotonic()
        self.file_bar["value"] = 0
        self.all_bar["value"] = 0
        self._set_running(True)
        self.thread = threading.Thread(target=self._worker, args=(self.converter,), daemon=True)
        self.thread.start()

    def _worker(self, conv: Converter) -> None:
        try:
            conv.run()
        except Exception as exc:  # unexpected (e.g. permission error on mkdir)
            self.q.put(("log", f"Fatal: {exc}", "error"))
            self.q.put(("done", None, True))

    def cancel_run(self) -> None:
        if self.converter:
            self.log_line("Cancelling… (partial file will be removed)", "warn")
            self.converter.cancel()

    def quit(self) -> None:
        if self.converter:
            if not messagebox.askyesno("treeconv", "A conversion is running. Cancel it and quit?",
                                       parent=self.root):
                return
            self._closing = True
            self.converter.cancel()
            self.current.configure(text="Stopping…")
            self._wait_then_close()
            return
        self.root.destroy()

    def _wait_then_close(self) -> None:
        # let the worker remove its temp file before the process exits
        if self.thread and self.thread.is_alive():
            self.root.after(100, self._wait_then_close)
        else:
            self.root.destroy()

    # ---- worker -> UI ----------------------------------------------------- #
    def _drain(self) -> None:
        try:
            while True:
                kind, *args = self.q.get_nowait()
                getattr(self, f"_on_{kind}")(*args)
        except queue.Empty:
            pass
        if not self._closing or (self.thread and self.thread.is_alive()):
            self.root.after(100, self._drain)

    def _on_log(self, msg: str, level: str) -> None:
        self.log_line(msg, level)

    def _on_start(self, index: int, total: int, job: Job) -> None:
        self._current = f"[{index}/{total}] {job.src}"
        self.current.configure(text=self._current)
        self.file_bar["value"] = 0

    def _on_file(self, fraction: float, speed: str) -> None:
        self.file_bar["value"] = fraction * 100
        if speed:
            self.current.configure(text=f"{self._current}   speed {speed}")

    def _on_overall(self, fraction: float) -> None:
        self.all_bar["value"] = fraction * 100
        elapsed = time.monotonic() - self.started_at
        eta = ""
        if fraction > 0.01:
            rem = elapsed / fraction - elapsed
            eta = f" - ETA {int(rem // 3600)}h{int(rem % 3600 // 60):02d}m"
        self.overall_lbl.configure(text=f"Overall {fraction * 100:.1f}%{eta}")
        self.root.title(f"{fraction * 100:.0f}% - treeconv")

    def _on_done(self, summary: Optional[Dict[str, int]], cancelled: bool) -> None:
        self.converter = None
        self.root.title("treeconv")
        if self._closing:
            return
        self._set_running(False)
        self.current.configure(text="Cancelled" if cancelled else "Finished")
        if not cancelled:
            self.file_bar["value"] = 100
        if summary:
            level = "error" if summary["failed"] else ("warn" if cancelled else "ok")
            self.log_line(("Cancelled. " if cancelled else "Done. ") + summary_text(summary), level)
            if not cancelled:
                self.root.bell()
                if summary["failed"]:
                    messagebox.showwarning("treeconv", summary_text(summary), parent=self.root)
                else:
                    messagebox.showinfo("treeconv", summary_text(summary), parent=self.root)


def _hidpi() -> None:
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass


def run_gui(settings: Settings) -> int:
    _hidpi()
    root = tk.Tk(className="treeconv")  # WM_CLASS, matches the Linux .desktop entry
    TreeConvGui(root, settings)
    root.mainloop()
    return 0


if __name__ == "__main__":
    import treeconv
    sys.exit(treeconv.main(force="gui"))
