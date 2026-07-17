# Format

The security camera SD cards (typically EZVIZ / Hikvision) use a pre-allocated, round-robin storage format. All files (`index00.bin`, `index01.bin`, and the video files `hivxxxxx.mp4`) are pre-allocated to the exact same size of 268.4 MB (281,444,352 bytes).

---

## Files

1. **`index00.bin`**: The primary index file containing pointers, timestamps, offsets, and checksums for the recorded video segments.
2. **`index01.bin`**: A redundant copy of `index00.bin` used for backup and reliability.
3. **`hivxxxxx.mp4`**: Pre-allocated video containers (numbered from `hiv00000.mp4` upwards) containing segment-based raw MPEG-PS streams.
4. **`index00p.bin` & `hiv00000.pic`**: Metadata and pictures used for mobile app push notifications (human/motion detection).

---

## Index File Layout (`index00.bin`)

An index file consists of:
1. A **1280-byte header** (`HEADER_BUFFER_LENGTH`).
2. An array of **AV-File records** (each 32 bytes).
3. An array of **Segment records** (each 80 bytes).

### 1. File Header (1280 Bytes)

The first 1280 bytes of the index file contains general settings, the number of files, and state flags.

| Offset | Size (Bytes) | Field Name | Data Type | Description |
| :--- | :--- | :--- | :--- | :--- |
| `0` | `8` | `modify_counter` | `uint64_t` (LE) | Number of times the video segments have been modified. |
| `8` | `4` | `index_version` | `uint32_t` (LE) | Version of the index file (typically `2` or `3`). |
| `12` | `4` | `av_files` | `uint32_t` (LE) | Total number of `hivxxxxx.mp4` files allocated on the SD card. |
| `16` | `4` | `next_file_no` | `uint32_t` (LE) | File number (`xxxxx`) of the next `hivxxxxx.mp4` to be written. |
| `20` | `4` | `last_file_no` | `uint32_t` (LE) | File number of the last recording stored. |
| `24` | `1176` | `cur_file_info` | `bytes` | Info about the current file (includes timestamps, write progress, and padding). |
| `1200` | `76` | `unknown` | `bytes` | Reserved padding. |
| `1276` | `4` | `checksum` | `uint32_t` (LE) | Custom checksum for header validation (not standard CRC32). |

### 2. AV-File Records Section

Immediately following the header at offset `1280`, there is a contiguous array of AV-File records.
- **Record Size**: 32 bytes (`FILE_RECORD_LENGTH`).
- **Total Records**: Equal to `av_files` from the header.
- **Span**: Offset `1280` to `1280 + (av_files * 32)`.

### 3. Segment Records Section

The segment records list begins directly after the AV-File records section. Each segment record is exactly **80 bytes** (`SEGMENT_RECORD_LENGTH`).

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

---

## Video File Structure (`hivxxxxx.mp4`)

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
