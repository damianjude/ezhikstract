import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import pytest

# Size constants from parser.py
HEADER_BUFFER_LENGTH = 1280
FILE_RECORD_LENGTH = 32
SEGMENT_RECORD_LENGTH = 80
SEGMENT_RECORD_LENGTH_PIC = 96


@pytest.fixture
def create_valid_index00(tmp_path: Path) -> Callable[[int, int], Path]:
    """Factory to create a valid index00.bin file with given number of av_files and segments."""

    def _create(num_files: int = 1, num_segments: int = 1) -> Path:
        path = tmp_path / "index00.bin"

        # Build header
        header = struct.pack(
            "<QIIII1176s76sI",
            1,  # modify_counter
            3,  # index_version
            num_files,  # av_files
            num_files,  # next_file_no
            max(0, num_files - 1),  # last_file_no
            b"\x00" * 1176,  # cur_file_info
            b"\x00" * 76,  # unknown
            0,  # checksum
        )
        # 1280 bytes

        file_records = b"\x00" * (FILE_RECORD_LENGTH * num_files)

        segment_records = b""
        for i in range(num_segments):
            start_time = int(
                datetime(2023, 1, 1, 12, 0, i, tzinfo=timezone.utc).timestamp()
            )
            end_time = int(
                datetime(2023, 1, 1, 12, 0, i + 1, tzinfo=timezone.utc).timestamp()
            )
            start_offset = i * 1024
            end_offset = (i + 1) * 1024

            segment_record = struct.pack(
                "<8xQQ16xII32x",
                start_time,
                end_time,
                start_offset,
                end_offset,
            )
            segment_records += segment_record

        content = header + file_records + segment_records
        path.write_bytes(content)
        return path

    return _create


@pytest.fixture
def create_valid_index00p(tmp_path: Path) -> Callable[[int], Path]:
    """Factory to create a valid index00p.bin (picture) file."""

    def _create(num_segments: int = 1) -> Path:
        path = tmp_path / "index00p.bin"

        header = struct.pack(
            "<QIIII1176s76sI",
            1,
            3,
            1,
            1,
            0,
            b"\x00" * 1176,
            b"\x00" * 76,
            0,
        )

        file_records = b"\x00" * FILE_RECORD_LENGTH

        segment_records = b""
        for i in range(num_segments):
            time_val = int(
                datetime(2023, 1, 1, 12, 0, i, tzinfo=timezone.utc).timestamp()
            )
            start_offset = i * 100
            end_offset = (i + 1) * 100

            segment_record = struct.pack(
                "<4s4xQQ16xII48x",
                b"\x01\x02\x03\x04",
                time_val,
                time_val,
                start_offset,
                end_offset,
            )
            segment_records += segment_record

        content = header + file_records + segment_records
        path.write_bytes(content)
        return path

    return _create


@pytest.fixture
def create_valid_mpeg_ps(tmp_path: Path) -> Callable[[str, int], Path]:
    """Factory to create a mock hivXXXXX.mp4 file containing valid MPEG-PS headers."""

    def _create(filename: str = "hiv00000.mp4", size: int = 2048) -> Path:
        path = tmp_path / filename
        # Valid MPEG-PS start (0x000001BA) with MPEG-2 marker (0x40)
        content = bytearray(b"\x00\x00\x01\xba\x40")
        content += b"\x00" * 100
        content += b"\x00\x00\x01\xbb"
        content += b"\x00" * max(0, size - len(content))
        path.write_bytes(content)
        return path

    return _create


@pytest.fixture
def create_valid_pic(tmp_path: Path) -> Callable[[str, int], Path]:
    """Factory to create a mock hivXXXXX.pic file containing valid JPEG headers."""

    def _create(filename: str = "hiv00000.pic", size: int = 100) -> Path:
        path = tmp_path / filename
        content = bytearray(b"\xff\xd8\xff")
        content += b"\x00" * max(0, size - len(content))
        path.write_bytes(content)
        return path

    return _create


@pytest.fixture
def camera_dir(tmp_path: Path, create_valid_index00, create_valid_mpeg_ps) -> Path:
    """Fixture providing a realistic SD card camera directory."""
    cam_dir = tmp_path / "camera"
    cam_dir.mkdir()

    # Generate index00.bin
    create_valid_index00.__defaults__ = (1, 2)  # 1 av_file, 2 segments
    index_path = create_valid_index00()
    index_path.rename(cam_dir / "index00.bin")

    # Generate matching hiv00000.mp4
    mpeg_path = create_valid_mpeg_ps("hiv00000.mp4", 4096)
    mpeg_path.rename(cam_dir / "hiv00000.mp4")

    return cam_dir


@pytest.fixture
def mock_ffmpeg(mocker):
    """Mocks imageio_ffmpeg.get_ffmpeg_exe and prevents subprocess execution."""
    mocker.patch("imageio_ffmpeg.get_ffmpeg_exe", return_value="/mock/bin/ffmpeg")
