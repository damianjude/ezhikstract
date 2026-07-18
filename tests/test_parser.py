"""
Tests for parser.py

What needs testing:
- parse_index_header
- parse_segment
- parse_picture_segment
- load_index
- load_picture_index

Important edge cases:
- Invalid index file sizes (too small)
- Corrupted values (very large av_files) preventing memory exhaustion / tight loops.
- Empty padding blocks terminating picture segment parsing.
"""

import struct
from pathlib import Path

import pytest
from hypothesis import given, settings, HealthCheck, strategies as st

from ezhikstract.parser import (
    HEADER_BUFFER_LENGTH,
    FILE_RECORD_LENGTH,
    SEGMENT_RECORD_LENGTH,
    SEGMENT_RECORD_LENGTH_PIC,
    load_index,
    load_picture_index,
    parse_index_header,
    parse_segment,
    parse_picture_segment,
)


@given(
    modify_counter=st.integers(min_value=0, max_value=2**64 - 1),
    index_version=st.integers(min_value=0, max_value=2**32 - 1),
    av_files=st.integers(min_value=0, max_value=2**32 - 1),
    next_file_no=st.integers(min_value=0, max_value=2**32 - 1),
    last_file_no=st.integers(min_value=0, max_value=2**32 - 1),
    cur_file_info=st.binary(min_size=1176, max_size=1176),
    unknown=st.binary(min_size=76, max_size=76),
    checksum=st.integers(min_value=0, max_value=2**32 - 1),
)
def test_parse_index_header_property(
    modify_counter,
    index_version,
    av_files,
    next_file_no,
    last_file_no,
    cur_file_info,
    unknown,
    checksum,
):
    """Property-based test to ensure header parsing handles all valid unsigned int ranges."""
    data = struct.pack(
        "<QIIII1176s76sI",
        modify_counter,
        index_version,
        av_files,
        next_file_no,
        last_file_no,
        cur_file_info,
        unknown,
        checksum,
    )
    header = parse_index_header(data)
    assert header.modify_counter == modify_counter
    assert header.av_files == av_files
    assert header.checksum == checksum


@given(st.binary(min_size=0, max_size=HEADER_BUFFER_LENGTH - 1))
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_load_index_too_small(tmp_path: Path, data: bytes):
    """Loading an index file smaller than the header buffer should raise ValueError."""
    index_path = tmp_path / "index00.bin"
    index_path.write_bytes(data)

    with pytest.raises(ValueError, match="Index file is too small"):
        load_index(str(index_path))


@given(st.binary(min_size=0, max_size=HEADER_BUFFER_LENGTH - 1))
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_load_picture_index_too_small(tmp_path: Path, data: bytes):
    """Loading a picture index file smaller than the header buffer should raise ValueError."""
    index_path = tmp_path / "index00p.bin"
    index_path.write_bytes(data)

    with pytest.raises(ValueError, match="Index file is too small"):
        load_picture_index(str(index_path))


def test_load_index_valid(create_valid_index00):
    """Valid index files should be parsed, yielding the header and segments."""
    index_path = create_valid_index00(num_files=2, num_segments=3)
    header, segments = load_index(str(index_path))

    assert header.av_files == 2
    assert len(segments) == 3
    assert segments[0].start_offset == 0
    assert segments[0].end_offset == 1024


def test_load_picture_index_valid(create_valid_index00p):
    """Valid picture index files should pack consecutive pictures into segments."""
    index_path = create_valid_index00p(num_segments=3)
    header, segments = load_picture_index(str(index_path))

    assert header.av_files == 1
    assert len(segments) == 3
    # Verify tuple layout is (file_idx, segment)
    assert segments[0][0] == 0
    assert segments[0][1].start_offset == 0


def test_parse_segment_offsets():
    """Verify struct offsets for segments align correctly with raw bytes."""
    # Build 80 byte record
    # 8 padding, 2 Q (start, end), 16 padding, 2 I (start, end offset), 32 padding
    start_time = 1234567890
    end_time = 1234567891
    start_offset = 100
    end_offset = 200

    data = struct.pack("<8xQQ16xII32x", start_time, end_time, start_offset, end_offset)

    assert len(data) == SEGMENT_RECORD_LENGTH
    seg = parse_segment(data)
    assert seg.start_time_raw == start_time
    assert seg.end_offset == end_offset


def test_parse_picture_segment_offsets():
    """Verify struct offsets for picture segments align correctly with raw bytes."""
    # Build 96 byte record
    # 8 padding, 2 Q (start, end), 16 padding, 2 I (start, end offset), 48 padding
    time_val = 1234567890
    start_offset = 100
    end_offset = 200

    data = struct.pack("<8xQQ16xII48x", time_val, time_val, start_offset, end_offset)

    assert len(data) == SEGMENT_RECORD_LENGTH_PIC
    seg = parse_picture_segment(data)
    assert seg.start_time_raw == time_val
    assert seg.end_offset == end_offset


def test_load_index_corrupt_av_files_avoids_memory_error(
    create_valid_index00, tmp_path: Path
):
    """An maliciously high av_files value shouldn't crash the loop via out-of-bounds offset reads."""
    index_path = tmp_path / "index00.bin"
    # Create header with av_files = 1,000,000 but small file size
    header = struct.pack(
        "<QIIII1176s76sI", 1, 3, 1000000, 1, 0, b"\x00" * 1176, b"\x00" * 76, 0
    )
    index_path.write_bytes(header)

    # Should safely terminate loop as offset will exceed file size
    header_res, segments_res = load_index(str(index_path))
    assert header_res.av_files == 1000000
    assert len(segments_res) == 0


def test_load_picture_index_terminates_on_padding(tmp_path: Path):
    """A padding block (\x00\x00\x00\x00) should cleanly terminate parsing of index00p.bin."""
    index_path = tmp_path / "index00p.bin"

    header = struct.pack(
        "<QIIII1176s76sI", 1, 3, 1, 1, 0, b"\x00" * 1176, b"\x00" * 76, 0
    )
    # File record
    file_record = b"\x00" * FILE_RECORD_LENGTH
    # 1 valid segment
    time_val = 1234567890
    seg = struct.pack(
        "<4s4xQQ16xII48x", b"\x01\x02\x03\x04", time_val, time_val, 0, 100
    )
    # 1 padding block
    pad = b"\x00\x00\x00\x00" + b"\xff" * (SEGMENT_RECORD_LENGTH_PIC - 4)

    index_path.write_bytes(header + file_record + seg + pad)

    header_res, segments_res = load_picture_index(str(index_path))

    # Should only read the first segment, returning lengths of 1
    assert len(segments_res) == 1
