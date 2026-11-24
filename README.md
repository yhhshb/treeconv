# vidconv

Batch video converter with a terminal UI (Textual + ffmpeg). Mirrors input folders into an
output folder, re-encoding videos (default: x265 → x264) and copying everything else as-is.

## Install

    sudo apt install ffmpeg          # or brew install ffmpeg
    pip install -r requirements.txt

## Run

    python vidconv.py                                    # TUI
    python vidconv.py -i ~/Shows -i ~/Films -o ~/out     # TUI, pre-filled
    python vidconv.py -i ~/Shows -o ~/out --run          # headless (no TUI)

`python vidconv.py --help` lists all options (codec, CRF, preset, container, …).

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
