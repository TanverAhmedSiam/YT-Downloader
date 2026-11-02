# ytdl-studio

A production-quality command-line downloader and converter for YouTube and
YouTube Music. It uses yt-dlp's Python API (no fragile subprocess parsing),
FFmpeg for conversion and tagging, and Rich for a live, readable interface.

- Unlimited downloads, playlists, channels, mixes, albums, and batch files
- Full quality range: 8K/4K/1440p/1080p/720p/480p and codec-specific presets
- Video containers: MP4 (H.264/H.265), MKV, WebM (VP9/AV1), MOV, animated GIF
- Audio: MP3, M4A/AAC, Opus, Ogg Vorbis, FLAC, WAV, ALAC
- Tags, cover art, chapters, subtitles, SponsorBlock, and `.info.json` sidecars
- Resume, archive, retries with backoff, bounded concurrency, rate limiting
- Machine-readable `--json` output and `--dry-run` planning
- A warm-dark, minimal local studio interface (`ytdl gui`) for point-and-click downloads
- Cross-platform diagnostics with install hints via `ytdl doctor`

## Requirements

- Python 3.11 or newer
- FFmpeg and ffprobe (required for merging, conversion, covers, and tagging)
- Optional: [aria2c](https://aria2.github.io/) for accelerated downloads from
  sources that support parallel range requests. YouTube uses the native
  downloader because its signed media URLs reject aria2c's range requests with
  HTTP 403. The studio detects an existing install, and on Windows can fetch the
  official static build into `%LOCALAPPDATA%\ytdl-studio\bin` from the system
  check.

### Install FFmpeg

| Platform | Command |
| --- | --- |
| Windows | `winget install Gyan.FFmpeg` (or `choco install ffmpeg`) |
| macOS | `brew install ffmpeg` |
| Debian/Ubuntu | `sudo apt install ffmpeg` |
| Fedora | `sudo dnf install ffmpeg` |
| Arch | `sudo pacman -S ffmpeg` |

Verify both tools are visible:

```console
ffmpeg -version
ffprobe -version
ytdl doctor
```

### Install ytdl-studio

From this project folder:

```console
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate

python -m pip install --upgrade pip
python -m pip install -e .
```

For an isolated, always-available command, use pipx:

```console
pipx install .
```

The console command is `ytdl`; `python -m ytdl` behaves identically.

## Quick start

```console
# The default command is download
ytdl https://www.youtube.com/watch?v=VIDEO_ID

# Pick a resolution
ytdl download https://www.youtube.com/watch?v=VIDEO_ID -q 1080p

# Extract tagged MP3 at 320 kbps
ytdl audio https://www.youtube.com/watch?v=VIDEO_ID -f mp3 -b 320

# Download the first five items of a playlist
ytdl playlist https://www.youtube.com/playlist?list=PLAYLIST_ID \
  --playlist-start 1 --playlist-end 5

# Inspect before downloading
ytdl info https://www.youtube.com/watch?v=VIDEO_ID
ytdl formats https://www.youtube.com/watch?v=VIDEO_ID

# Check the environment, including aria2c
ytdl doctor

# Windows: fetch a managed aria2c when no copy is available
ytdl doctor --install-aria2c
```

Use `ytdl COMMAND --help` for the complete option list.

## Commands

| Command | Purpose |
| --- | --- |
| `ytdl URL...` | Shorthand for `ytdl download URL...` |
| `ytdl download URL...` | Download one or more videos, playlists, channels, or albums |
| `ytdl audio URL...` | Extract audio, with tags and cover art |
| `ytdl playlist URL` | Download a playlist/album/channel with ranges, shuffle, and reverse |
| `ytdl batch FILE` | Read one URL per line; blank lines and `#` comments are ignored |
| `ytdl info URL` | Show media metadata and a readable format table |
| `ytdl formats URL` | List every selectable yt-dlp format ID |
| `ytdl gui` | Launch the local graphical studio in your browser |
| `ytdl doctor` | Check Python, yt-dlp, FFmpeg, ffprobe, aria2c, disk, and network |
| `ytdl doctor --install-aria2c` | Windows-only: fetch a managed aria2c when missing |

`download`, `audio`, `playlist`, and `batch` share the same download options.
`info`, `formats`, and `doctor` support `--json`, `--quiet`, and `--verbose`
where meaningful.

## Graphical studio

`ytdl gui` starts a private, warm-dark interface in your browser. It listens only
on `127.0.0.1` and uses a one-time access token that becomes an `HttpOnly`
cookie, so nothing is exposed to your network.

```console
ytdl gui                       # pick a free port and open the browser
ytdl gui --port 8791           # fixed port
ytdl gui --no-browser          # print the access URL only
ytdl-gui                       # same thing as a standalone command
```

The flow is deliberately short:

1. **Paste** a video, track, playlist, album, channel, or mix link.
2. **Inspect** — the studio reads the source and shows its real title, channel,
   duration, thumbnail, and the resolutions that source actually offers.
3. **Pick** MP4 or MP3. MP4 lists only the available heights, lowest to highest,
   with approximate sizes. MP3 offers 128, 192, 256, or 320 kbps. The highest
   quality is selected by default.
4. **Keep** — choose a folder (remembered on this machine) and download.

Details:

- Files are named exactly after the source title (`%(title)s.%(ext)s`), with no
  id suffix and no channel or year folders. Characters Windows forbids are
  replaced by the usual full-width equivalents.
- Studio downloads embed metadata and cover art, but suppress `.info.json`
  sidecars and keep only the final media file.
- A playlist, album, or channel link includes every item by default. A watch
  link with `&list=` stays a single video; open the list in the picker to select
  specific tracks.
- Channel links open as grouped sections. YouTube channels expose Videos,
  Shorts, Live, Playlists, and Releases when those tabs exist. YouTube Music
  channels split releases into **Albums** and **Singles & EPs** using track
  counts. Every section is numbered and sorted on its own, so Albums starts at
  1 instead of continuing the Videos count; selection can still span sections.
- Albums and playlists in a channel or collection have a chevron. Open one to
  see its tracks inline, then tick individual tracks or use **All tracks**.
  Selecting the album downloads the whole collection. Expanded rows start
  collapsed and only load when opened, keeping large channels compact.
- Audio covers that come from square sources are center-cropped to 1:1, so the
  artwork stays square instead of keeping coloured side bars.
- The queue shows live size, speed, ETA, per-track progress, cancellation, and
  open-folder shortcuts.
- Settings hold the download folder, browser cookies, cookie file, proxy,
  parallel downloads, and retries. They persist in `studio.json` beside the
  optional user config (on Windows, `%APPDATA%\ytdl-studio\studio.json`), so the
  destination survives restarts even though each launch uses a new port.
  Browser `localStorage` keeps a fast local copy.
- Browser cookies default to **Automatic**. The studio tries Firefox first,
  then Chrome, Chromium, Edge, Brave, Opera, Vivaldi, Whale, and Safari, and
  uses the first readable store with an eligible session. A browser you choose
  explicitly is tried first, then the studio falls back automatically.
  Locked databases, unsupported encryption (including Edge DPAPI failures),
  and missing profiles are skipped instead of becoming download errors. If no
  browser works, the download proceeds anonymously. A valid `cookies.txt`
  takes priority; a missing or malformed one is ignored so automatic detection
  can take over. The resolved jar is kept in a private temporary file and
  removed when the studio exits.
- The system check runs the same diagnostics as `ytdl doctor`. If aria2c is
  missing on Windows, a **Download** button in the check installs the official
  static build into the app data folder; no administrator access is required.

The studio is a focused subset of the CLI — MP4 and MP3 only for now. Everything
else remains available from `ytdl download`, `ytdl audio`, `ytdl playlist`, and
the option flags. The interface bundles its fonts (Bricolage Grotesque and
Instrument Sans) locally; the only third-party requests are the source
thumbnails shown by the inspector.

## Format presets

Pass a preset with `-f/--format`. Use `-q/--quality` to cap a video preset's
height without changing its container/codec policy.

| Preset | Output |
| --- | --- |
| `best` | Best video + best audio, merged to MP4 |
| `8k`, `4k`, `1440p`, `1080p`, `720p`, `480p`, `360p` | Height-capped video |
| `mp4` | MP4, preferring H.264 + AAC |
| `mp4-h264` | MP4 with H.264 video and AAC audio |
| `mp4-h265` | MP4 with H.265/HEVC video |
| `webm` | WebM, preferring VP9/AV1 + Opus |
| `webm-vp9`, `webm-av1` | Codec-specific WebM |
| `mkv` | Matroska with the original streams |
| `mov` | QuickTime MOV, preferring H.264 + AAC |
| `gif` | Animated GIF (15 fps, palette optimised) |
| `mp3` | MP3 via LAME |
| `m4a`, `aac` | AAC audio in an M4A container |
| `opus` | Opus |
| `ogg`, `vorbis` | Ogg Vorbis |
| `flac` | FLAC lossless |
| `wav` | Uncompressed PCM WAV |
| `alac` | Apple Lossless in M4A |

Aliases include `h264`, `h265`, `hevc`, `vp9`, `av1`, `matroska`, `quicktime`,
`audio`, numeric heights (`1080`), and combined names such as `mp4-1080p` or
`1080p-mp4`.

Examples:

```console
ytdl download URL -f mp4-h265
ytdl download URL -f webm --quality 1440p
ytdl audio URL -f flac
ytdl audio URL -f m4a -b 256
ytdl download URL -f gif -q 480p
```

### About `-q 1080p`

When no explicit format is given, `-q 1080p` selects the compatibility-first
MP4 policy (H.264 video, AAC audio) while capping the height at 1080p. This
avoids AV1/VP9 files that some players cannot open. Combine `-q` with `-f` to
choose a different container or codec:

```console
ytdl download URL -q 1080p                 # 1080p MP4, H.264/AAC preferred
ytdl download URL -q 1080p -f mkv          # 1080p in MKV, original codecs
ytdl download URL -q 1080p -f webm-vp9     # 1080p WebM with VP9
```

If the requested height is unavailable, the next best height below the cap is
used and a warning reports the actual resolution.

## Output layout and templates

Default video template:

```text
{channel_or_uploader}/{upload_year}/{title} [{id}].{ext}
```

Default music/audio template:

```text
{artist}/{album}/{track_number:02d} - {title}.{ext}
```

The real yt-dlp syntax used internally is:

```text
%(channel,uploader|Unknown)s/%(upload_date>%Y,release_date>%Y|Unknown)s/%(title)s [%(id)s].%(ext)s
%(artist,creator,uploader|Unknown Artist)s/%(album|Unknown Album)s/%(track_number,playlist_index|0)02d - %(title)s.%(ext)s
```

Studio downloads are intentionally flatter: the graphical interface always uses
`%(title)s.%(ext)s` and never writes a `.info.json` sidecar.

Override with `-o/--output` and change the base directory with `-d/--dir`:

```console
ytdl download URL -d "D:/Media/YouTube"
ytdl download URL -o "%(uploader)s/%(title)s [%(id)s].%(ext)s"
```

Relative templates are resolved under `--dir`. Absolute templates are used as
given. yt-dlp's `windowsfilenames` and length trimming keep names valid on
every supported platform.

## Playlists, ranges, shuffle, and reverse

A link that points at one video downloads one video — even when it carries a
`&list=` parameter from an album, a radio mix, or someone else's playlist. This
`auto` policy applies to `ytdl URL`, `ytdl download`, and `ytdl audio`:

| Link | `auto` (default) |
| --- | --- |
| `watch?v=…` | that video |
| `watch?v=…&list=…` | that video (not the list) |
| `playlist?list=…`, `/album/…`, `/@channel`, `/channel/…` | every item |

Use the `playlist` command when you really want the whole collection:

```console
ytdl playlist URL --playlist-start 10 --playlist-end 20
ytdl playlist URL --playlist-start 1 --playlist-end 50 --shuffle
ytdl playlist URL --reverse
```

`--playlist-start` and `--playlist-end` still cap any link that expands. In the
studio, a collection link downloads every item unless you open the picker and
tick specific tracks; those tracks download at their original playlist
positions. Collection rows can also be expanded in place to pick individual
album or playlist tracks without leaving the channel picker. A watch link with
`&list=` stays a single video until you pick items.

For channels and large playlists, only a small window of work is queued at a
time, so memory use stays flat even with tens of thousands of entries.

## Batch mode

Create `urls.txt`:

```text
# One URL per line; comments and blank lines are ignored
https://www.youtube.com/watch?v=VIDEO_ID_1
https://www.youtube.com/watch?v=VIDEO_ID_2
https://www.youtube.com/playlist?list=PLAYLIST_ID
```

Run it:

```console
ytdl batch urls.txt -q 720p --concurrent 4 --archive .cache/archive.txt
```

## Metadata, subtitles, chapters, and covers

Defaults are on for thumbnails, metadata, and chapters. Subtitle download and
embedding is opt-in because it changes the experience:

```console
ytdl download URL --subs                 # English
ytdl download URL --subs en,de           # Multiple languages
ytdl download URL --subs=en --no-thumbnail
```

Use the negative flags to disable features:

```console
ytdl download URL --no-metadata --no-thumbnail --no-chapters --no-sponsorblock
```

SponsorBlock defaults to `sponsor,selfpromo`. Choose categories or disable it:

```console
ytdl download URL --sponsorblock all
ytdl download URL --sponsorblock intro,outro
ytdl download URL --no-sponsorblock
```

Audio tagging is performed with Mutagen after FFmpeg extraction, which works
for MP3, M4A/AAC/ALAC, FLAC, Ogg Vorbis, Opus, and WAV. Core tags include title,
artist, album artist, album, date/year, track and disc numbers, genre, composer,
and comment. Source details are written as best-effort extended tags too:
description, copyright, license, publisher, language, channel and uploader IDs
and URLs, creator, release year, playlist, source URL, counts, availability,
live status, and source tags. MP3/WAV use ID3 frames and `TXXX`, MP4 uses freeform
atoms, and FLAC/Ogg use Vorbis comments.

Cover art is embedded when available. If the source video is square, the
thumbnail is centre-cropped to 1:1 so side padding from YouTube's 16:9
thumbnail is removed rather than turned into coloured bars.

## Resume, archive, retries, and concurrency

- Partial downloads resume automatically (`.part` files are kept).
- `--archive FILE` writes only after successful post-processing; failed
  conversions are retried on the next run instead of being marked complete.
- Network failures are retried with exponential backoff. `--retries N`
  controls yt-dlp's network retry budget.
- If YouTube rejects the selected media URL with a 403, the studio retries the
  same item over HLS, then a fallback YouTube client. Audio stays audio-only;
  video keeps the requested height cap, so a 1080p request is not silently
  replaced with 360p unless no matching stream exists.
- `--concurrent N` downloads multiple items in parallel with no artificial cap.
- `--rate-limit 10M` throttles bandwidth; omit it for unlimited.

```console
ytdl playlist URL --archive .cache/music-archive.txt --concurrent 4
```

## Configuration file

Copy `config.example.toml` to one of these locations (highest priority first):

1. `./ytdl-studio.toml`
2. Per-user config:
   - Windows: `%APPDATA%\ytdl-studio\config.toml`
   - macOS: `~/Library/Application Support/ytdl-studio/config.toml`
   - Linux: `$XDG_CONFIG_HOME/ytdl-studio/config.toml` or
     `~/.config/ytdl-studio/config.toml`

Precedence is:

```text
built-in defaults < config file < command-line flags
```

Example:

```toml
format = "1080p"
bitrate = 256
directory = "D:/Media"
concurrent = 4
subtitle_langs = "en,de"
sponsorblock = "sponsor,selfpromo"
archive = ".cache/archive.txt"
```

The loader accepts both a flat file and common sections such as `[download]`,
`[paths]`, `[metadata]`, `[playlist]`, and `[network]`. Unknown keys are
ignored; invalid values produce a clear error instead of a traceback.

## Authentication and network options

```console
ytdl download URL --cookies-from-browser chrome
ytdl download URL --cookies cookies.txt
ytdl download URL --proxy http://127.0.0.1:8080
```

Use browser cookies for private, age-restricted, or members-only media that
your account can access. The studio does this automatically: leave Browser
cookies on **Automatic (recommended)** and sign in to YouTube in Firefox (or
another browser). No cookie flags are needed there. If you pick a specific
browser, it is tried first; if its store cannot be read, the studio falls back
to another readable browser rather than failing. A valid exported
`cookies.txt` overrides browser detection, and a missing or malformed file is
ignored.

On the CLI, choose a source explicitly:

```console
ytdl download URL --cookies-from-browser firefox
ytdl download URL --cookies cookies.txt
```

Respect creators' rights and the platform's terms.

## Scripting and automation

`--json` emits a single JSON document on stdout, with no Rich decoration:

```console
ytdl download URL --json
ytdl info URL --json
ytdl formats URL --json
ytdl doctor --json
```

Download summary shape:

```json
{
  "ok": true,
  "elapsed_seconds": 12.4,
  "total_size": 32456789,
  "completed": 1,
  "skipped": 0,
  "failed": 0,
  "items": [
    {
      "title": "Example",
      "format": "1080p",
      "size": 32456789,
      "duration": 213.0,
      "path": "downloads/Channel/2024/Example [id].mp4",
      "status": "completed",
      "error": null,
      "url": "https://www.youtube.com/watch?v=id"
    }
  ]
}
```

`--dry-run` resolves playlists and prints the plan without writing media.
`--quiet` suppresses banners/progress; `--verbose` sends extractor and
conversion details to stderr.

## Troubleshooting

Run `ytdl doctor` first. It checks every dependency and prints platform-specific
install commands for anything missing.

**"FFmpeg and ffprobe are required"** — install FFmpeg and confirm both
binaries are on `PATH`. ytdl-studio also searches WinGet, Homebrew, and common
Windows install folders. You can set `FFMPEG_BINARY` and `FFPROBE_BINARY` to
explicit paths.

**"No matching format is available"** — the requested codec/resolution does not
exist for that video. Run `ytdl formats URL` and choose a lower height or a
different container.

**"unable to download video data: HTTP Error 403"** — YouTube rejected the
selected media URL. The studio automatically retries the same item over HLS
and then a fallback YouTube client, keeping the requested height cap. If every
attempt still fails, refresh the browser sign-in and retry.

**"This video is private/age-restricted"** — use
Automatic browser cookies in the studio, or
`--cookies-from-browser firefox` on the CLI, with an account that can view the
video.

**Downloads are slow or throttled** — omit `--rate-limit`, reduce
`--concurrent`, or install aria2c. If YouTube returns HTTP 429, wait a few
minutes and retry; the archive prevents duplicate work.

**A conversion failed** — the original download is usually kept so the item is
not added to the archive. Re-run the same command; use `--keep-video` to retain
sources deliberately.

**Unreadable characters in filenames** — yt-dlp sanitizes Windows-invalid
characters. Unicode is preserved on purpose; use `restrictfilenames` via a
custom `output` template only if you need ASCII-only names.

## Development

```console
python -m venv .venv
# activate the venv
python -m pip install -e ".[dev]"

pytest
ruff check ytdl tests
python -m compileall -q ytdl
```

Tests cover preset mappings and selectors, path sanitization and collision
handling, rate-limit parsing, output-template resolution, FFmpeg argument
construction, conversion decisions, the playlist policy (a watch link with a
`list=` stays one video, explicit item selections use original positions), the
studio's source-derived quality ladder, its title-only naming and sidecar
suppression, and HTTP authentication. Tests do not require a network connection.

The studio ships inside the same package with no extra dependencies: its backend
uses the standard library HTTP server and its front end is plain HTML, CSS, and
JavaScript.

## License

MIT. This project is a front end for yt-dlp and FFmpeg; review their licenses
and the terms of service of any site you download from. Only download content
you have the right to store.
