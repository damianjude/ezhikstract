"""
merger.py

Concatenates per-segment .mp4 files for a single day into one merged .mp4
using ffmpeg's concat demuxer (stream-copy, no re-encoding).
"""

import subprocess
import tempfile
from pathlib import Path

import imageio_ffmpeg  # type: ignore[import-untyped]


def merge_day(
    segment_files: list[Path],
    output_path: Path,
    *,
    replace: bool = True,
) -> Path | None:
    """
    Concatenate segment_files (already time-sorted) into a single .mp4 at output_path.

    Returns the path to the merged file, or None if ffmpeg fails.
    """
    if not segment_files:
        return None

    if output_path.exists() and not replace:
        return output_path

    try:
        output_path.unlink(missing_ok=True)
    except OSError as error:
        print(f"Error: Failed to delete existing output file {output_path}: {error}")
        return None

    # Escape single quotes to prevent syntax errors in the ffmpeg concat file
    lines: list[str] = []
    for f in segment_files:
        escaped_path = f.resolve().as_posix().replace("'", "'\\''")
        lines.append(f"file '{escaped_path}'")

    # Create temporary concat instruction file for ffmpeg
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write("\n".join(lines))
            concat_list = Path(tmp.name)
    except OSError as error:
        print(f"Failed to create temporary concat file: {error}")
        return None

    # Stream-copy concatenate segment files without re-encoding
    cmd = [
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_list),
        "-c",
        "copy",
        "-y",
        str(output_path),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        if result.stderr:
            # Avoid printing all standard logs of ffmpeg to avoid noise, but we can print it if there's any actual issue.
            pass
        print(f"Merged → {output_path.name}")
        return output_path
    except subprocess.CalledProcessError as error:
        print(f"Merge failed with exit code {error.returncode}.")
        if error.stderr:
            print(f"ffmpeg stderr:\n{error.stderr}")
        if output_path.exists():
            output_path.unlink(missing_ok=True)
        return None
    except (subprocess.SubprocessError, OSError) as error:
        print(f"Merge failed: {error}")
        if output_path.exists():
            output_path.unlink(missing_ok=True)
        return None
    finally:
        concat_list.unlink(missing_ok=True)
