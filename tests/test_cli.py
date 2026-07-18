"""
Tests for cli.py

What needs testing:
- list videos / list pictures
- extract videos / extract pictures

Important edge cases:
- Missing/invalid input directories.
- Bad time string formats.
- Exception handling in CLI.
"""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from ezhikstract.cli import app


runner = CliRunner()


def test_cli_list_videos_invalid_dir():
    """An invalid directory should exit with code 2 gracefully via Typer's built-in checks."""
    result = runner.invoke(app, ["list", "videos", "/does/not/exist"])
    assert result.exit_code != 0
    assert "does not exist" in result.output


def test_cli_list_videos_no_index(tmp_path: Path):
    """If the directory exists but index00.bin doesn't, it should raise a clear error."""
    result = runner.invoke(app, ["list", "videos", str(tmp_path)])
    assert result.exit_code == 1
    assert "not found" in result.stderr


def test_cli_extract_videos(tmp_path: Path, mocker):
    """Extract videos command should parse arguments and call the extractor."""
    # Setup dummy directory
    cam_dir = tmp_path / "camera"
    cam_dir.mkdir()
    
    # Mock the internal logic
    mock_process = mocker.patch("ezhikstract.extractor.process_segments", return_value=(None, []))
    mock_extract = mocker.patch("ezhikstract.extractor.extract_all_segments")
    
    out_dir = tmp_path / "out"
    
    result = runner.invoke(
        app,
        ["extract", "videos", str(cam_dir), "--output", str(out_dir), "--from", "2023-01-01 12:00:00"]
    )
    
    assert result.exit_code == 0
    mock_process.assert_called_once_with(cam_dir)
    mock_extract.assert_called_once()
    
    # Verify the time argument is passed properly
    _, kwargs = mock_extract.call_args
    assert kwargs["from_time"] == "2023-01-01 12:00:00"


def test_cli_extract_pictures(tmp_path: Path, mocker):
    """Extract pictures command should parse arguments and call the picture extractor."""
    cam_dir = tmp_path / "camera"
    cam_dir.mkdir()
    
    mock_process = mocker.patch("ezhikstract.extractor.process_picture_segments", return_value=(None, []))
    mock_extract = mocker.patch("ezhikstract.extractor.extract_all_pictures")
    
    result = runner.invoke(app, ["extract", "pictures", str(cam_dir)])
    
    assert result.exit_code == 0
    mock_process.assert_called_once_with(cam_dir)
    mock_extract.assert_called_once()
