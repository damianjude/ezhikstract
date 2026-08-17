# ezhikstract

`ezhikstract` is a command-line interface (CLI) tool. It extracts playable video and pictures from the proprietary round-robin storage format of EZVIZ and Hikvision SD cards.

The tool operates on SD cards from security cameras and video doorbells that contain `hiv<xxxxx>.mp4` files and an `index00.bin` index file. The tool parses the index file, validates video segments, extracts the raw MPEG-PS / HEVC streams, and remuxes them into standard `.mp4` containers. It also merges segments from the same day into one daily video file formatted with UTC timestamps.

The tool does not re-encode video. It copies HEVC video streams directly. It transcodes audio streams (AAC / PCM G.711) to Opus to make them compatible with standard media players.

## Installation

Make sure that you install Python 3.10 or higher. Install the CLI with pipx:

```bash
pipx install ezhikstract
```

## Usage

The CLI provides two primary command groups: `list` and `extract`. Both groups operate on `videos` or `pictures`.

### 1. The `list` Command Group

Use the `list` command group to inspect valid records on the SD card without file extraction. The tool formats timestamps in UTC (`YYYY-MM-DD HH:MM:SS`).

#### Videos

List all valid active video segments on the SD card:

```bash
ezhikstract list videos INPUT_DIR
```

* `INPUT_DIR` (Mandatory): Root directory of the SD card that contains the `index00.bin` file.

#### Pictures

List all valid picture and thumbnail records on the SD card:

```bash
ezhikstract list pictures INPUT_DIR
```

* `INPUT_DIR` (Mandatory): Root directory of the SD card that contains the `index00p.bin` file.

---

### 2. The `extract` Command Group

Use the `extract` command group to extract and process records from the SD card.

#### Videos

Extract raw video segments from active container files. The command filters segments by timestamp if specified, remuxes them into standard `.mp4` containers, and merges segments from the same calendar day into one daily file. The file name uses the UTC start time format (`DDMMYYYY HHMMSS.mp4`).

```bash
ezhikstract extract videos INPUT_DIR [OPTIONS]
```

* `INPUT_DIR` (Mandatory): Root directory of the SD card that contains the `index00.bin` file.

**Options:**

* `-o, --output PATH`: Output directory for daily merged `.mp4` files. Default: `./recordings`.
* `--from DATETIME`: Start time filter (inclusive) in UTC. Format: `"YYYY-MM-DD HH:MM:SS"`.
* `--to DATETIME`: End time filter (exclusive) in UTC. Format: `"YYYY-MM-DD HH:MM:SS"`.
* `--replace / --no-replace`: Overwrite existing files in the output directory. Default: `--replace`.

#### Pictures

Extract raw snapshot thumbnails from picture container files. The command filters items by timestamp if specified, and writes standard JPEG (`.jpg`) files named with the UTC timestamp.

```bash
ezhikstract extract pictures INPUT_DIR [OPTIONS]
```

* `INPUT_DIR` (Mandatory): Root directory of the SD card that contains the `index00p.bin` file.

**Options:**

* `-o, --output PATH`: Output directory for extracted `.jpg` files. Default: `./pictures`.
* `--from DATETIME`: Start time filter (inclusive) in UTC. Format: `"YYYY-MM-DD HH:MM:SS"`.
* `--to DATETIME`: End time filter (exclusive) in UTC. Format: `"YYYY-MM-DD HH:MM:SS"`.
* `--replace / --no-replace`: Overwrite existing files in the output directory. Default: `--replace`.

## How It Works

The camera SD cards use a pre-allocated round-robin storage format:

1. The `index00.bin` file (and the backup `index01.bin` file) contains pointers, timestamps, offsets, header records, and checksums for recorded video segments.
2. The system writes video data to pre-allocated `hivxxxxx.mp4` files. Each file has a fixed size of 268.4 MB (equal to the index files).
3. `ezhikstract` parses the 32-byte per-file header records in `index00.bin`. It rejects unwritten round-robin container files (`segment_count == 65535`).
4. It aligns segment start offsets past proprietary headers to the MPEG-PS / HEVC start codes (`0x000001`).
5. It uses a three-tier fallback extraction procedure:
   * **MPEG-PS + AAC/Opus**: Decodes MPEG-PS streams and transcodes AAC/PCM audio to Opus.
   * **MPEG-PS Video-Only**: Uses video-only extraction (`-an`) if audio headers are missing or invalid.
   * **Raw HEVC Stream**: Uses `-f hevc` if the segment is a raw Annex-B HEVC stream without container headers.
6. The tool groups segments by UTC calendar day and concatenates them with the FFmpeg concat demuxer (`-map 0:v -map 0:a?`) to keep audio when available.

## Architecture and Design Decisions

The tool implements specific design choices for modularity, performance, and reliability:

### 1. Stream-Piped Concurrency and Non-Blocking I/O

* **Piped I/O**: The tool reads segments into memory and passes them to the standard input of `ffmpeg` subprocesses using `proc.communicate(input=segment_data, timeout=30)`. This prevents operating system pipe deadlocks.
* **Bounded Multithreading**: The tool uses a `ThreadPoolExecutor` to process segments in parallel. Concurrency limits prevent disk performance degradation (maximum 4 worker threads for video, 8 for pictures).

### 2. Lossless Remuxing and Transcoding

* **Video Quality**: The tool copies HEVC video streams directly (`-c:v copy`) with the `-tag:v hvc1` format. This ensures lossless rendering on Apple platforms and standard media players.
* **Audio Compatibility**: The tool transcodes AAC and PCM G.711 audio tracks to Opus (`-c:a libopus`) during extraction. This ensures playback compatibility without modification of the video stream.
* **Concat Demuxer**: The tool combines daily segments into a single file with the FFmpeg concat demuxer (`-f concat -map 0:v -map 0:a?`). This operation copies streams directly without video re-encoding.

### 3. Data Integrity and Validation

* **Round-Robin Filtering**: The tool reads 32-byte header records in `index00.bin` to ignore unwritten files (`segment_count == 65535`). This prevents extraction of old clips from previous recording cycles.
* **Dummy Timestamp Filtering**: The tool rejects zeroed or invalid segment records with timestamps before the year 2020 (`start_time_raw < 1577836800`).
* **MPEG-PS Validation**: The tool checks the first 2 KB of each sector for MPEG Program Stream start codes (`0x000001`). It ignores sectors damaged by sudden power loss or buffer overwrite.
* **JPEG Verification**: The tool verifies the Start of Image (SOI) magic bytes (`0xFF 0xD8 0xFF`) before it processes picture files.
* **UTC Time Zone Consistency**: The tool parses and formats all timestamps in UTC (`timezone.utc`).

## Storage Format

The camera SD cards use a pre-allocated round-robin storage format. All index and container files (`index00.bin`, `index01.bin`, `hivxxxxx.mp4`, `index00p.bin`, and `hivxxxxx.pic`) have a fixed pre-allocated size of 268.4 MB (281,444,352 bytes).

---

### Files

1. **`index00.bin`**: Primary index file that contains pointers, timestamps, offsets, and checksums for video segments.
2. **`index01.bin`**: Backup copy of `index00.bin`.
3. **`hivxxxxx.mp4`**: Pre-allocated video containers (numbered from `hiv00000.mp4` upward) that contain raw MPEG-PS streams.
4. **`index00p.bin`**: Metadata and index for snapshot pictures and thumbnails.
5. **`hivxxxxx.pic`**: Pre-allocated picture containers (numbered from `hiv00000.pic` upward) that contain raw JPEG images.

---

### Index File Structure (`index00.bin` / `index00p.bin`)

An index file contains three sections:

1. A **1280-byte header** (`HEADER_BUFFER_LENGTH`).
2. An array of **AV-File records** (32 bytes per record).
3. An array of **Segment records** (80 bytes for video, 96 bytes for pictures).

#### 1. File Header (1280 Bytes)

The first 1280 bytes of the index file contain configuration values, file counts, and status flags. `index00.bin` and `index00p.bin` use the same header structure.

| Offset | Size (Bytes) | Field Name | Data Type | Description |
| :--- | :--- | :--- | :--- | :--- |
| `0` | `8` | `modify_counter` | `uint64_t` (LE) | Modification count of the video segments. |
| `8` | `4` | `index_version` | `uint32_t` (LE) | Version number of the index file (typically `2` or `3`). |
| `12` | `4` | `av_files` | `uint32_t` (LE) | Total count of pre-allocated container files. |
| `16` | `4` | `next_file_no` | `uint32_t` (LE) | Number (`xxxxx`) of the next file to write. |
| `20` | `4` | `last_file_no` | `uint32_t` (LE) | Number of the most recently written file. |
| `24` | `1176` | `cur_file_info` | `bytes` | Current file information, write progress, and padding. |
| `1200` | `76` | `unknown` | `bytes` | Reserved padding bytes. |
| `1276` | `4` | `checksum` | `uint32_t` (LE) | Header checksum value. |

#### 2. AV-File Records Section

The AV-File records section starts at offset `1280`:

* **Record Size**: 32 bytes (`FILE_RECORD_LENGTH`).
* **Total Records**: Value of `av_files` from the header.
* **Byte Range**: Offset `1280` to `1280 + (av_files * 32)`.
* **Note**: `index00p.bin` contains placeholder values (`0xffff...`) in these records.

#### 3. Segment Records Section

The segment records section starts immediately after the AV-File records section.

##### Video Segments (`index00.bin`)

Each video segment record is **80 bytes** (`SEGMENT_RECORD_LENGTH`).
Each pre-allocated video file contains space for a maximum of **256** segment records (`MAX_SEGMENTS_PER_SOURCE_FILE`).

| Offset | Size (Bytes) | Field Name | Data Type | Description |
| :--- | :--- | :--- | :--- | :--- |
| `0` | `8` | *Unused* | `bytes` | Contains `segmentType`, `status`, `reservedA`, and `resolution`. |
| `8` | `8` | `start_time_raw` | `uint64_t` (LE) | Segment start timestamp. Lower 32 bits contain the Unix epoch. |
| `16` | `8` | `end_time_raw` | `uint64_t` (LE) | Segment end timestamp. Lower 32 bits contain the Unix epoch (`0` indicates an empty slot). |
| `24` | `16` | *Unused* | `bytes` | Contains keyframe timestamps (`firstKeyFrameAbsTime`, etc.). |
| `40` | `4` | `start_offset` | `uint32_t` (LE) | Start byte offset of the segment in the `hivxxxxx.mp4` file. |
| `44` | `4` | `end_offset` | `uint32_t` (LE) | End byte offset of the segment in the `hivxxxxx.mp4` file. |
| `48` | `32` | *Unused* | `bytes` | Reserved metadata fields. |

##### Picture Segments (`index00p.bin`)

Each picture segment record is **96 bytes**. Unused slots contain zero bytes (`0x0000...`).

| Offset | Size (Bytes) | Field Name | Data Type | Description |
| :--- | :--- | :--- | :--- | :--- |
| `0` | `8` | `flags` | `bytes` | Segment status and type flags (e.g. `0x0d00010000000000`). |
| `8` | `8` | `start_time_raw` | `uint64_t` (LE) | Picture timestamp. Lower 32 bits contain the Unix epoch. |
| `16` | `8` | `end_time_raw` | `uint64_t` (LE) | End timestamp (typically equals `start_time_raw`). |
| `24` | `16` | *Unused* | `bytes` | Reserved timestamp data. |
| `40` | `4` | `start_offset` | `uint32_t` (LE) | Start byte offset in the `hivxxxxx.pic` file. |
| `44` | `4` | `end_offset` | `uint32_t` (LE) | End byte offset in the `hivxxxxx.pic` file. |
| `48` | `32` | `info` | `bytes` | Contains the `INFO` identifier and offset data. |
| `80` | `16` | `watermark` | `bytes` | ASCII camera ID and watermark string (e.g. `BK1721071`). |

---

### Media Container Formats

#### Video File Format (`hivxxxxx.mp4`)

These files contain raw **MPEG Program Streams (MPEG-PS)**:

1. **Streams**:
   * **Video Codec**: HEVC (H.265).
   * **Audio Codec**: PCM (G.711 A-law / `pcm_alaw`).
2. **Segment Storage**:
   * The device writes segments sequentially into container files using offsets from `index00.bin`.
3. **MPEG-PS Validation**:
   * Valid segments begin with the standard Pack Start Code `0x000001BA` (with marker byte `0x40` at offset 4).
   * A System Header sequence `0x000001BB` is present within the first 2 KB of each valid segment.
   * Sudden power disconnection can cause invalid or incomplete sector data. The tool verifies these marker bytes to reject damaged records.

#### Picture File Format (`hivxxxxx.pic`)

These files store **raw JPEG images** (snapshots and thumbnails):

1. **Format**:
   * Standard JPEG images containing Start of Image (SOI) marker bytes `0xFF 0xD8 0xFF`.
2. **Segment Storage**:
   * The device writes successive snapshot images sequentially into the 268.4 MB file.
   * Byte offsets in `index00p.bin` locate each image in the `.pic` file.
