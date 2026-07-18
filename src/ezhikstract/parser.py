from dataclasses import dataclass

# Index file layout
HEADER_BUFFER_LENGTH: int = 1280
FILE_RECORD_LENGTH: int = 32  # bytes per av-file record in the header area
SEGMENT_RECORD_LENGTH: int = 80  # bytes per video segment record
SEGMENT_RECORD_LENGTH_PIC: int = 96  # bytes per picture segment record
MAX_SEGMENTS_PER_SOURCE_FILE: int = 256


@dataclass
class IndexHeader:
    modify_counter: int  # number of times video segments have been modified
    index_version: int  # 2 or 3
    av_files: int  # total number of hivXXXXX.mp4 files on the SD card
    next_file_no: int  # xxxxx of the next hivXXXXX.mp4 to be written
    last_file_no: int
    cur_file_info: bytes  # see README.md
    unknown: bytes
    checksum: int


@dataclass
class Segment:
    start_time_raw: int
    end_time_raw: int
    start_offset: int
    end_offset: int  # byte offset of segment end inside hivXXXXX.mp4


def parse_index_header(data: bytes) -> IndexHeader:
    """Parse the first HEADER_BUFFER_LENGTH bytes of an index file."""
    off = 0
    modify_counter = int.from_bytes(data[off : off + 8], "little")
    off += 8
    index_version = int.from_bytes(data[off : off + 4], "little")
    off += 4
    av_files = int.from_bytes(data[off : off + 4], "little")
    off += 4
    next_file_no = int.from_bytes(data[off : off + 4], "little")
    off += 4
    last_file_no = int.from_bytes(data[off : off + 4], "little")
    off += 4
    cur_file_info = data[off : off + 1176]
    off += 1176
    unknown = data[off : off + 76]
    off += 76
    checksum = int.from_bytes(data[off : off + 4], "little")
    off += 4

    return IndexHeader(
        modify_counter=modify_counter,
        index_version=index_version,
        av_files=av_files,
        next_file_no=next_file_no,
        last_file_no=last_file_no,
        cur_file_info=cur_file_info,
        unknown=unknown,
        checksum=checksum,
    )


def parse_segment(data: bytes) -> Segment:
    """Parse one 80-byte segment record from the index file."""
    off = 0

    # segmentType + status + reservedA + resolution (8 bytes — unused)
    off += 8

    start_time_raw = int.from_bytes(data[off : off + 8], "little")
    off += 8
    end_time_raw = int.from_bytes(data[off : off + 8], "little")
    off += 8

    # firstKeyFrameAbsTime + firstKeyFrameStdTime + lastFrameStdTime (16 bytes — unused)
    off += 16

    start_offset = int.from_bytes(data[off : off + 4], "little")
    off += 4
    end_offset = int.from_bytes(data[off : off + 4], "little")
    off += 4

    # reservedB + infoCount + infoTypes + infoStartTime + infoEndTime +
    # infoStartOffset + infoEndOffset (28 bytes — unused)
    off += 28

    return Segment(
        start_time_raw=start_time_raw,
        end_time_raw=end_time_raw,
        start_offset=start_offset,
        end_offset=end_offset,
    )


def parse_picture_segment(data: bytes) -> Segment:
    """Parse one 96-byte segment record from the picture index file."""
    off = 0

    # type + status + resA + resolution (8 bytes — unused)
    off += 8

    start_time_raw = int.from_bytes(data[off : off + 8], "little")
    off += 8
    end_time_raw = int.from_bytes(data[off : off + 8], "little")
    off += 8

    # firstKeyFrameAbsTime + firstKeyFrameStdTime + lastFrameStdTime (16 bytes — unused)
    off += 16

    start_offset = int.from_bytes(data[off : off + 4], "little")
    off += 4
    end_offset = int.from_bytes(data[off : off + 4], "little")
    off += 4

    # resB + infoNum + infoTypes + infoStartTime + infoEndTime + infoStartOffset + infoEndOffset + watermark (44 bytes - unused)
    off += 44

    return Segment(
        start_time_raw=start_time_raw,
        end_time_raw=end_time_raw,
        start_offset=start_offset,
        end_offset=end_offset,
    )


def load_index(index_path: str) -> tuple[IndexHeader, list[Segment]]:
    """
    Read and parse an index00.bin file.

    Returns the file header plus every segment record found in the file
    (invalid / zero-endTime records are included; callers should filter them).
    """
    with open(index_path, "rb") as fh:
        data = fh.read()

    # Ensure index file is at least large enough to contain the header buffer
    if len(data) < HEADER_BUFFER_LENGTH:
        raise ValueError(
            f"Index file is too small ({len(data)} bytes). Expected at least {HEADER_BUFFER_LENGTH} bytes."
        )

    header = parse_index_header(data[:HEADER_BUFFER_LENGTH])

    segments: list[Segment] = []

    # Segment table starts immediately after the header + the per-file records.
    offset = HEADER_BUFFER_LENGTH + header.av_files * FILE_RECORD_LENGTH

    for _ in range(header.av_files):
        # Prevent tight loops if corrupted header specifies too many files
        if offset >= len(data):
            break
        for _ in range(MAX_SEGMENTS_PER_SOURCE_FILE):
            if offset + SEGMENT_RECORD_LENGTH > len(data):
                break
            segment_data = data[offset : offset + SEGMENT_RECORD_LENGTH]
            segments.append(parse_segment(segment_data))
            offset += SEGMENT_RECORD_LENGTH

    return header, segments


def load_picture_index(
    index_path: str,
) -> tuple[IndexHeader, list[tuple[int, Segment]]]:
    """
    Read and parse an index00p.bin file.

    Returns the file header plus every segment record found in the file along with its
    determined source file index. Valid records are tightly packed.
    """
    with open(index_path, "rb") as fh:
        data = fh.read()

    if len(data) < HEADER_BUFFER_LENGTH:
        raise ValueError(
            f"Index file is too small ({len(data)} bytes). Expected at least {HEADER_BUFFER_LENGTH} bytes."
        )

    header = parse_index_header(data[:HEADER_BUFFER_LENGTH])

    segments: list[tuple[int, Segment]] = []

    # Segment table starts immediately after the header + the per-file records.
    offset = HEADER_BUFFER_LENGTH + header.av_files * FILE_RECORD_LENGTH

    current_file_idx = 0
    prev_end_offset = -1

    while offset + SEGMENT_RECORD_LENGTH_PIC <= len(data):
        segment_data = data[offset : offset + SEGMENT_RECORD_LENGTH_PIC]

        # Check for empty padding block which indicates end of valid segments
        if (
            segment_data[:4] == b"\x00\x00\x00\x00"
            or segment_data[:4] == b"\xff\xff\xff\xff"
        ):
            break

        segment = parse_picture_segment(segment_data)

        if segment.end_offset != 0:
            if prev_end_offset != -1 and segment.start_offset < prev_end_offset:
                current_file_idx += 1

            segments.append((current_file_idx, segment))
            prev_end_offset = segment.end_offset

        offset += SEGMENT_RECORD_LENGTH_PIC

    return header, segments
