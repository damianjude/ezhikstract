"""
Tests for extractor.py

What needs testing:
- _is_valid_mpeg_ps
- process_segments & process_picture_segments
- extract_segment & extract_picture_segment
- extract_all_segments & extract_all_pictures

Important edge cases:
- Invalid MPEG-PS markers.
- Time filtering overlapping edges.
- Subprocess mocking for ffmpeg extraction.
"""

from datetime import datetime, timezone
from pathlib import Path

from ezhikstract.extractor import (
    RecordingSegment,
    _is_valid_mpeg_ps,
    extract_all_segments,
    extract_picture_segment,
    extract_segment,
    process_picture_segments,
    process_segments,
)
from ezhikstract.parser import Segment


def test_is_valid_mpeg_ps(tmp_path: Path, create_valid_mpeg_ps):
    """Valid and invalid MPEG headers should be recognized."""
    # Valid with system header
    valid_file = create_valid_mpeg_ps("valid.mp4")
    assert _is_valid_mpeg_ps(valid_file, offset=0) is True

    # Valid MPEG-PS Pack Header (0x000001BA + 0x40) WITHOUT System Header (0x000001BB)
    valid_no_sys = tmp_path / "valid_no_sys.mp4"
    valid_no_sys.write_bytes(b"\x00\x00\x01\xba\x40" + b"\x00" * 100)
    assert _is_valid_mpeg_ps(valid_no_sys, offset=0) is True

    # Invalid (missing marker)
    invalid_file = tmp_path / "invalid.mp4"
    invalid_file.write_bytes(b"\x00" * 2048)
    assert _is_valid_mpeg_ps(invalid_file, offset=0) is False

    # Invalid (too short)
    short_file = tmp_path / "short.mp4"
    short_file.write_bytes(b"\x00\x00")
    assert _is_valid_mpeg_ps(short_file, offset=0) is False

    # File not found
    assert _is_valid_mpeg_ps(tmp_path / "missing.mp4", offset=0) is False


def test_process_segments(camera_dir: Path):
    """Given a valid camera directory, process_segments should discover available videos."""
    header, segments = process_segments(camera_dir)
    assert header.av_files == 1
    assert len(segments) == 1
    assert segments[0].source_file_name == "hiv00000.mp4"
    # Ensure it's parsed as datetime
    assert isinstance(segments[0].start_dt, datetime)


def test_process_segments_missing_source_file(camera_dir: Path):
    """If the source hivXXXXX.mp4 is missing, the segments are skipped gracefully."""
    (camera_dir / "hiv00000.mp4").unlink()

    _, segments = process_segments(camera_dir)
    assert len(segments) == 0


def test_process_segments_unfinalized_active_file(tmp_path: Path):
    """Ensure segments in active/unfinalized files (segment_count=0 in file_records) are still processed."""
    import struct

    from ezhikstract.parser import MAX_SEGMENTS_PER_SOURCE_FILE, SEGMENT_RECORD_LENGTH

    cam_dir = tmp_path / "camera_active"
    cam_dir.mkdir()

    # 2 files total
    num_files = 2
    header_bytes = struct.pack(
        "<QIIII1176s76sI",
        1,
        3,
        num_files,
        num_files,
        1,
        b"\x00" * 1176,
        b"\x00" * 76,
        0,
    )

    # File 0 covers seg0, File 1 (active) covers seg1
    rec0 = struct.pack("<IIII16x", 0, 5, 1672574000, 1672574408)
    rec1 = struct.pack("<IIII16x", 1, 0, 0, 0)
    file_records = rec0 + rec1

    # Segment for File 0 (timestamp 1672574400, inside rec0 range)
    seg0 = struct.pack("<8xQQ16xII32x", 1672574400, 1672574405, 0, 1024)
    # Segment for File 1 (timestamp 1672580000, after rec0 range -> active file hiv00001)
    seg1 = struct.pack("<8xQQ16xII32x", 1672580000, 1672580005, 0, 1024)

    # Pad segments array up to 2 * MAX_SEGMENTS_PER_SOURCE_FILE
    segments_bytes = seg0 + b"\x00" * (
        SEGMENT_RECORD_LENGTH * (MAX_SEGMENTS_PER_SOURCE_FILE - 1)
    )
    segments_bytes += seg1 + b"\x00" * (
        SEGMENT_RECORD_LENGTH * (MAX_SEGMENTS_PER_SOURCE_FILE - 1)
    )

    index_path = cam_dir / "index00.bin"
    index_path.write_bytes(header_bytes + file_records + segments_bytes)

    # Create video files for both
    valid_mpeg = (
        b"\x00\x00\x01\xba\x40" + b"\x00" * 100 + b"\x00\x00\x01\xbb" + b"\x00" * 2000
    )
    (cam_dir / "hiv00000.mp4").write_bytes(valid_mpeg)
    (cam_dir / "hiv00001.mp4").write_bytes(valid_mpeg)

    _, segments = process_segments(cam_dir)
    assert len(segments) == 2
    assert segments[0].source_file_name == "hiv00000.mp4"
    assert segments[1].source_file_name == "hiv00001.mp4"


def test_extract_segment_success(camera_dir: Path, tmp_path: Path, mock_ffmpeg, mocker):
    """Mock the subprocess Popen to simulate successful extraction."""
    _, segments = process_segments(camera_dir)
    segment = segments[0]

    # Mock Popen
    mock_proc = mocker.MagicMock()
    mock_proc.returncode = 0
    mock_proc.communicate.return_value = (b"", b"")
    mock_popen = mocker.patch("subprocess.Popen", return_value=mock_proc)

    out_dir = tmp_path / "out"

    # Ensure side-effect of creating output file when Popen is called
    def _side_effect(*args, **kwargs):
        # Create output file so stat().st_size > 0 passes
        s_str = segment.start_dt.strftime("%d%m%Y %H%M%S")
        e_str = segment.end_dt.strftime("%d%m%Y %H%M%S")
        stem = f"{s_str} - {e_str} ({segment.source_file_index:05d}-{segment.source_file_segment_index:03d})"
        mp4 = out_dir / f"{stem}.mp4"
        mp4.write_bytes(b"\x00" * 100)
        return mock_proc

    mock_popen.side_effect = _side_effect

    result = extract_segment(segment, camera_dir, out_dir, replace=True)

    assert result is not None
    assert result.parent == out_dir
    assert result.suffix == ".mp4"
    mock_popen.assert_called()


def test_extract_segment_ffmpeg_failure(
    camera_dir: Path, tmp_path: Path, mock_ffmpeg, mocker
):
    """If ffmpeg fails (non-zero exit code), it should clean up the output file and return None."""
    _, segments = process_segments(camera_dir)
    segment = segments[0]

    mock_proc = mocker.MagicMock()
    mock_proc.returncode = 1
    mock_proc.communicate.return_value = (b"Error parsing stream", b"")
    mocker.patch("subprocess.Popen", return_value=mock_proc)

    out_dir = tmp_path / "out"
    result = extract_segment(segment, camera_dir, out_dir, replace=True)

    assert result is None


def test_extract_all_segments(camera_dir: Path, tmp_path: Path, mock_ffmpeg, mocker):
    """Extracting all segments should handle concurrency and call merger."""
    _, segments = process_segments(camera_dir)

    def mock_extract(seg, cam, out, replace):
        p = out / "dummy.mp4"
        p.touch(exist_ok=True)
        return p

    # Mock extract_segment to bypass real extraction logic inside the ThreadPoolExecutor
    mock_extract_segment = mocker.patch(
        "ezhikstract.extractor.extract_segment", side_effect=mock_extract
    )
    mock_merge = mocker.patch("ezhikstract.merger.merge_day")

    out_dir = tmp_path / "recordings"
    extract_all_segments(segments, camera_dir, output_dir=out_dir)

    mock_extract_segment.assert_called_once()
    mock_merge.assert_called_once()


def test_extract_all_segments_time_filter(camera_dir: Path, tmp_path: Path, mocker):
    """Time filters should correctly exclude segments outside the range."""
    # Create two segments, manually bypassing parser for speed
    seg1 = RecordingSegment(
        raw=Segment(0, 0, 0, 0),
        start_dt=datetime(2023, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        end_dt=datetime(2023, 1, 1, 12, 5, 0, tzinfo=timezone.utc),
        source_file_index=0,
        source_file_segment_index=0,
        source_file_name="test.mp4",
    )

    mock_extract = mocker.patch("ezhikstract.extractor.extract_segment")
    mocker.patch("ezhikstract.merger.merge_day")

    # Time filter completely outside segment
    extract_all_segments([seg1], camera_dir, from_time="2023-01-01 13:00:00")
    mock_extract.assert_not_called()

    # Time filter overlaps segment
    extract_all_segments([seg1], camera_dir, from_time="2023-01-01 11:00:00")
    mock_extract.assert_called_once()


def test_process_picture_segments(
    tmp_path: Path, create_valid_index00p, create_valid_pic
):
    """Valid picture directories should be processed."""
    cam_dir = tmp_path / "camera"
    cam_dir.mkdir()

    index_path = create_valid_index00p(1)
    index_path.rename(cam_dir / "index00p.bin")

    pic_path = create_valid_pic("hiv00000.pic", 200)
    pic_path.rename(cam_dir / "hiv00000.pic")

    header, segments = process_picture_segments(cam_dir)
    assert header.av_files == 1
    assert len(segments) == 1
    assert segments[0].source_file_name == "hiv00000.pic"


def test_extract_picture_segment(tmp_path: Path):
    """Pictures are extracted by slicing bytes directly and trimming SOI/EOI alignment padding."""
    cam_dir = tmp_path / "camera"
    cam_dir.mkdir()

    # Raw picture data with 4 leading padding bytes, JPEG payload (FF D8 ... FF D9), and 3 trailing padding bytes
    raw_payload = b"\x00\x00\x00\x00\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9\x00\x00\x00"
    (cam_dir / "hiv00000.pic").write_bytes(raw_payload)

    out_dir = tmp_path / "out"

    seg = RecordingSegment(
        raw=Segment(
            start_time_raw=123,
            end_time_raw=123,
            start_offset=0,
            end_offset=len(raw_payload),
        ),
        start_dt=datetime(2023, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        end_dt=datetime(2023, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        source_file_index=0,
        source_file_segment_index=0,
        source_file_name="hiv00000.pic",
    )

    result = extract_picture_segment(seg, cam_dir, out_dir, replace=True)
    assert result is not None
    assert result.exists()

    data = result.read_bytes()
    assert data.startswith(b"\xff\xd8")
    assert data.endswith(b"\xff\xd9")
    assert len(data) == 22
