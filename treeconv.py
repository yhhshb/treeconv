#!/usr/bin/env python3
"""treeconv - batch video/audio converter that mirrors whole folder trees.

With a video target (default: H.264/x264) every video is re-encoded, keeping all
audio tracks, embedded subtitles, chapters, metadata and attachments. With an
audio target (e.g. -c mp3) every audio file is converted, keeping tags and cover
art. All other files (including videos, for an audio target) are copied as-is.

How it starts:
    treeconv                          from a terminal: terminal UI
    (double-click)                    desktop GUI
    treeconv -i Shows -o out          any option: command-line mode (no UI)
    treeconv --tui / --gui [options]  open a UI, pre-filled with the options
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

from core import (CODECS, CONTAINERS, PRIORITIES, __version__, Callbacks, Converter, Job, Plan, Settings,
                  scan, summary_text, tool_problems, validate)


# --------------------------------------------------------------------------- #
# Headless mode
# --------------------------------------------------------------------------- #

class _PrintCallbacks(Callbacks):
    def __init__(self) -> None:
        self._last = 0.0
        self._name = ""

    def log(self, msg: str, level: str = "info") -> None:
        if level == "dim":
            return
        sys.stdout.write("\r\033[K" + ("! " if level in ("warn", "error") else "  ") + msg + "\n")

    def file_start(self, index: int, total: int, job: Job) -> None:
        self._name = f"[{index}/{total}] {job.src.name}"

    def file_progress(self, fraction: float, speed: str) -> None:
        now = time.monotonic()
        if now - self._last > 0.5 or fraction >= 1:
            self._last = now
            sys.stdout.write(f"\r\033[K{self._name}  {fraction * 100:5.1f}%  {speed}")
            sys.stdout.flush()

    def finished(self, summary: Dict[str, int], cancelled: bool) -> None:
        sys.stdout.write("\r\033[K")
        print(("Cancelled. " if cancelled else "Done. ") + summary_text(summary))


def run_headless(settings: Settings) -> int:
    errors = validate(settings.inputs, settings.output) + tool_problems(settings.codec)
    if errors:
        for e in errors:
            print("error:", e, file=sys.stderr)
        return 2
    plan = scan(settings.inputs, settings.output, settings.codec)
    print(plan.describe())
    conv = Converter(settings, plan, _PrintCallbacks())
    try:
        summary = conv.run()
    except KeyboardInterrupt:
        conv.cancel()
        print("\nInterrupted.")
        return 130
    return 1 if summary["failed"] else 0


# --------------------------------------------------------------------------- #
# TUI
# --------------------------------------------------------------------------- #

def build_app(settings: Settings):
    from textual import on, work
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.screen import ModalScreen
    from textual.widgets import (Button, Checkbox, DirectoryTree, Footer, Header, Input,
                                 Label, ListItem, ListView, ProgressBar, RichLog, Select)

    class DirsOnlyTree(DirectoryTree):
        def filter_paths(self, paths):
            return [p for p in paths if p.is_dir() and not p.name.startswith(".")]

    class DirPicker(ModalScreen[Optional[Path]]):
        """Modal folder browser."""

        BINDINGS = [Binding("escape", "dismiss_none", "Cancel"),
                    Binding("backspace", "up", "Parent folder", show=True)]

        def __init__(self, title: str, start: Path) -> None:
            super().__init__()
            self._title = title
            start = start if start.is_dir() else Path.home()
            self._selected = start
            self._root = start

        def compose(self) -> ComposeResult:
            with Vertical(id="picker"):
                yield Label(f"[b]{self._title}[/b]  (Enter opens a folder, Backspace goes up)")
                yield Input(str(self._selected), id="picker_path")
                yield DirsOnlyTree(self._root, id="picker_tree")
                with Horizontal(classes="buttons"):
                    yield Button("Up", id="picker_up")
                    yield Button("Select", id="picker_ok", variant="primary")
                    yield Button("Cancel", id="picker_cancel")

        def _set_selected(self, path: Path) -> None:
            self._selected = path
            self.query_one("#picker_path", Input).value = str(path)

        @on(DirectoryTree.DirectorySelected)
        def _dir_selected(self, event: DirectoryTree.DirectorySelected) -> None:
            self._set_selected(Path(event.path))

        @on(Input.Submitted, "#picker_path")
        def _path_typed(self, event: Input.Submitted) -> None:
            p = Path(event.value).expanduser()
            if p.is_dir():
                self.query_one(DirsOnlyTree).path = p
                self._set_selected(p.resolve())

        def action_up(self) -> None:
            tree = self.query_one(DirsOnlyTree)
            parent = Path(tree.path).resolve().parent
            tree.path = parent
            self._set_selected(parent)

        def action_dismiss_none(self) -> None:
            self.dismiss(None)

        @on(Button.Pressed, "#picker_up")
        def _up(self) -> None:
            self.action_up()

        @on(Button.Pressed, "#picker_ok")
        def _ok(self) -> None:
            typed = Path(self.query_one("#picker_path", Input).value).expanduser()
            self.dismiss(typed.resolve() if typed.is_dir() else self._selected)

        @on(Button.Pressed, "#picker_cancel")
        def _cancel(self) -> None:
            self.dismiss(None)

    class TuiCallbacks(Callbacks):
        def __init__(self, app: "TreeConvApp") -> None:
            self.app = app

        def log(self, msg: str, level: str = "info") -> None:
            self.app.call_from_thread(self.app.log_line, msg, level)

        def file_start(self, index: int, total: int, job: Job) -> None:
            self.app.call_from_thread(self.app.set_current, index, total, job)

        def file_progress(self, fraction: float, speed: str) -> None:
            self.app.call_from_thread(self.app.set_file_progress, fraction, speed)

        def overall(self, fraction: float) -> None:
            self.app.call_from_thread(self.app.set_overall, fraction)

        def finished(self, summary: Dict[str, int], cancelled: bool) -> None:
            self.app.call_from_thread(self.app.on_finished, summary, cancelled)

    class TreeConvApp(App):
        TITLE = "treeconv"
        SUB_TITLE = "batch video & audio converter - keeps your folder tree"
        CSS = """
        #config { height: auto; max-height: 55%; padding: 0 1; }
        .row { height: auto; }
        .row > * { margin-right: 1; }
        .lbl { width: 16; padding-top: 1; color: $text-muted; }
        #in_path, #out_path { width: 1fr; }
        #in_list { height: 5; border: round $panel; }
        #codec { width: 36; } #container { width: 40; } #preset { width: 16; }
        #crf { width: 8; } #priority { width: 18; }
        #actions { height: auto; margin-bottom: 1; }
        #actions Button { margin-right: 2; }
        #status { height: 1fr; padding: 0 1; border-top: solid $panel; }
        #current, #overall_lbl { height: 1; }
        ProgressBar { margin-bottom: 1; }
        ProgressBar Bar { width: 1fr; }
        #log { height: 1fr; border: round $panel; }
        DirPicker { align: center middle; }
        #picker { width: 90%; height: 85%; border: thick $accent; background: $surface; padding: 1; }
        #picker_tree { height: 1fr; }
        #picker .buttons { height: auto; margin-top: 1; }
        #picker .buttons Button { margin-right: 2; }
        """
        BINDINGS = [
            Binding("ctrl+s", "start", "Start"),
            Binding("ctrl+r", "scan", "Scan"),
            Binding("ctrl+x", "cancel_run", "Cancel run"),
            Binding("delete", "remove_input", "Remove input"),
            Binding("ctrl+q", "quit", "Quit"),
        ]

        def __init__(self, settings: Settings) -> None:
            super().__init__()
            self.s = settings
            self.inputs: List[Path] = [Path(p).expanduser().resolve() for p in settings.inputs]
            self.converter: Optional[Converter] = None
            self.plan: Optional[Plan] = None
            self.started_at = 0.0
            self._current = ""

        # ---- layout -------------------------------------------------------- #
        def compose(self) -> ComposeResult:
            codec = CODECS[self.s.codec]
            yield Header()
            with VerticalScroll(id="config"):
                with Horizontal(classes="row"):
                    yield Label("Input folder", classes="lbl")
                    yield Input(placeholder="/path/to/input (Enter to add)", id="in_path")
                    yield Button("Browse…", id="in_browse")
                    yield Button("Add", id="in_add", variant="success")
                with Horizontal(classes="row"):
                    yield Label("Inputs", classes="lbl")
                    yield ListView(*[ListItem(Label(str(p))) for p in self.inputs], id="in_list")
                    yield Button("Remove", id="in_remove", variant="error")
                with Horizontal(classes="row"):
                    yield Label("Output folder", classes="lbl")
                    yield Input(str(self.s.output) if str(self.s.output) not in ("", ".") else "",
                                placeholder="/path/to/output", id="out_path")
                    yield Button("Browse…", id="out_browse")
                with Horizontal(classes="row"):
                    yield Label("Target", classes="lbl")
                    yield Select([(c.label, k) for k, c in CODECS.items()], value=self.s.codec,
                                 allow_blank=False, id="codec")
                    yield Label("CRF", classes="lbl", id="crf_lbl")
                    yield Input(str(self.s.crf), id="crf", type="integer")
                    yield Select([(p, p) for p in codec.presets], value=self.s.preset
                                 if self.s.preset in codec.presets else codec.default_preset,
                                 allow_blank=False, id="preset")
                    yield Select([(v, k) for k, v in CONTAINERS.items()], value=self.s.container,
                                 allow_blank=False, id="container")
                with Horizontal(classes="row"):
                    yield Label("Options", classes="lbl")
                    yield Checkbox("Copy if already in target format", self.s.copy_if_same_codec, id="opt_same")
                    yield Checkbox("Skip existing (resume)", self.s.skip_existing, id="opt_skip")
                    yield Checkbox("8-bit 4:2:0 output", self.s.eight_bit, id="opt_8bit")
                    yield Select([(v, k) for k, v in PRIORITIES.items()], value=self.s.priority,
                                 allow_blank=False, id="priority")
            with Vertical(id="status"):
                with Horizontal(id="actions"):
                    yield Button("Scan", id="scan")
                    yield Button("Start", id="start", variant="primary")
                    yield Button("Cancel", id="cancel", variant="error", disabled=True)
                yield Label("Idle", id="current")
                yield ProgressBar(total=100, show_eta=False, id="file_bar")
                yield Label("Overall", id="overall_lbl")
                yield ProgressBar(total=100, show_eta=False, id="all_bar")
                yield RichLog(id="log", markup=True, wrap=True, highlight=False)
            yield Footer()

        def on_mount(self) -> None:
            self.query_one("#crf_lbl", Label).styles.width = 5
            self._apply_kind(CODECS[self.s.codec])
            self.log_line("Add input folders, choose an output folder, then press [b]Start[/b] (Ctrl+S).")

        # ---- helpers ------------------------------------------------------- #
        def log_line(self, msg: str, level: str = "info") -> None:
            from rich.markup import escape
            style = {"error": "bold red", "warn": "yellow", "dim": "dim", "ok": "green"}.get(level)
            text = msg if level == "info" and "[b]" in msg else escape(msg)
            self.query_one("#log", RichLog).write(f"[{style}]{text}[/]" if style else text)

        def _refresh_inputs(self) -> None:
            lv = self.query_one("#in_list", ListView)
            lv.clear()
            for p in self.inputs:
                lv.append(ListItem(Label(str(p))))

        def _add_input(self, raw: str) -> None:
            raw = raw.strip()
            if not raw:
                return
            p = Path(raw).expanduser().resolve()
            if not p.is_dir():
                self.notify(f"Not a folder: {p}", severity="error")
                return
            if p in self.inputs:
                self.notify("Already added", severity="warning")
                return
            self.inputs.append(p)
            self._refresh_inputs()
            self.query_one("#in_path", Input).value = ""

        def _collect(self) -> Optional[Settings]:
            out_raw = self.query_one("#out_path", Input).value.strip()
            try:
                crf = int(self.query_one("#crf", Input).value)
            except ValueError:
                self.notify("CRF must be an integer", severity="error")
                return None
            return Settings(
                inputs=list(self.inputs),
                output=Path(out_raw).expanduser() if out_raw else Path(""),
                codec=str(self.query_one("#codec", Select).value),
                crf=crf,
                preset=str(self.query_one("#preset", Select).value),
                container=str(self.query_one("#container", Select).value),
                copy_if_same_codec=self.query_one("#opt_same", Checkbox).value,
                skip_existing=self.query_one("#opt_skip", Checkbox).value,
                eight_bit=self.query_one("#opt_8bit", Checkbox).value,
                priority=str(self.query_one("#priority", Select).value),
            )

        def _check(self, s: Settings) -> bool:
            errors = validate(s.inputs, s.output if str(s.output) else None) + tool_problems(s.codec)
            for e in errors:
                self.log_line(e, "error")
            if errors:
                self.notify(errors[0], severity="error", timeout=8)
            return not errors

        def _set_running(self, running: bool) -> None:
            for w in self.query("#config Input, #config Select, #config Checkbox, "
                                "#config Button, #config ListView, #actions Button"):
                w.disabled = (not running) if w.id == "cancel" else running
            if not running:
                self._apply_kind(CODECS[str(self.query_one("#codec", Select).value)])

        def _apply_kind(self, codec) -> None:
            """Video-only settings are greyed out for audio targets."""
            for wid in ("#crf", "#container", "#opt_8bit"):
                self.query_one(wid).disabled = codec.is_audio
            self.query_one("#crf_lbl", Label).update("" if codec.is_audio else "CRF")

        # ---- events -------------------------------------------------------- #
        @on(Input.Submitted, "#in_path")
        def _in_submitted(self, event: Input.Submitted) -> None:
            self._add_input(event.value)

        @on(Button.Pressed, "#in_add")
        def _in_add(self) -> None:
            self._add_input(self.query_one("#in_path", Input).value)

        @on(Button.Pressed, "#in_browse")
        def _in_browse(self) -> None:
            start = self.inputs[-1].parent if self.inputs else Path.home()

            def done(p: Optional[Path]) -> None:
                if p:
                    self._add_input(str(p))
            self.push_screen(DirPicker("Choose an input folder", start), done)

        @on(Button.Pressed, "#out_browse")
        def _out_browse(self) -> None:
            cur = self.query_one("#out_path", Input).value.strip()
            start = Path(cur).expanduser() if cur else Path.home()

            def done(p: Optional[Path]) -> None:
                if p:
                    self.query_one("#out_path", Input).value = str(p)
            self.push_screen(DirPicker("Choose the output folder", start), done)

        @on(Button.Pressed, "#in_remove")
        def action_remove_input(self) -> None:
            if self.converter:
                return
            lv = self.query_one("#in_list", ListView)
            idx = lv.index
            if idx is not None and 0 <= idx < len(self.inputs):
                self.inputs.pop(idx)
                self._refresh_inputs()

        @on(Select.Changed, "#codec")
        def _codec_changed(self, event: Select.Changed) -> None:
            codec = CODECS[str(event.value)]
            preset = self.query_one("#preset", Select)
            preset.set_options([(p, p) for p in codec.presets])
            preset.value = codec.default_preset
            if not codec.is_audio:
                self.query_one("#crf", Input).value = str(codec.default_crf)
            self._apply_kind(codec)

        @on(Button.Pressed, "#scan")
        def action_scan(self) -> None:
            s = self._collect()
            if not s or not self._check(s):
                return
            plan = scan(s.inputs, s.output, s.codec)
            self.log_line(f"Scan: {plan.describe()}.", "ok")
            for inp in s.inputs:
                self.log_line(f"  {inp}  ->  {Path(s.output).expanduser().resolve() / inp.name}", "dim")

        @on(Button.Pressed, "#start")
        def action_start(self) -> None:
            if self.converter:
                return
            s = self._collect()
            if not s or not self._check(s):
                return
            s.output = s.output.expanduser().resolve()
            plan = scan(s.inputs, s.output, s.codec)
            if not plan.jobs and not plan.dirs:
                self.notify("Nothing to do", severity="warning")
                return
            self.log_line(f"Starting: {plan.describe()} -> {s.output}", "ok")
            self.converter = Converter(s, plan, TuiCallbacks(self))
            self.started_at = time.monotonic()
            self._set_running(True)
            self._worker(self.converter)

        @work(thread=True, exclusive=True, group="convert")
        def _worker(self, conv: Converter) -> None:
            try:
                conv.run()
            except Exception as exc:  # unexpected (e.g. permission error on mkdir)
                self.call_from_thread(self.log_line, f"Fatal: {exc}", "error")
                self.call_from_thread(self.on_finished, None, True)

        @on(Button.Pressed, "#cancel")
        def action_cancel_run(self) -> None:
            if self.converter:
                self.log_line("Cancelling… (partial file will be removed)", "warn")
                self.converter.cancel()

        async def action_quit(self) -> None:
            if self.converter:
                self.converter.cancel()
            self.exit()

        # ---- progress callbacks (main thread) ------------------------------ #
        def set_current(self, index: int, total: int, job: Job) -> None:
            from rich.markup import escape
            self._current = escape(f"[{index}/{total}] {job.src}")
            self.query_one("#current", Label).update(self._current)
            self.query_one("#file_bar", ProgressBar).update(progress=0)

        def set_file_progress(self, fraction: float, speed: str) -> None:
            self.query_one("#file_bar", ProgressBar).update(progress=fraction * 100)
            if speed:
                self.query_one("#current", Label).update(f"{self._current}  [dim]speed {speed}[/]")

        def set_overall(self, fraction: float) -> None:
            self.query_one("#all_bar", ProgressBar).update(progress=fraction * 100)
            elapsed = time.monotonic() - self.started_at
            eta = ""
            if fraction > 0.01:
                rem = elapsed / fraction - elapsed
                eta = f" - ETA {int(rem // 3600)}h{int(rem % 3600 // 60):02d}m"
            self.query_one("#overall_lbl", Label).update(f"Overall {fraction * 100:.1f}%{eta}")

        def on_finished(self, summary: Optional[Dict[str, int]], cancelled: bool) -> None:
            self.converter = None
            self._set_running(False)
            self.query_one("#current", Label).update("Cancelled" if cancelled else "Finished")
            if summary:
                level = "error" if summary["failed"] else ("warn" if cancelled else "ok")
                self.log_line(("Cancelled. " if cancelled else "Done. ") + summary_text(summary), level)
                self.notify(summary_text(summary), timeout=10)

    return TreeConvApp(settings)


def run_tui(settings: Settings) -> int:
    build_app(settings).run()
    return 0


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def _launched_from_terminal() -> bool:
    """True when started from a shell, False when double-clicked on a desktop."""
    if sys.platform == "win32":
        import ctypes
        try:
            pids = (ctypes.c_uint32 * 8)()
            count = ctypes.windll.kernel32.GetConsoleProcessList(pids, 8)
        except Exception:
            return True
        if count == 0:            # no console at all (windowed build)
            return False
        # A double-clicked console program is alone on its new console. A one-file
        # PyInstaller build is two processes (bootloader + app) sharing it.
        own = 1
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass and Path(meipass).parent != Path(sys.executable).parent:
            own = 2
        return count > own
    return bool(sys.stdin and sys.stdin.isatty() and sys.stdout and sys.stdout.isatty())


def _handoff_to_gui_exe(argv: List[str]) -> bool:
    """Windows: a double-clicked console exe starts its windowed twin and exits,
    so no empty console window stays open next to the GUI."""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return False
    exe = Path(sys.executable)
    twin = exe.with_name(exe.stem + "-gui" + exe.suffix)
    if not twin.is_file():
        return False
    import subprocess
    subprocess.Popen([str(twin), *argv], close_fds=True,
                     creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
    return True


def _is_gui_build() -> bool:
    """The windowed twin executable (treeconv-gui) always opens the GUI."""
    return getattr(sys, "frozen", False) and Path(sys.executable).stem.lower().endswith("-gui")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="treeconv", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-i", "--input", action="append", default=[], type=Path, help="input folder (repeatable)")
    ap.add_argument("-o", "--output", type=Path, default=Path(""), help="output folder")
    ap.add_argument("-c", "--codec", choices=list(CODECS), default="x264", metavar="FORMAT",
                    help="target format. Video: " + ", ".join(k for k, c in CODECS.items() if not c.is_audio)
                    + " (default x264). Audio: " + ", ".join(k for k, c in CODECS.items() if c.is_audio)
                    + ". Only files of that kind are converted, everything else is copied")
    ap.add_argument("--crf", type=int, help="video quality (lower = better). Default depends on codec")
    ap.add_argument("--preset", help="video: encoder preset (default medium / 6 for AV1). "
                    "Audio: quality, e.g. mp3 V0 (default) or 320k; aac/opus bitrate like 256k; "
                    "vorbis q6; flac 'level 5'")
    ap.add_argument("--container", choices=list(CONTAINERS), default="same", help="video only")
    ap.add_argument("--reencode-same", action="store_true", help="re-encode even files already in target codec")
    ap.add_argument("--overwrite", action="store_true", help="redo outputs that already exist")
    ap.add_argument("--keep-bit-depth", action="store_true", help="don't force 8-bit yuv420p output")
    ap.add_argument("--priority", choices=list(PRIORITIES), default="low",
                    help="ffmpeg CPU/disk priority: normal, low (default) or idle")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--tui", action="store_true", help="open the terminal UI (pre-filled with the options)")
    mode.add_argument("--gui", action="store_true", help="open the desktop GUI (pre-filled with the options)")
    mode.add_argument("--run", action="store_true", help=argparse.SUPPRESS)  # old name of CLI mode
    ap.add_argument("-V", "--version", action="version", version=f"treeconv {__version__}")
    return ap


def main(argv: Optional[List[str]] = None, force: Optional[str] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    argv = [x for x in argv if not x.startswith("-psn_")]  # old macOS Finder launch argument
    if force is None and _is_gui_build():
        force = "gui"
    parser = build_parser()
    a = parser.parse_args(argv)
    codec = CODECS[a.codec]
    if a.preset and a.preset not in codec.presets:
        parser.error(f"--preset for {a.codec} must be one of: " + ", ".join(codec.presets))
    settings = Settings(
        inputs=a.input, output=a.output, codec=a.codec,
        crf=a.crf if a.crf is not None else codec.default_crf,
        preset=a.preset or codec.default_preset, container=a.container,
        copy_if_same_codec=not a.reencode_same, skip_existing=not a.overwrite,
        eight_bit=not a.keep_bit_depth, priority=a.priority)

    if force:
        mode = force
    elif a.gui:
        mode = "gui"
    elif a.tui:
        mode = "tui"
    elif argv:                                   # options given: plain command-line tool
        mode = "cli"
    elif _launched_from_terminal():
        mode = "tui"
    else:
        if _handoff_to_gui_exe(argv):
            return 0
        mode = "gui"

    if mode == "cli":
        if not settings.inputs or not str(settings.output):
            build_parser().error("command-line mode needs at least one -i/--input and -o/--output "
                                 "(or use --tui / --gui to open a UI)")
        return run_headless(settings)
    if mode == "gui":
        try:
            from gui import run_gui
        except ImportError as exc:
            print(f"The GUI needs Tkinter ({exc}). On Debian/Ubuntu: sudo apt install python3-tk",
                  file=sys.stderr)
            return 1
        return run_gui(settings)
    try:
        import textual  # noqa: F401
    except ImportError:
        print("The terminal UI needs 'textual':  pip install textual   "
              "(or pass -i/-o to use command-line mode)", file=sys.stderr)
        return 1
    return run_tui(settings)


if __name__ == "__main__":
    sys.exit(main())
