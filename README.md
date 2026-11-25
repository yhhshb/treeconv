# treeconv

**Convert a whole folder tree in one go and get the same tree back.** Point treeconv at a folder
(or several) and it rebuilds every subfolder in the output, converting the files that match the
target format and copying everything else unchanged. No selecting files subfolder by subfolder,
and no flattened output where every file lands in one folder.

```
Shows/                                  out/Shows/
├── Show A/                             ├── Show A/
│   ├── Season 1/                       │   ├── Season 1/
│   │   ├── E01.mkv     (x265)    →     │   │   ├── E01.mkv     (x264, all audio + subtitles kept)
│   │   ├── E01.en.srt                  │   │   ├── E01.en.srt  (copied)
│   │   └── E02.mkv                     │   │   └── E02.mkv
│   └── poster.jpg                      │   └── poster.jpg      (copied)
└── Show B/ …                           └── Show B/ …
```

It works for video (H.264, H.265, AV1, with an optional resolution limit) and audio (MP3, AAC,
Opus, Vorbis, FLAC, ALAC), and comes with a desktop GUI, a terminal UI and a command-line mode,
all powered by ffmpeg.

## Download

Ready-to-run packages are on the [Releases](https://github.com/yhhshb/treeconv/releases) page.
treeconv itself needs nothing else, but it uses **ffmpeg**, which you install separately:
`winget install ffmpeg` (Windows), `brew install ffmpeg` (macOS), `sudo apt install ffmpeg` (Linux).
On Windows you can also put `ffmpeg.exe` and `ffprobe.exe` in treeconv's folder.

| System | File | |
|---|---|---|
| Windows | `…-windows-x64-setup.exe` | Installer: Start-menu shortcut, optional `treeconv` on PATH |
| Windows | `…-windows-x64.zip` | Portable: unzip, double-click `treeconv-gui.exe` |
| macOS | `…-macos-arm64.dmg` (Apple silicon), `…-macos-x64.dmg` (Intel) | Drag `treeconv.app` to Applications |
| Linux | `…-linux-x64.AppImage`, `…-linux-arm64.AppImage` | `chmod +x`, then double-click or run it |
| Linux | `…-linux-x64.tar.gz` | Plain folder, for servers or your own packaging |

The builds are not code-signed yet, so the first launch shows a warning: on Windows click
*More info → Run anyway*; on macOS open *System Settings → Privacy & Security* and click
*Open Anyway* (or run `xattr -dr com.apple.quarantine /Applications/treeconv.app`).

## How it starts

The same program has three faces:

- **Double-clicked** (desktop, Start menu, Finder, file manager): the desktop GUI.
- **Run from a terminal with no options**: the terminal UI.
- **Run with options**: a plain command-line tool, e.g. for scripts:

      treeconv -i ~/Shows -i ~/Films -o ~/out --codec x265 --priority idle
      treeconv -i ~/Films -o ~/Films-1080p --resolution 1080p  # 4K -> 1080p, the rest untouched
      treeconv -i ~/Music -o ~/Music-mp3 -c mp3                # FLAC library -> MP3 (V0)

`--tui` or `--gui` open a UI pre-filled with the given options. `treeconv --help` lists all options.

The TUI and GUI share the same layout, options and keys: `Ctrl+S` start, `Ctrl+R` scan,
`Ctrl+X` cancel, `Del` remove selected input, `Ctrl+Q` quit (`Cmd` instead of `Ctrl` on macOS in the GUI).

To type `treeconv` in a terminal:

- Windows: tick "Add treeconv to PATH" in the installer (or use `treeconv.exe` from the zip).
- macOS: `sudo ln -s /Applications/treeconv.app/Contents/MacOS/treeconv /usr/local/bin/treeconv`
- Linux: move the AppImage to a folder on your PATH, e.g. `~/.local/bin/treeconv`.

## Run from source

    pip install -r requirements.txt  # textual, for the terminal UI
    python treeconv.py               # same rules as above (python gui.py always opens the GUI)

The GUI uses Tkinter from the standard library (on Debian/Ubuntu: `sudo apt install python3-tk`).

## Building the packages

    pip install -r packaging/requirements.txt
    python packaging/build.py        # build + package + smoke test, output in dist/release/

PyInstaller cannot cross-compile, so each platform is built on its own system. The
[release workflow](.github/workflows/release.yml) does this on GitHub for all of them: bump
`__version__` in `core.py`, then push a matching tag (`git tag v0.1.0 && git push --tags`). It
builds and tests every package and creates a draft release with them attached; review it and
press *Publish*. Windows installers need [Inno Setup](https://jrsoftware.org/isinfo.php).

## What it does

- `out/<input name>/…` is created for every input folder, with the same tree (empty folders too).
- The **target format** decides what is converted. A video codec (x264, x265, AV1) converts video
  files; an audio format (MP3, AAC, Opus, Vorbis, FLAC, ALAC) converts audio files. Everything else
  is copied unchanged, so with an audio target, videos and their soundtracks are left alone.
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
- **Resolution** (`--resolution`, "Picture" row): cap videos at 2160p, 1440p, 1080p, 720p or 480p.
  The limit applies to the short side, so a vertical phone video (2160×3840) becomes 1080×1920 at
  1080p. Larger videos are downscaled with the aspect ratio kept; smaller ones are never upscaled.
  A video already in the target codec but too large is re-encoded instead of copied.
  This is separate from the encoder **preset** (ultrafast … veryslow), which only trades encoding
  time for file size at the same quality and resolution, and from **CRF**, which sets the quality.
- "8-bit 4:2:0 output" (on by default) makes 10-bit x265 sources play on devices that only
  support standard H.264. HDR sources are not tone-mapped (colours will look washed out).
- Data streams (e.g. MP4 timecode tracks) are dropped.

### Audio targets

| Format | Output | Quality (`--preset`, default first) |
|---|---|---|
| `mp3` | `.mp3` | `V0` (LAME VBR ~245 kbps), `V2`, `V4`, `320k`, `256k`, `192k`, `128k` |
| `aac` | `.m4a` | `256k`, `320k`, `192k`, `128k`, `96k` |
| `opus` | `.opus` | `160k`, `256k`, `192k`, `128k`, `96k`, `64k` |
| `vorbis` | `.ogg` | `q6`, `q10` … `q3` |
| `flac` | `.flac` | `level 5`, `level 0` … `level 12` (all lossless; higher = smaller, slower) |
| `alac` | `.m4a` | lossless, no options |

- Tags (title, artist, album, track…) are kept, from any source (FLAC/Vorbis comments, ID3, MP4).
  Embedded cover art is kept in MP3, M4A and FLAC (Ogg/Opus outputs drop it). Folder images
  such as `cover.jpg` are copied like any other file.
- Sample rate and channels are adapted only when the format requires it (e.g. 96 kHz → 48 kHz
  and 5.1 → stereo for MP3; Opus is always 48 kHz).
- Files already in the target format are copied (turn off "Copy if already in target format" to
  re-encode them). CRF, container and 8-bit only apply to video, so they are greyed out.
- If two sources would produce the same file (`song.flac` and `song.wav` → `song.mp3`), the first
  one wins and the other is reported and skipped.
- ffmpeg uses all CPU cores on one file at a time, but runs at **low priority** by default
  (`--priority low`), so other programs get the CPU first and encoding only uses spare capacity.
  `--priority idle` runs it only when the CPU would otherwise be idle (Linux: `SCHED_IDLE` and idle
  disk I/O; Windows: idle priority class); `--priority normal` turns this off for the fastest
  encodes.

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

## About this project and AI use

treeconv's code was written by an AI assistant (Claude), following my requirements. 
I needed the tool quickly, and nothing in it is novel: the heavy lifting is
done by ffmpeg, and the rest (walking a folder tree, mapping streams, packaging) is well-known,
widely documented work. Using AI let me get a working, tested tool in a short time instead of
writing that routine code by hand. The behaviour has been tested on sample files, but please
report anything that looks wrong in the [issues](https://github.com/yhhshb/treeconv/issues).
