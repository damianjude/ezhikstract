import struct
from dataclasses import dataclass, field
from pathlib import Path

# Index file layout
HEADER_BUFFER_LENGTH: int = 1280
FILE_RECORD_LENGTH: int = 32  # bytes per av-file record in the header area
SEGMENT_RECORD_LENGTH: int = 80  # legacy video segment record size
SEGMENT_RECORD_LENGTH_96: int = 96  # EZVIZ v3 96-byte video segment record size
SEGMENT_RECORD_LENGTH_PIC: int = 96  # bytes per picture segment record
MAX_SEGMENTS_PER_SOURCE_FILE: int = 256


HEADER_STRUCT = struct.Struct("<QIIII1176s76sI")
FILE_RECORD_STRUCT = struct.Struct("<IIII16x")
SEGMENT_STRUCT = struct.Struct("<8xQQ16xII32x")
SEGMENT_STRUCT_96 = struct.Struct("<8xQQ16xII48x")
PICTURE_SEGMENT_STRUCT = struct.Struct("<8xQQ16xII48x")


@dataclass
class FileRecord:
    file_index: int
    segment_count: int
    start_time_raw: int
    end_time_raw: int


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
    file_records: list[FileRecord] = field(default_factory=list)  # pyright: ignore[reportUnknownVariableType]


@dataclass
class Segment:
    start_time_raw: int
    end_time_raw: int
    start_offset: int
    end_offset: int  # byte offset of segment end inside hivXXXXX.mp4


def parse_index_header(data: bytes) -> IndexHeader:
    """Parse the first HEADER_BUFFER_LENGTH bytes of an index file."""
    (
        modify_counter,
        index_version,
        av_files,
        next_file_no,
        last_file_no,
        cur_file_info,
        unknown,
        checksum,
    ) = HEADER_STRUCT.unpack(data)

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


def parse_file_record(data: bytes) -> FileRecord:
    """Parse one 32-byte per-file record in the index header area."""
    file_index, segment_count, start_time_raw, end_time_raw = FILE_RECORD_STRUCT.unpack(
        data
    )
    return FileRecord(
        file_index=file_index,
        segment_count=segment_count,
        start_time_raw=start_time_raw,
        end_time_raw=end_time_raw,
    )


def parse_segment(data: bytes) -> Segment:
    """Parse one 80-byte segment record from the index file."""
    start_time_raw, end_time_raw, start_offset, end_offset = SEGMENT_STRUCT.unpack(data)

    return Segment(
        start_time_raw=start_time_raw,
        end_time_raw=end_time_raw,
        start_offset=start_offset,
        end_offset=end_offset,
    )


def parse_picture_segment(data: bytes) -> Segment:
    """Parse one 96-byte segment record from the picture index file."""
    start_time_raw, end_time_raw, start_offset, end_offset = (
        PICTURE_SEGMENT_STRUCT.unpack(data)
    )

    return Segment(
        start_time_raw=start_time_raw,
        end_time_raw=end_time_raw,
        start_offset=start_offset,
        end_offset=end_offset,
    )


def detect_video_segment_record_length(
    data: bytes, av_files: int, default_version: int = 3
) -> int:
    """
    Detect whether video segment records use 96 bytes (EZVIZ v3 / newer cameras)
    or 80 bytes (legacy Hikvision format) by scoring record sanity.
    """
    table_offset = HEADER_BUFFER_LENGTH + av_files * FILE_RECORD_LENGTH
    rem_len = len(data) - table_offset
    if rem_len <= 0:
        return (
            SEGMENT_RECORD_LENGTH_96 if default_version >= 3 else SEGMENT_RECORD_LENGTH
        )

    scores = {SEGMENT_RECORD_LENGTH_96: 0, SEGMENT_RECORD_LENGTH: 0}

    for stride in (SEGMENT_RECORD_LENGTH_96, SEGMENT_RECORD_LENGTH):
        # 1. Check first 32 contiguous slots
        max_scan_records = min(32, rem_len // stride)
        for i in range(max_scan_records):
            off = table_offset + i * stride
            rec = data[off : off + stride]
            if len(rec) < 48:
                break
            st_raw, et_raw, s_off, e_off = struct.unpack("<8xQQ16xII", rec[:48])
            st = st_raw & 0xFFFFFFFF
            et = et_raw & 0xFFFFFFFF
            if 0 <= s_off < e_off <= 268435456 and 1577836800 <= st <= et:
                scores[stride] += 1
                if stride == 96 and (b"INFO" in rec[48:80] or b"OFNI" in rec[48:80]):
                    scores[stride] += 1

        # 2. Check the first slot of subsequent files
        for f_idx in range(1, min(av_files, 16)):
            off = table_offset + f_idx * MAX_SEGMENTS_PER_SOURCE_FILE * stride
            if off + stride <= len(data):
                rec = data[off : off + stride]
                st_raw, et_raw, s_off, e_off = struct.unpack("<8xQQ16xII", rec[:48])
                st = st_raw & 0xFFFFFFFF
                et = et_raw & 0xFFFFFFFF
                if 0 <= s_off < e_off <= 268435456 and 1577836800 <= st <= et:
                    scores[stride] += 2

    if scores[SEGMENT_RECORD_LENGTH_96] > scores[SEGMENT_RECORD_LENGTH]:
        return SEGMENT_RECORD_LENGTH_96
    if scores[SEGMENT_RECORD_LENGTH] > scores[SEGMENT_RECORD_LENGTH_96]:
        return SEGMENT_RECORD_LENGTH

    # Tie-breaker: table size divisibility
    div_96 = rem_len % SEGMENT_RECORD_LENGTH_96 == 0
    div_80 = rem_len % SEGMENT_RECORD_LENGTH == 0
    if div_96 and not div_80:
        return SEGMENT_RECORD_LENGTH_96
    if div_80 and not div_96:
        return SEGMENT_RECORD_LENGTH

    return SEGMENT_RECORD_LENGTH_96 if default_version >= 3 else SEGMENT_RECORD_LENGTH


def load_index(
    index_path: str | Path, record_length: int | None = None
) -> tuple[IndexHeader, list[Segment]]:
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
            f"Index file is too small ({len(data)} bytes). "
            f"Expected at least {HEADER_BUFFER_LENGTH} bytes."
        )

    header = parse_index_header(data[:HEADER_BUFFER_LENGTH])

    # Parse per-file 32-byte records from header area
    file_records: list[FileRecord] = []
    file_rec_offset = HEADER_BUFFER_LENGTH
    max_possible_records = max(
        0, (len(data) - HEADER_BUFFER_LENGTH) // FILE_RECORD_LENGTH
    )
    num_records = min(header.av_files, max_possible_records)
    for _ in range(num_records):
        file_records.append(
            parse_file_record(
                data[file_rec_offset : file_rec_offset + FILE_RECORD_LENGTH]
            )
        )
        file_rec_offset += FILE_RECORD_LENGTH
    header.file_records = file_records

    if record_length is None:
        record_length = detect_video_segment_record_length(
            data, header.av_files, header.index_version
        )

    segment_struct = (
        SEGMENT_STRUCT_96
        if record_length == SEGMENT_RECORD_LENGTH_96
        else SEGMENT_STRUCT
    )

    segments: list[Segment] = []

    # Segment table starts immediately after the header + the per-file records.
    offset = HEADER_BUFFER_LENGTH + header.av_files * FILE_RECORD_LENGTH
    max_segments = header.av_files * MAX_SEGMENTS_PER_SOURCE_FILE

    for _ in range(max_segments):
        if offset + record_length > len(data):
            break
        start_time_raw, end_time_raw, start_offset, end_offset = (
            segment_struct.unpack_from(data, offset)
        )
        segments.append(
            Segment(
                start_time_raw=start_time_raw,
                end_time_raw=end_time_raw,
                start_offset=start_offset,
                end_offset=end_offset,
            )
        )
        offset += record_length

    return header, segments


def load_picture_index(
    index_path: str | Path,
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
            f"Index file is too small ({len(data)} bytes). "
            f"Expected at least {HEADER_BUFFER_LENGTH} bytes."
        )

    header = parse_index_header(data[:HEADER_BUFFER_LENGTH])

    # Parse per-file 32-byte records from header area
    file_records: list[FileRecord] = []
    file_rec_offset = HEADER_BUFFER_LENGTH
    max_possible_records = max(
        0, (len(data) - HEADER_BUFFER_LENGTH) // FILE_RECORD_LENGTH
    )
    num_records = min(header.av_files, max_possible_records)
    for _ in range(num_records):
        file_records.append(
            parse_file_record(
                data[file_rec_offset : file_rec_offset + FILE_RECORD_LENGTH]
            )
        )
        file_rec_offset += FILE_RECORD_LENGTH
    header.file_records = file_records

    segments: list[tuple[int, Segment]] = []

    # Segment table starts immediately after the header + the per-file records.
    offset = HEADER_BUFFER_LENGTH + header.av_files * FILE_RECORD_LENGTH

    current_file_idx = 0
    prev_end_offset = -1

    while offset + SEGMENT_RECORD_LENGTH_PIC <= len(data):
        header_tag = data[offset : offset + 4]
        if header_tag == b"\x00\x00\x00\x00" or header_tag == b"\xff\xff\xff\xff":
            break

        start_time_raw, end_time_raw, start_offset, end_offset = (
            PICTURE_SEGMENT_STRUCT.unpack_from(data, offset)
        )
        offset += SEGMENT_RECORD_LENGTH_PIC

        if end_offset != 0 and start_offset < end_offset:
            if prev_end_offset != -1 and start_offset < prev_end_offset:
                current_file_idx += 1

            segments.append(
                (
                    current_file_idx,
                    Segment(
                        start_time_raw=start_time_raw,
                        end_time_raw=end_time_raw,
                        start_offset=start_offset,
                        end_offset=end_offset,
                    ),
                )
            )
            prev_end_offset = end_offset

    return header, segments
