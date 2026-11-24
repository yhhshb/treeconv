"""Core logic for vidconv: validation, scanning, probing and ffmpeg conversion.

No UI code lives here, so it can be driven by the TUI or by the headless CLI.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- #
# Static tables
# --------------------------------------------------------------------------- #

VIDEO_EXTS = {
    ".mkv", ".mp4", ".m4v", ".mov", ".avi", ".wmv", ".flv", ".webm", ".ts",
    ".m2ts", ".mts", ".mpg", ".mpeg", ".vob", ".3gp", ".ogv", ".divx", ".asf",
}

X26X_PRESETS = ("ultrafast", "superfast", "veryfast", "faster", "fast",
                "medium", "slow", "slower", "veryslow")


@dataclass(frozen=True)
class Codec:
    key: str
    label: str
    encoder: str            # ffmpeg encoder name
    codec_name: str         # ffprobe codec_name of streams already in this codec
    default_crf: int
    presets: Tuple[str, ...]
    default_preset: str


CODECS: Dict[str, Codec] = {
    "x264": Codec("x264", "H.264 / AVC (libx264)", "libx264", "h264", 20, X26X_PRESETS, "medium"),
    "x265": Codec("x265", "H.265 / HEVC (libx265)", "libx265", "hevc", 22, X26X_PRESETS, "medium"),
    "av1": Codec("av1", "AV1 (libsvtav1)", "libsvtav1", "av1", 30,
                 tuple(str(i) for i in range(13, -1, -1)), "6"),
}

CONTAINERS = {
    "same": "Same as source (MKV fallback)",
    ".mkv": "Matroska (.mkv)",
    ".mp4": "MP4 (.mp4)",
}

# Which video codecs each container can hold (by extension).
_VIDEO_OK: Dict[str, set] = {
    ".mkv": {"h264", "hevc", "av1"},
    ".mp4": {"h264", "hevc", "av1"},
    ".m4v": {"h264", "hevc", "av1"},
    ".mov": {"h264", "hevc", "av1"},
    ".webm": {"av1"},
    ".ts": {"h264", "hevc"},
    ".m2ts": {"h264", "hevc"},
    ".mts": {"h264", "hevc"},
}

_MP4_FAMILY = {".mp4", ".m4v", ".mov"}
_TS_FAMILY = {".ts", ".m2ts", ".mts"}

# Subtitle handling per container: codec -> output codec ("copy" or encoder).
_TEXT_SUBS = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text"}
_SUBS_MKV = {**{c: "copy" for c in ("subrip", "ass", "ssa", "webvtt",
                                     "hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle")},
             "mov_text": "srt", "text": "srt", "srt": "srt"}
# ASS/SSA are deliberately absent: converting them to mov_text loses styling,
# so such files fall back to MKV instead.
_SUBS_MP4 = {"mov_text": "copy", "dvd_subtitle": "copy",
             **{c: "mov_text" for c in ("subrip", "srt", "webvtt", "text")}}
_SUBS_WEBM = {"webvtt": "copy",
              **{c: "webvtt" for c in ("subrip", "srt", "ass", "ssa", "mov_text", "text")}}
_SUBS_TS = {"dvb_subtitle": "copy", "dvb_teletext": "copy"}

_AUDIO_MP4 = {"aac", "mp3", "ac3", "eac3", "alac", "flac", "opus", "mp2"}
_AUDIO_WEBM = {"opus", "vorbis"}
_AUDIO_TS = {"aac", "mp3", "ac3", "eac3", "mp2", "dts", "truehd", "opus"}


def _sub_table(ext: str) -> Optional[Dict[str, str]]:
    if ext == ".mkv":
        return _SUBS_MKV
    if ext in _MP4_FAMILY:
        return _SUBS_MP4
    if ext == ".webm":
        return _SUBS_WEBM
    if ext in _TS_FAMILY:
        return _SUBS_TS
    return None


def _audio_ok(ext: str, codec: str) -> bool:
    if ext == ".mkv":
        return True
    if ext in _MP4_FAMILY:
        return codec in _AUDIO_MP4 or codec.startswith("pcm_") and ext == ".mov"
    if ext == ".webm":
        return codec in _AUDIO_WEBM
    if ext in _TS_FAMILY:
        return codec in _AUDIO_TS
    return False


# --------------------------------------------------------------------------- #
# Settings / validation
# --------------------------------------------------------------------------- #

@dataclass
class Settings:
    inputs: List[Path]
    output: Path
    codec: str = "x264"
    crf: int = 20
    preset: str = "medium"
    container: str = "same"          # "same", ".mkv" or ".mp4"
    copy_if_same_codec: bool = True  # copy files already in the target codec
    skip_existing: bool = True       # resume: skip outputs that already exist
    eight_bit: bool = True           # force yuv420p (best device compatibility)


def tool_problems(codec_key: str) -> List[str]:
    """Return a list of problems with the ffmpeg installation (empty = OK)."""
    problems = []
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            problems.append(f"'{tool}' not found on PATH. Install ffmpeg first.")
    if problems:
        return problems
    enc = CODECS[codec_key].encoder
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                             capture_output=True, text=True, timeout=30).stdout
        if f" {enc} " not in out:
            problems.append(f"Your ffmpeg build has no '{enc}' encoder.")
    except Exception as exc:  # pragma: no cover
        problems.append(f"Could not query ffmpeg encoders: {exc}")
    return problems


def _within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def validate(inputs: Sequence[Path], output: Optional[Path]) -> List[str]:
    """Return a list of human readable errors (empty list = valid)."""
    errors: List[str] = []
    if not inputs:
        errors.append("Add at least one input folder.")
    if output is None or str(output).strip() == "":
        errors.append("Choose an output folder.")
        return errors

    out = Path(output).expanduser().resolve()
    if out.exists() and not out.is_dir():
        errors.append(f"Output path exists and is not a folder: {out}")

    seen: Dict[str, Path] = {}
    for raw in inputs:
        inp = Path(raw).expanduser().resolve()
        if not inp.is_dir():
            errors.append(f"Input folder does not exist: {inp}")
            continue
        if not inp.name:
            errors.append(f"Cannot use a filesystem root as input: {inp}")
            continue
        if inp == out:
            errors.append(f"Output folder must be different from input folder: {inp}")
            continue
        if _within(out, inp):
            errors.append(f"Output folder must not be inside input folder {inp}")
            continue
        dest = out / inp.name
        if dest == inp or _within(inp, dest):
            errors.append(f"Destination {dest} would overwrite input folder {inp}")
            continue
        if inp.name in seen:
            errors.append(f"Two input folders share the name '{inp.name}' "
                          f"({seen[inp.name]} and {inp}); they would collide in the output.")
            continue
        seen[inp.name] = inp
    return errors


# --------------------------------------------------------------------------- #
# Scanning
# --------------------------------------------------------------------------- #

@dataclass
class Job:
    kind: str          # "video" or "copy"
    src: Path
    dst: Path          # for videos: destination with the *source* extension (final ext decided at run time)
    size: int


@dataclass
class Plan:
    jobs: List[Job] = field(default_factory=list)
    dirs: List[Path] = field(default_factory=list)

    @property
    def videos(self) -> List[Job]:
        return [j for j in self.jobs if j.kind == "video"]

    @property
    def others(self) -> List[Job]:
        return [j for j in self.jobs if j.kind == "copy"]

    @property
    def total_bytes(self) -> int:
        return sum(j.size for j in self.jobs)


def is_temp_name(name: str) -> bool:
    return ".vidconv-tmp" in name


def scan(inputs: Sequence[Path], output: Path) -> Plan:
    plan = Plan()
    out = Path(output).expanduser().resolve()
    for raw in inputs:
        inp = Path(raw).expanduser().resolve()
        dest_root = out / inp.name
        plan.dirs.append(dest_root)
        for root, dirnames, filenames in os.walk(inp):
            dirnames.sort()
            rel = Path(root).relative_to(inp)
            for d in dirnames:
                plan.dirs.append(dest_root / rel / d)
            for name in sorted(filenames):
                if is_temp_name(name):
                    continue
                src = Path(root) / name
                try:
                    size = src.stat().st_size
                except OSError:
                    continue
                kind = "video" if src.suffix.lower() in VIDEO_EXTS else "copy"
                plan.jobs.append(Job(kind, src, dest_root / rel / name, size))
    return plan


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# --------------------------------------------------------------------------- #
# Probing and ffmpeg command building
# --------------------------------------------------------------------------- #

def probe(path: Path) -> dict:
    res = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_streams", "-show_format", str(path)],
        capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or "ffprobe failed")
    return json.loads(res.stdout or "{}")


def _is_cover(stream: dict) -> bool:
    return bool(stream.get("disposition", {}).get("attached_pic")) or \
        stream.get("codec_name") in ("mjpeg", "png", "bmp", "gif")


def main_video_streams(info: dict) -> List[dict]:
    return [s for s in info.get("streams", [])
            if s.get("codec_type") == "video" and not _is_cover(s)]


@dataclass
class StreamPlan:
    ext: str                                  # output extension
    maps: List[Tuple[int, str, str]]          # (input index, kind, action: 'encode'|'copy'|<sub encoder>)
    dropped: List[str]                        # human readable descriptions of dropped streams
    notes: List[str]


def _describe(s: dict) -> str:
    lang = s.get("tags", {}).get("language", "")
    title = s.get("tags", {}).get("title", "")
    extra = ", ".join(x for x in (lang, title) if x)
    return f"#{s.get('index')} {s.get('codec_type')}/{s.get('codec_name')}" + (f" ({extra})" if extra else "")


def _plan_for_ext(info: dict, ext: str, target: str, remux: bool) -> Tuple[List[Tuple[int, str, str]], List[str]]:
    """Return (maps, unsupported) for a given output container extension."""
    maps: List[Tuple[int, str, str]] = []
    unsupported: List[str] = []
    subs = _sub_table(ext)
    for s in info.get("streams", []):
        idx, ctype, cname = s["index"], s.get("codec_type"), s.get("codec_name", "")
        if ctype == "video":
            if _is_cover(s):
                if ext in _MP4_FAMILY or ext == ".mkv":
                    maps.append((idx, "cover", "copy"))
                # covers elsewhere are silently dropped (not a subtitle / not content)
            elif target in _VIDEO_OK.get(ext, set()):
                maps.append((idx, "video", "copy" if remux else "encode"))
            else:
                unsupported.append(_describe(s))
        elif ctype == "audio":
            if _audio_ok(ext, cname):
                maps.append((idx, "audio", "copy"))
            else:
                unsupported.append(_describe(s))
        elif ctype == "subtitle":
            action = subs.get(cname) if subs else None
            if action:
                maps.append((idx, "subtitle", action))
            else:
                unsupported.append(_describe(s))
        elif ctype == "attachment":
            if ext == ".mkv":
                maps.append((idx, "attachment", "copy"))
            else:
                unsupported.append(_describe(s))
        # data streams (timecodes, chapters-as-data, ...) are dropped on purpose
    return maps, unsupported


def plan_streams(info: dict, src_ext: str, container: str, target: str, remux: bool = False) -> StreamPlan:
    """Pick the output container and per-stream actions. `target` is an ffprobe codec name."""
    src_ext = src_ext.lower()
    wanted = src_ext if container == "same" else container
    candidates = [wanted] if wanted == ".mkv" else [wanted, ".mkv"]
    notes: List[str] = []
    best = None
    for ext in candidates:
        maps, unsupported = _plan_for_ext(info, ext, target, remux)
        has_video = any(k == "video" for _, k, _ in maps)
        if has_video and not unsupported:
            if ext != wanted:
                notes.append(f"container switched {wanted} -> {ext} so every stream (incl. subtitles) is kept")
            return StreamPlan(ext, maps, [], notes)
        if best is None or (has_video and len(unsupported) < len(best[2])):
            best = (ext, maps, unsupported)
    ext, maps, unsupported = best  # type: ignore[misc]
    if ext != wanted:
        notes.append(f"container switched {wanted} -> {ext}")
    return StreamPlan(ext, maps, unsupported, notes)


def build_ffmpeg_cmd(src: Path, tmp: Path, sp: StreamPlan, s: Settings) -> List[str]:
    codec = CODECS[s.codec]
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
           "-nostats", "-progress", "pipe:1", "-i", str(src)]
    for idx, _, _ in sp.maps:
        cmd += ["-map", f"0:{idx}"]
    cmd += ["-map_metadata", "0", "-map_chapters", "0"]
    for o, (_, kind, action) in enumerate(sp.maps):
        if kind == "video" and action == "encode":
            cmd += [f"-c:{o}", codec.encoder, f"-crf:{o}", str(s.crf), f"-preset:{o}", s.preset]
            if s.eight_bit:
                cmd += [f"-pix_fmt:{o}", "yuv420p"]
            if s.codec == "x265":
                cmd += [f"-x265-params:{o}", "log-level=error"]
                if sp.ext in _MP4_FAMILY:
                    cmd += [f"-tag:{o}", "hvc1"]
        else:
            cmd += [f"-c:{o}", action]
    if sp.ext in _MP4_FAMILY:
        cmd += ["-movflags", "+faststart"]
    cmd += ["-max_muxing_queue_size", "9999", str(tmp)]
    return cmd


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #

class Callbacks:
    """Override any of these; all are called from the worker thread."""

    def log(self, msg: str, level: str = "info") -> None: ...
    def file_start(self, index: int, total: int, job: Job) -> None: ...
    def file_progress(self, fraction: float, speed: str) -> None: ...
    def overall(self, fraction: float) -> None: ...
    def finished(self, summary: Dict[str, int], cancelled: bool) -> None: ...


class Converter:
    def __init__(self, settings: Settings, plan: Plan, cb: Callbacks):
        self.s = settings
        self.plan = plan
        self.cb = cb
        self._cancel = threading.Event()
        self._proc: Optional[subprocess.Popen] = None

    # -- control ------------------------------------------------------------ #
    def cancel(self) -> None:
        self._cancel.set()
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    # -- main loop ------------------------------------------------------------ #
    def run(self) -> Dict[str, int]:
        summary = {"converted": 0, "copied_video": 0, "copied": 0, "skipped": 0, "failed": 0}
        for d in self.plan.dirs:
            d.mkdir(parents=True, exist_ok=True)
        total_bytes = max(1, self.plan.total_bytes)
        done_bytes = 0
        jobs = self.plan.jobs
        for i, job in enumerate(jobs, 1):
            if self.cancelled:
                break
            self.cb.file_start(i, len(jobs), job)
            base = done_bytes

            def on_progress(frac: float, speed: str = "", _base=base, _size=job.size) -> None:
                self.cb.file_progress(frac, speed)
                self.cb.overall((_base + frac * _size) / total_bytes)

            try:
                status = self._do_video(job, on_progress) if job.kind == "video" \
                    else self._do_copy(job)
                summary[status] += 1
            except Exception as exc:
                if self.cancelled:
                    break
                summary["failed"] += 1
                self.cb.log(f"FAILED {job.src}: {exc}", "error")
            done_bytes += job.size
            self.cb.file_progress(1.0, "")
            self.cb.overall(done_bytes / total_bytes)
        self.cb.finished(summary, self.cancelled)
        return summary

    # -- plain copy ----------------------------------------------------------- #
    def _do_copy(self, job: Job) -> str:
        if self.s.skip_existing and job.dst.exists():
            st_s, st_d = job.src.stat(), job.dst.stat()
            if st_s.st_size == st_d.st_size and int(st_d.st_mtime) >= int(st_s.st_mtime):
                return "skipped"
        job.dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(job.src, job.dst)
        self.cb.log(f"copied   {job.src.name}", "dim")
        return "copied"

    # -- video ---------------------------------------------------------------- #
    def _do_video(self, job: Job, on_progress: Callable[..., None]) -> str:
        codec = CODECS[self.s.codec]
        info = probe(job.src)
        vids = main_video_streams(info)
        if not vids:
            self.cb.log(f"no video stream in {job.src.name}; copying as-is", "warn")
            return self._do_copy(job)

        remux = self.s.copy_if_same_codec and all(v.get("codec_name") == codec.codec_name for v in vids)
        if remux and self.s.container == "same":
            if self.s.skip_existing and job.dst.exists():
                return "skipped"
            shutil.copy2(job.src, job.dst)
            self.cb.log(f"already {codec.codec_name}, copied {job.src.name}", "dim")
            return "copied_video"

        sp = plan_streams(info, job.src.suffix, self.s.container, codec.codec_name, remux)
        if not any(k == "video" for _, k, _ in sp.maps):
            raise RuntimeError(f"no container can hold {codec.codec_name} video for this file")
        dst = job.dst if sp.ext == job.src.suffix.lower() else job.dst.with_suffix(sp.ext)
        if self.s.skip_existing and dst.exists():
            self.cb.log(f"exists, skipped {dst.name}", "dim")
            return "skipped"

        for n in sp.notes:
            self.cb.log(f"{job.src.name}: {n}", "warn")
        for d in sp.dropped:
            self.cb.log(f"{job.src.name}: stream cannot be kept in {sp.ext}, dropped: {d}", "warn")

        n_subs = sum(1 for _, k, _ in sp.maps if k == "subtitle")
        n_aud = sum(1 for _, k, _ in sp.maps if k == "audio")
        verb = "remuxing" if remux else "encoding"
        self.cb.log(f"{verb} {job.src.name}  [{n_aud} audio, {n_subs} subtitle stream(s)]")

        duration = float(info.get("format", {}).get("duration") or 0) or \
            max((float(v.get("duration") or 0) for v in vids), default=0.0)
        tmp = dst.with_name(f".{dst.stem}.vidconv-tmp{sp.ext}")
        cmd = build_ffmpeg_cmd(job.src, tmp, sp, self.s)
        try:
            self._run_ffmpeg(cmd, duration, on_progress)
            if self.cancelled:
                raise RuntimeError("cancelled")
            os.replace(tmp, dst)
            shutil.copystat(job.src, dst)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
        return "copied_video" if remux else "converted"

    def _run_ffmpeg(self, cmd: List[str], duration: float, on_progress: Callable[..., None]) -> None:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, bufsize=1)
        self._proc = proc
        err_lines: List[str] = []

        def drain() -> None:
            assert proc.stderr is not None
            for line in proc.stderr:
                err_lines.append(line.rstrip())
                del err_lines[:-30]

        t = threading.Thread(target=drain, daemon=True)
        t.start()
        speed = ""
        assert proc.stdout is not None
        for line in proc.stdout:
            key, _, val = line.strip().partition("=")
            if key == "speed":
                speed = val.strip()
            elif key in ("out_time_us", "out_time_ms") and duration > 0:
                try:
                    secs = int(val) / 1_000_000
                except ValueError:
                    continue
                on_progress(min(max(secs / duration, 0.0), 1.0), speed)
        proc.wait()
        t.join(timeout=2)
        self._proc = None
        if proc.returncode != 0 and not self.cancelled:
            msg = "\n".join(err_lines[-8:]) or f"ffmpeg exited with code {proc.returncode}"
            raise RuntimeError(msg)
