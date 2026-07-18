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

import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ezhikstract.extractor import (
    _is_valid_mpeg_ps,
    process_segments,
    extract_segment,
    extract_all_segments,
    process_picture_segments,
    extract_picture_segment,
    extract_all_pictures,
    RecordingSegment,
)
from ezhikstract.parser import Segment


def test_is_valid_mpeg_ps(tmp_path: Path, create_valid_mpeg_ps):
    """Valid and invalid MPEG headers should be recognized."""
    # Valid
    valid_file = create_valid_mpeg_ps("valid.mp4")
    assert _is_valid_mpeg_ps(valid_file, offset=0) is True

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
    
    header, segments = process_segments(camera_dir)
    assert len(segments) == 0


def test_extract_segment_success(camera_dir: Path, tmp_path: Path, mock_ffmpeg, mocker):
    """Mock the subprocess Popen to simulate successful extraction."""
    _, segments = process_segments(camera_dir)
    segment = segments[0]

    # Mock Popen
    mock_proc = mocker.MagicMock()
    mock_proc.wait.return_value = 0
    mock_proc.stderr.read.return_value = b""
    mock_popen = mocker.patch("subprocess.Popen", return_value=mock_proc)

    out_dir = tmp_path / "out"
    result = extract_segment(segment, camera_dir, out_dir, replace=True)

    assert result is not None
    assert result.parent == out_dir
    assert result.suffix == ".mp4"
    mock_popen.assert_called_once()
    mock_proc.stdin.write.assert_called()


def test_extract_segment_ffmpeg_failure(camera_dir: Path, tmp_path: Path, mock_ffmpeg, mocker):
    """If ffmpeg fails (non-zero exit code), it should clean up the output file and return None."""
    _, segments = process_segments(camera_dir)
    segment = segments[0]

    mock_proc = mocker.MagicMock()
    mock_proc.wait.return_value = 1
    mock_proc.stderr.read.return_value = b"Error parsing stream"
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
    mock_extract_segment = mocker.patch("ezhikstract.extractor.extract_segment", side_effect=mock_extract)
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


def test_process_picture_segments(tmp_path: Path, create_valid_index00p, create_valid_pic):
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


def test_extract_picture_segment(tmp_path: Path, create_valid_pic):
    """Pictures are extracted by slicing bytes directly."""
    cam_dir = tmp_path / "camera"
    cam_dir.mkdir()
    pic_path = create_valid_pic("hiv00000.pic", 200)
    pic_path.rename(cam_dir / "hiv00000.pic")
    
    out_dir = tmp_path / "out"
    
    seg = RecordingSegment(
        raw=Segment(start_time_raw=123, end_time_raw=123, start_offset=0, end_offset=100),
        start_dt=datetime(2023, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        end_dt=datetime(2023, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        source_file_index=0,
        source_file_segment_index=0,
        source_file_name="hiv00000.pic",
    )
    
    result = extract_picture_segment(seg, cam_dir, out_dir, replace=True)
    assert result is not None
    assert result.exists()
    assert result.stat().st_size == 100
