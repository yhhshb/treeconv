# treeconv

Batch video converter with a terminal UI (Textual + ffmpeg). Mirrors input folders into an
output folder, re-encoding videos (default: x265 → x264) and copying everything else as-is.

## Install

    sudo apt install ffmpeg          # or brew install ffmpeg
    pip install -r requirements.txt

## Run

    python treeconv.py                                    # TUI
    python treeconv.py -i ~/Shows -i ~/Films -o ~/out     # TUI, pre-filled
    python treeconv.py -i ~/Shows -o ~/out --run          # headless (no TUI)

`python treeconv.py --help` lists all options (codec, CRF, preset, container, …).

Keys: `Ctrl+S` start, `Ctrl+R` scan, `Ctrl+X` cancel, `Del` remove selected input, `Ctrl+Q` quit.

## What it does

- `out/<input name>/…` is created for every input folder, with the same tree (empty folders too).
- Refuses to run if output == an input, output is inside an input, an input would be
  overwritten, or two inputs share a folder name.
- Video files are re-encoded; **all** audio tracks, embedded subtitles (with language, title,
  default/forced flags), chapters, metadata and MKV attachments (ASS fonts) are kept.
  Audio is copied, not re-encoded.
- Other files (external `.srt`/`.ass`, `.nfo`, images…) are copied unchanged. Since video
  basenames are kept, external subtitles still match their video.
- Container defaults to "same as source". If a stream can't live in that container
  (e.g. WebM can't hold H.264, MP4 can't hold PGS/ASS subtitles or fonts), the file is written
  as `.mkv` instead so nothing is lost; this is logged.
- Videos already in the target codec are copied (or only remuxed if you forced a container).
- Encodes go to a hidden temp file and are renamed when complete, so an interrupted run never
  leaves half files; re-running with "Skip existing" resumes where it stopped.
- "8-bit 4:2:0 output" (on by default) makes 10-bit x265 sources play on devices that only
  support standard H.264. HDR sources are not tone-mapped (colours will look washed out).
- Data streams (e.g. MP4 timecode tracks) are dropped.

## Why not X?

**HandBrake** — Great encoder with excellent presets, but it's built around single titles/files.
Batch mode means adding files to a queue, and output goes into one folder, so the tree is lost.
It always re-encodes video (no stream copy), doesn't keep MKV attachments (ASS fonts), and
doesn't copy non-video files. treeconv uses ffmpeg directly, so it can copy or remux what
doesn't need re-encoding and keep every stream.

**HandBrakeCLI / ffmpeg shell scripts** — You can rebuild the tree with `find` and a loop, and many
gists do. Then you're also handling odd file names, stream mapping, container compatibility,
temp files on interruption, resuming, and copying the non-video files. treeconv does all of
that, and shows the plan before it starts.

**Tdarr** — Powerful, but it's a server: a web UI, worker nodes, libraries, plugin/flow stacks.
It's a good fit for an always-on *arr stack, and too much for "convert this folder tree once".
treeconv is a single script with no setup.

**HBBatchBeast** — The closest desktop equivalent (it mirrors the source tree), but it's no
longer actively developed; its author moved on to Tdarr.

**FFmpeg Batch AV Converter** — Can keep the source path structure, but it's Windows-only and
expects you to write the ffmpeg parameters yourself.

**dBpoweramp Batch Video Converter, MediaCoder, …** — Some can preserve the source path, but
they're closed-source and mostly Windows-only.

treeconv's niche: free, open source, runs anywhere Python and ffmpeg do, and its defaults
already give you a complete mirrored copy of your library with nothing lost.
