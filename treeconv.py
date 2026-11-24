#!/usr/bin/env python3
"""vidconv - batch video converter with a terminal UI.

Mirrors one or more input folders into an output folder, re-encoding every
video (default: to H.264/x264) while keeping all audio tracks, embedded
subtitles, chapters, metadata and attachments. Every other file (external
.srt/.ass subtitles, .nfo, artwork, ...) is copied as-is.

Usage:
    python vidconv.py                                  # interactive TUI
    python vidconv.py -i /media/Shows -i /media/Films -o /media/out   # TUI, pre-filled
    python vidconv.py -i /media/Shows -o /media/out --run             # headless
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

from core import (CODECS, CONTAINERS, Callbacks, Converter, Job, Plan, Settings,
                  human_size, scan, tool_problems, validate)


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
        print(("Cancelled. " if cancelled else "Done. ") + _summary_text(summary))


def _summary_text(s: Dict[str, int]) -> str:
    return (f"{s['converted']} converted, {s['copied_video']} videos already in target codec, "
            f"{s['copied']} other files copied, {s['skipped']} skipped, {s['failed']} failed")


def run_headless(settings: Settings) -> int:
    errors = validate(settings.inputs, settings.output) + tool_problems(settings.codec)
    if errors:
        for e in errors:
            print("error:", e, file=sys.stderr)
        return 2
    plan = scan(settings.inputs, settings.output)
    print(f"{len(plan.videos)} videos, {len(plan.others)} other files, {human_size(plan.total_bytes)}")
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
        def __init__(self, app: "VidConvApp") -> None:
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

    class VidConvApp(App):
        TITLE = "vidconv"
        SUB_TITLE = "batch video converter - keeps every subtitle"
        CSS = """
        #config { height: auto; max-height: 55%; padding: 0 1; }
        .row { height: auto; }
        .row > * { margin-right: 1; }
        .lbl { width: 16; padding-top: 1; color: $text-muted; }
        #in_path, #out_path { width: 1fr; }
        #in_list { height: 5; border: round $panel; }
        #codec { width: 30; } #container { width: 36; } #preset { width: 16; }
        #crf { width: 8; }
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
                    yield Checkbox("Copy videos already in target codec", self.s.copy_if_same_codec, id="opt_same")
                    yield Checkbox("Skip existing outputs (resume)", self.s.skip_existing, id="opt_skip")
                    yield Checkbox("8-bit 4:2:0 output", self.s.eight_bit, id="opt_8bit")
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
            self.query_one("#crf", Input).value = str(codec.default_crf)

        @on(Button.Pressed, "#scan")
        def action_scan(self) -> None:
            s = self._collect()
            if not s or not self._check(s):
                return
            plan = scan(s.inputs, s.output)
            self.log_line(f"Scan: {len(plan.videos)} video file(s) "
                          f"({human_size(sum(j.size for j in plan.videos))}), "
                          f"{len(plan.others)} other file(s), {len(plan.dirs)} folder(s).", "ok")
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
            plan = scan(s.inputs, s.output)
            if not plan.jobs and not plan.dirs:
                self.notify("Nothing to do", severity="warning")
                return
            self.log_line(f"Starting: {len(plan.videos)} video(s), {len(plan.others)} other file(s) "
                          f"-> {s.output}", "ok")
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
                self.log_line(("Cancelled. " if cancelled else "Done. ") + _summary_text(summary), level)
                self.notify(_summary_text(summary), timeout=10)

    return VidConvApp(settings)


def run_tui(settings: Settings) -> int:
    build_app(settings).run()
    return 0


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-i", "--input", action="append", default=[], type=Path, help="input folder (repeatable)")
    ap.add_argument("-o", "--output", type=Path, default=Path(""), help="output folder")
    ap.add_argument("-c", "--codec", choices=list(CODECS), default="x264", help="target codec (default x264)")
    ap.add_argument("--crf", type=int, help="quality (lower = better). Default depends on codec")
    ap.add_argument("--preset", help="encoder preset (default: medium / 6 for AV1)")
    ap.add_argument("--container", choices=list(CONTAINERS), default="same")
    ap.add_argument("--reencode-same", action="store_true", help="re-encode even files already in target codec")
    ap.add_argument("--overwrite", action="store_true", help="redo outputs that already exist")
    ap.add_argument("--keep-bit-depth", action="store_true", help="don't force 8-bit yuv420p output")
    ap.add_argument("--run", action="store_true", help="run headless (no TUI)")
    a = ap.parse_args(argv)
    codec = CODECS[a.codec]
    settings = Settings(
        inputs=a.input, output=a.output, codec=a.codec,
        crf=a.crf if a.crf is not None else codec.default_crf,
        preset=a.preset or codec.default_preset, container=a.container,
        copy_if_same_codec=not a.reencode_same, skip_existing=not a.overwrite,
        eight_bit=not a.keep_bit_depth)
    if a.run:
        return run_headless(settings)
    try:
        import textual  # noqa: F401
    except ImportError:
        print("The TUI needs 'textual':  pip install textual   (or use --run for headless mode)",
              file=sys.stderr)
        return 1
    return run_tui(settings)


if __name__ == "__main__":
    sys.exit(main())
