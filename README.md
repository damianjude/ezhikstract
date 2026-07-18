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

## Architecture and Design Decisions

The repository is built around several design choices to maintain clean separation, high performance, and robustness:

### 1. Stream-Piped Concurrency
* **Piped I/O**: Rather than reading massive 268MB files into memory or writing huge intermediate raw stream dumps to disk, segments are read in small chunks and piped directly to the standard input of the `ffmpeg` subprocess.
* **Bounded Multithreading**: Uses a `ThreadPoolExecutor` to process segments in parallel. Concurrency limits are tuned to avoid high disk latency (limited to 4 workers for videos and 8 for pictures).

### 2. Lossless Remuxing & Transcoding
* **Video Quality**: Video streams (HEVC) are copied directly (`-c:v copy`) using the `-tag:v hvc1` format to ensure native, lossless rendering on iOS and macOS players.
* **Audio Compatibility**: PCM G.711 / Alaw audio tracks are transcoded on the fly to Opus (`-c:a libopus`), resolving compatibility issues without modifying the underlying video track.
* **Concat Demuxer**: Uses the FFmpeg concat demuxer (`-f concat`) to combine chronological daily segments into a single file. This is a stream-copy operation, meaning no full decode-encode passes are executed.

### 3. Integrity and Validation
* **MPEG-PS Validation**: Checks the first 2KB of each raw sector boundary for MPEG Program Stream Pack Start (`0x000001BA`) and System Header (`0x000001BB`) markers. Any sectors corrupt from sudden power loss or circular buffer overwrites are ignored.
* **JPEG Verification**: Validates the Start of Image (SOI) magic bytes (`0xFF 0xD8 0xFF`) for all picture/thumbnail files before parsing.
* **Range Checks**: Automatically discards records with inverted offsets/timestamps, or segments belonging to missing video containers.

## Storage Format

The security camera SD cards (typically EZVIZ / Hikvision) use a pre-allocated, round-robin storage format. All files (`index00.bin`, `index01.bin`, and the video files `hivxxxxx.mp4`) are pre-allocated to the exact same size of 268.4 MB (281,444,352 bytes). The same pre-allocation strategy applies to the mobile app thumbnails (`index00p.bin` and `hivxxxxx.pic`).

---

### Files

1. **`index00.bin`**: The primary index file containing pointers, timestamps, offsets, and checksums for the recorded video segments.
2. **`index01.bin`**: A redundant copy of `index00.bin` used for backup and reliability.
3. **`hivxxxxx.mp4`**: Pre-allocated video containers (numbered from `hiv00000.mp4` upwards) containing segment-based raw MPEG-PS streams.
4. **`index00p.bin`**: Metadata and index for the pictures/thumbnails used for mobile app push notifications (human/motion detection).
5. **`hivxxxxx.pic`**: Pre-allocated picture containers (numbered from `hiv00000.pic` upwards) containing segment-based raw JPEG images.

---

### Index File Layout (`index00.bin` / `index00p.bin`)

An index file consists of:
1. A **1280-byte header** (`HEADER_BUFFER_LENGTH`).
2. An array of **AV-File records** (each 32 bytes).
3. An array of **Segment records** (80 bytes for video, 96 bytes for pictures).

#### 1. File Header (1280 Bytes)

The first 1280 bytes of the index file contains general settings, the number of files, and state flags. Both `index00.bin` and `index00p.bin` share this exact structure.

| Offset | Size (Bytes) | Field Name | Data Type | Description |
| :--- | :--- | :--- | :--- | :--- |
| `0` | `8` | `modify_counter` | `uint64_t` (LE) | Number of times the video segments have been modified. |
| `8` | `4` | `index_version` | `uint32_t` (LE) | Version of the index file (typically `2` or `3`). |
| `12` | `4` | `av_files` | `uint32_t` (LE) | Total number of pre-allocated `.mp4` or `.pic` files. |
| `16` | `4` | `next_file_no` | `uint32_t` (LE) | File number (`xxxxx`) of the next file to be written. |
| `20` | `4` | `last_file_no` | `uint32_t` (LE) | File number of the last recording stored. |
| `24` | `1176` | `cur_file_info` | `bytes` | Info about the current file (includes timestamps, write progress, and padding). |
| `1200` | `76` | `unknown` | `bytes` | Reserved padding. |
| `1276` | `4` | `checksum` | `uint32_t` (LE) | Custom checksum for header validation (not standard CRC32). |

#### 2. AV-File Records Section

Immediately following the header at offset `1280`, there is a contiguous array of AV-File records.
- **Record Size**: 32 bytes (`FILE_RECORD_LENGTH`).
- **Total Records**: Equal to `av_files` from the header.
- **Span**: Offset `1280` to `1280 + (av_files * 32)`.
- **Note**: In `index00p.bin`, these blocks are primarily empty placeholder values (`0xffff...`).

#### 3. Segment Records Section

The segment records list begins directly after the AV-File records section.

##### Video Segments (`index00.bin`)
Each segment record is exactly **80 bytes** (`SEGMENT_RECORD_LENGTH`).
For each pre-allocated video file, the index contains space for up to **256** segment entries (`MAX_SEGMENTS_PER_SOURCE_FILE`).

| Offset | Size (Bytes) | Field Name | Data Type | Description |
| :--- | :--- | :--- | :--- | :--- |
| `0` | `8` | *Unused* | `bytes` | Contains `segmentType`, `status`, `reservedA`, and `resolution`. |
| `8` | `8` | `start_time_raw` | `uint64_t` (LE) | Segment start timestamp. Lower 32 bits represent the Unix epoch. |
| `16` | `8` | `end_time_raw` | `uint64_t` (LE) | Segment end timestamp. Lower 32 bits represent the Unix epoch. A value of `0` denotes an empty slot. |
| `24` | `16` | *Unused* | `bytes` | Contains keyframe timestamps (`firstKeyFrameAbsTime`, etc.). |
| `40` | `4` | `start_offset` | `uint32_t` (LE) | Start byte offset of this segment inside its `hivxxxxx.mp4` file. |
| `44` | `4` | `end_offset` | `uint32_t` (LE) | End byte offset of this segment inside its `hivxxxxx.mp4` file. |
| `48` | `32` | *Unused* | `bytes` | Reserved fields and metadata info segments. |

##### Picture Segments (`index00p.bin`)
Each segment record is exactly **96 bytes**. Unused segment slots are padded with zero bytes (`0x0000...`).

| Offset | Size (Bytes) | Field Name | Data Type | Description |
| :--- | :--- | :--- | :--- | :--- |
| `0` | `8` | `flags` | `bytes` | Type/Status flag for the segment (e.g. `0x0d00010000000000`). |
| `8` | `8` | `start_time_raw` | `uint64_t` (LE) | Segment timestamp. Lower 32 bits represent the Unix epoch. |
| `16` | `8` | `end_time_raw` | `uint64_t` (LE) | Segment end timestamp (usually matches `start_time_raw` for pictures). |
| `24` | `16` | *Unused* | `bytes` | Reserved timestamp block. |
| `40` | `4` | `start_offset` | `uint32_t` (LE) | Start byte offset of this segment inside its `hivxxxxx.pic` file. |
| `44` | `4` | `end_offset` | `uint32_t` (LE) | End byte offset of this segment inside its `hivxxxxx.pic` file. |
| `48` | `32` | `info` | `bytes` | Contains `INFO` string and related info offsets. |
| `80` | `16` | `watermark` | `bytes` | ASCII camera ID/Watermark string appended at the end of the segment (e.g., `BK1721071`). |

---

### Media Container Structures

#### Video File Structure (`hivxxxxx.mp4`)

Despite the `.mp4` file extension, these files are not standard MP4 containers. Instead, they are pre-allocated byte blocks containing raw **MPEG Program Streams (MPEG-PS)**:

1. **Streams**:
   - **Video Codec**: HEVC (H.265).
   - **Audio Codec**: PCM (G.711 A-law / `pcm_alaw`). *Note: Original recordings do not use AAC.*
2. **Segment Storage**:
   - Multiple recordings are written sequentially into these containers, mapped by the offsets defined in the segment tables in `index00.bin`.
3. **MPEG-PS Validation**:
   - Valid segments start with the standard MPEG Program Stream Pack Start Code `0x000001BA` (with MPEG-2 marker prefix byte `0x40` at offset 4).
   - A System Header sequence `0x000001BB` is present within the first 2KB of any valid segment.
   - When a device is abruptly powered down or files are overwritten, segment blocks can become corrupted or partially written, which requires checking these magic bytes to prevent loading corrupt recordings.

#### Picture File Structure (`hivxxxxx.pic`)

These files act identically to the `.mp4` files but are used exclusively for storing **raw JPEG images** (snapshots/thumbnails). 

1. **Format**:
   - Standard JPEG binary images containing SOI (Start of Image) magic bytes `0xFF 0xD8 0xFF`.
2. **Segment Storage**:
   - Successive snapshots are dumped sequentially into the 256.4 MB block.
   - Offsets mapping to exact byte locations in the `.pic` block are found within `index00p.bin`.
