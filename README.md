# ezhikstract

CLI tool to extract playable video from EZVIZ and Hikvision SD cards' proprietary round-robin storage format.

It works specifically with security camera or video doorbell SD cards that contain files named `hiv<xxxxx>.mp4` and an `index00.bin` metadata/index file. The tool parses the index file, validates video segments, extracts the raw MPEG-PS streams, and remuxes them into standard `.mp4` containers, merging segments from the same day into a single daily video file.

No full video re-encoding is performed; video streams (HEVC) are copied directly. Audio streams (e.g., PCM G.711 / Alaw) are re-encoded to Opus to ensure compatibility with standard media players.

## Installation

Install the package directly from the repository directory:

```bash
pip install ezhikstract
```

## Usage

The CLI provides two main commands: `list` and `extract`.

### Command: `list`

Lists all valid recordings found on the SD card without extracting them.

#### Usage

```bash
ezhikstract list INPUT_DIR
```

#### Arguments

* `INPUT_DIR` (Required): Root directory of the SD card containing `index00.bin`.

### Command: `extract`

Extracts recording segments from the SD card, filters them by date/time if specified, and merges segments from the same day into a single file named by start time (`DDMMYYYY HHMMSS.mp4`).

#### Usage

```bash
ezhikstract extract INPUT_DIR [OPTIONS]
```

#### Arguments

* `INPUT_DIR` (Required): Root directory of the SD card containing `index00.bin`.

#### Options

* `-o, --output PATH`: Output directory for the extracted and merged `.mp4` files. Default is `recordings`.
* `--from DATETIME`: Inclusive start filter, UTC (format: `YYYY-MM-DD HH:MM:SS`).
* `--to DATETIME`: Exclusive end filter, UTC (format: `YYYY-MM-DD HH:MM:SS`).
* `--replace / --no-replace`: Whether to overwrite existing files in the output directory. Default is `--replace`.

## How it Works

The SD cards of these cameras use a pre-allocated round-robin storage file format:
1. `index00.bin` (and the backup copy `index01.bin`) contains pointers, timestamps, offsets, and checksums for the recorded video segments.
2. The video data is written to pre-allocated `hivxxxxx.mp4` files, which are all exactly 268.4 MB (as are the index files).
3. Within the `hivxxxxx.mp4` files, the videos are stored as raw MPEG-PS streams (MPEG Program Stream with HEVC video and G.711/PCM audio).
4. `ezhikstract` parses `index00.bin`, verifies the boundaries and starts of the segments inside `hivxxxxx.mp4` (checking for valid MPEG-PS headers), extracts the segments, groups them by day, stream-copies the HEVC video tracks, re-encodes the audio to Opus, and concats the daily segments using the FFmpeg concat demuxer.
