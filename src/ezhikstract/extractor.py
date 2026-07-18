import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile

import imageio_ffmpeg

from .parser import (
    IndexHeader,
    MAX_SEGMENTS_PER_SOURCE_FILE,
    Segment,
    load_index,
)

_DATE_MASK: int = 0x00000000FFFFFFFF  # lower 32 bits of the 64-bit time field


@dataclass
class RecordingSegment:
    raw: Segment

    start_dt: datetime
    end_dt: datetime

    source_file_index: int
    source_file_segment_index: int
    source_file_name: str  # e.g. "hiv00002.mp4"


def _is_valid_mpeg_ps(path: Path, offset: int) -> bool:
    """
    Peek at the bytes at offset and confirm the MPEG Program Stream Pack Start Code (0x000001BA),
    MPEG-2 marker, and System Header (0x000001BB) within the first 2KB are present.
    """
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            buffer = fh.read(2048)
    except (OSError, ValueError):
        return False

    if len(buffer) < 5:
        return False

    # Check for MPEG-PS start prefix (0x000001BA) and the MPEG-2 marker
    if not (
        int.from_bytes(buffer[:4], "big") == 0x000001BA and (buffer[4] & 0xC0) == 0x40
    ):
        return False

    # Check for System Header (0x000001BB) within the first 2KB, matching mpegPsValidator.js
    return b"\x00\x00\x01\xbb" in buffer


def process_segments(camera_dir: Path) -> tuple[IndexHeader, list[RecordingSegment]]:
    """
    Parse index00.bin, validate each segment against its source file, and return a time-sorted list of valid RecordingSegments.
    """
    index_path = camera_dir / "index00.bin"
    # Attempt to load and parse the binary index file
    try:
        header, raw_segments = load_index(str(index_path))
    except FileNotFoundError:
        raise FileNotFoundError(f"Index file index00.bin not found in '{camera_dir}'.")
    except OSError as error:
        raise OSError(f"Failed to read index file '{index_path}': {error}")

    segments: list[RecordingSegment] = []
    skipped = 0
    warned_missing: set[str] = set()

    for source_file_index in range(header.av_files):
        if source_file_index * MAX_SEGMENTS_PER_SOURCE_FILE >= len(raw_segments):
            break
        for source_file_segment_index in range(MAX_SEGMENTS_PER_SOURCE_FILE):
            flat = (
                source_file_index * MAX_SEGMENTS_PER_SOURCE_FILE
                + source_file_segment_index
            )
            if flat >= len(raw_segments):
                break

            seg = raw_segments[flat]
            if seg.end_time_raw == 0:
                continue

            # Filter out corrupted records with inverted offsets or start/end times
            if seg.start_offset >= seg.end_offset or (
                seg.start_time_raw & _DATE_MASK
            ) >= (seg.end_time_raw & _DATE_MASK):
                skipped += 1
                continue

            source_name = f"hiv{source_file_index:05d}.mp4"
            source_path = camera_dir / source_name

            if not source_path.exists():
                # Warn once per missing video file to avoid spamming output
                if source_name not in warned_missing:
                    print(
                        f"Warning: Source file '{source_name}' does not exist. Skipping its segments.",
                        file=sys.stderr,
                    )
                    warned_missing.add(source_name)
                skipped += 1
                continue

            try:
                if seg.end_offset > source_path.stat().st_size:
                    skipped += 1
                    continue
            except OSError:
                skipped += 1
                continue

            if not _is_valid_mpeg_ps(source_path, seg.start_offset):
                skipped += 1
                continue

            # Apply date mask to extract the lower 32-bit Unix epoch timestamp
            segments.append(
                RecordingSegment(
                    raw=seg,
                    start_dt=datetime.fromtimestamp(
                        seg.start_time_raw & _DATE_MASK, tz=timezone.utc
                    ),
                    end_dt=datetime.fromtimestamp(
                        seg.end_time_raw & _DATE_MASK, tz=timezone.utc
                    ),
                    source_file_index=source_file_index,
                    source_file_segment_index=source_file_segment_index,
                    source_file_name=source_name,
                )
            )

    segments.sort(key=lambda s: s.start_dt)  # sort by datetime

    summary = f"Found {len(segments)} recordings"
    if skipped:
        summary += f", skipped {skipped} invalid"
    print(summary)

    return header, segments


def extract_segment(
    segment: RecordingSegment,
    camera_dir: Path,
    output_dir: Path,
    *,
    replace: bool = True,
) -> Path | None:
    """
    Extract one recording segment from its source .mp4 container and remux it into a proper .mp4:
      - Video: HEVC stream-copied (hvc1 tag for broad compatibility)
      - Audio: re-encoded to Opus at 64 kbps (pcm_alaw isn't valid in .mp4)

    The segment is read in chunks and piped directly to ffmpeg for remuxing.
    Returns the path to the produced .mp4, or None on failure.
    """
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        print(
            f"Error: Failed to create output directory {output_dir}: {error}",
            file=sys.stderr,
        )
        return None

    start_str = segment.start_dt.strftime("%d%m%Y %H%M%S")
    end_str = segment.end_dt.strftime("%d%m%Y %H%M%S")
    stem = (
        f"{start_str} - {end_str} "
        f"({segment.source_file_index:05d}-{segment.source_file_segment_index:03d})"
    )
    mp4_file = output_dir / f"{stem}.mp4"

    if mp4_file.exists() and not replace:
        return mp4_file

    try:
        mp4_file.unlink(missing_ok=True)
    except OSError as error:
        print(
            f"Error: Failed to delete existing output file {mp4_file}: {error}",
            file=sys.stderr,
        )
        return None

    # Stream-copy HEVC video and re-encode audio to Opus
    cmd = [
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-loglevel",
        "error",
        "-f",
        "mpeg",
        "-i",
        "pipe:0",
        "-c:v",
        "copy",
        "-tag:v",
        "hvc1",
        "-c:a",
        "libopus",
        "-b:a",
        "64k",
        "-y",
        str(mp4_file),
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except (subprocess.SubprocessError, OSError) as error:
        print(
            f"Failed to start ffmpeg process for segment {segment.start_dt}: {error}",
            file=sys.stderr,
        )
        return None

    if proc.stdin is None or proc.stderr is None:
        print(
            f"Failed to initialize ffmpeg pipes for segment {segment.start_dt}",
            file=sys.stderr,
        )
        return None

    try:
        with open(camera_dir / segment.source_file_name, "rb") as fh:
            fh.seek(segment.raw.start_offset)
            remaining = segment.raw.end_offset - segment.raw.start_offset

            try:
                while remaining > 0:
                    chunk_size = min(remaining, 1024 * 1024)  # 1MB chunk
                    chunk = fh.read(chunk_size)
                    if not chunk:
                        break
                    proc.stdin.write(chunk)
                    remaining -= len(chunk)
                proc.stdin.close()
            except (BrokenPipeError, ConnectionResetError):
                pass

        stderr_bytes = proc.stderr.read()
        return_code = proc.wait()

        if return_code != 0:
            stderr_text = stderr_bytes.decode("utf-8", errors="replace")
            print(
                f"ffmpeg failed on segment {segment.start_dt} with exit code {return_code}.\n"
                f"ffmpeg stderr: {stderr_text}",
                file=sys.stderr,
            )
            try:
                mp4_file.unlink(missing_ok=True)
            except OSError:
                pass
            return None

        return mp4_file
    except Exception as error:
        print(f"Failed to extract segment {segment.start_dt}: {error}", file=sys.stderr)
        proc.kill()
        try:
            mp4_file.unlink(missing_ok=True)
        except OSError:
            pass
        return None


def extract_all_segments(
    segments: list[RecordingSegment],
    camera_dir: Path,
    *,
    from_time: str | None = None,
    to_time: str | None = None,
    output_dir: Path = Path("extracted"),
    replace: bool = True,
) -> None:
    """
    Extract all (or a filtered subset of) recording segments, merging each day's output into a single .mp4 in output_dir.

    Time filters use "YYYY-MM-DD HH:MM:SS" format (UTC).
    """
    from .merger import merge_day  # local import avoids circular dependency
    from rich.progress import Progress, BarColumn, TextColumn, TimeRemainingColumn

    to_process = segments
    if from_time or to_time:
        fmt = "%Y-%m-%d %H:%M:%S"
        try:
            start_dt = (
                datetime.strptime(from_time, fmt).replace(tzinfo=timezone.utc)
                if from_time
                else None
            )
        except ValueError:
            raise ValueError(
                f"Invalid --from time format. Expected 'YYYY-MM-DD HH:MM:SS', got '{from_time}'"
            )
        try:
            end_dt = (
                datetime.strptime(to_time, fmt).replace(tzinfo=timezone.utc)
                if to_time
                else None
            )
        except ValueError:
            raise ValueError(
                f"Invalid --to time format. Expected 'YYYY-MM-DD HH:MM:SS', got '{to_time}'"
            )

        to_process = [
            s
            for s in segments
            if (start_dt is None or s.end_dt > start_dt)
            and (end_dt is None or s.start_dt < end_dt)
        ]

    print(f"{len(to_process)} of {len(segments)} segments will be extracted")
    if not to_process:
        return

    # Group recording segments by calendar day
    by_day: dict[str, list[RecordingSegment]] = {}
    for seg in to_process:
        by_day.setdefault(seg.start_dt.strftime("%Y-%m-%d"), []).append(seg)

    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise OSError(f"Failed to create output directory '{output_dir}': {error}")

    with Progress(
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TextColumn("({task.completed}/{task.total} segments)"),
        TimeRemainingColumn(),
    ) as progress:
        task_id = progress.add_task(
            "Extracting video segments...", total=len(to_process)
        )

        for day_key in sorted(by_day):
            day_segs = by_day[day_key]
            progress.console.print(
                f"[bold green]Processing {day_key} ({len(day_segs)} segments)[/bold green]"
            )

            # Generate target output path for the daily merged video
            first_start = day_segs[0].start_dt
            output_name = first_start.strftime("%d%m%Y %H%M%S") + ".mp4"
            output_path = output_dir / output_name

            if output_path.exists() and not replace:
                progress.console.print(
                    f"Merged file {output_name} already exists. Skipping day {day_key}."
                )
                progress.advance(task_id, advance=len(day_segs))
                continue

            # Extract segments into a temp directory to avoid cluttering output_dir
            with tempfile.TemporaryDirectory(dir=output_dir) as tmpdir_str:
                tmpdir = Path(tmpdir_str)
                extracted_map: dict[int, Path] = {}

                # Limit concurrency to 4 workers or CPU cores to avoid overwhelming the disk
                max_workers = min(4, os.cpu_count() or 1)
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = {
                        executor.submit(
                            extract_segment, seg, camera_dir, tmpdir, replace=replace
                        ): seg
                        for seg in day_segs
                    }
                    for future in as_completed(futures):
                        seg = futures[future]
                        try:
                            path = future.result()
                            if path and path.exists():
                                extracted_map[id(seg)] = path
                        except Exception as error:
                            progress.console.print(
                                f"[bold red]Error extracting segment {seg.start_dt}: {error}[/bold red]",
                            )
                        progress.advance(task_id)

                # Ensure extracted segments are sorted chronologically by their start_dt
                extracted = [
                    extracted_map[id(seg)]
                    for seg in day_segs
                    if id(seg) in extracted_map
                ]

                if extracted:
                    merge_day(extracted, output_path, replace=replace)


def log_available_recordings(segments: list[RecordingSegment]) -> None:
    """Print a human-readable list of all available recordings."""
    for i, seg in enumerate(segments):
        start = seg.start_dt.strftime("%Y-%m-%d %H:%M:%S")
        end = seg.end_dt.strftime("%Y-%m-%d %H:%M:%S")
        print(
            f"{i:>4}  {seg.source_file_name}  {start} → {end}  "
            f"({seg.raw.start_offset:09d} – {seg.raw.end_offset:09d})"
        )


def process_picture_segments(
    camera_dir: Path,
) -> tuple[IndexHeader, list[RecordingSegment]]:
    """
    Parse index00p.bin, validate each segment against its source file, and return a time-sorted list of valid RecordingSegments for pictures.
    """
    from .parser import load_picture_index

    index_path = camera_dir / "index00p.bin"
    # Attempt to load and parse the binary index file
    try:
        header, raw_segments = load_picture_index(str(index_path))
    except FileNotFoundError:
        raise FileNotFoundError(f"Index file index00p.bin not found in '{camera_dir}'.")
    except OSError as error:
        raise OSError(f"Failed to read index file '{index_path}': {error}")

    segments: list[RecordingSegment] = []
    skipped = 0
    warned_missing: set[str] = set()

    for source_file_index, seg in raw_segments:
        # Filter out corrupted records with inverted offsets or start/end times
        if seg.start_offset >= seg.end_offset or (seg.start_time_raw & _DATE_MASK) > (
            seg.end_time_raw & _DATE_MASK
        ):
            skipped += 1
            continue

        source_name = f"hiv{source_file_index:05d}.pic"
        source_path = camera_dir / source_name

        if not source_path.exists():
            if source_name not in warned_missing:
                print(
                    f"Warning: Source file '{source_name}' does not exist. Skipping its segments.",
                    file=sys.stderr,
                )
                warned_missing.add(source_name)
            skipped += 1
            continue

        try:
            if seg.end_offset > source_path.stat().st_size:
                skipped += 1
                continue
        except (OSError, ValueError):
            skipped += 1
            continue

        try:
            with open(source_path, "rb") as fh:
                fh.seek(seg.start_offset)
                magic = fh.read(3)
                if magic != b"\xff\xd8\xff":
                    skipped += 1
                    continue
        except (OSError, ValueError):
            skipped += 1
            continue

        segments.append(
            RecordingSegment(
                raw=seg,
                start_dt=datetime.fromtimestamp(
                    seg.start_time_raw & _DATE_MASK, tz=timezone.utc
                ),
                end_dt=datetime.fromtimestamp(
                    seg.end_time_raw & _DATE_MASK, tz=timezone.utc
                ),
                source_file_index=source_file_index,
                source_file_segment_index=0,
                source_file_name=source_name,
            )
        )

    segments.sort(key=lambda s: s.start_dt)  # sort by datetime

    summary = f"Found {len(segments)} pictures"
    if skipped:
        summary += f", skipped {skipped} invalid"
    print(summary)

    return header, segments


def extract_picture_segment(
    segment: RecordingSegment,
    camera_dir: Path,
    output_dir: Path,
    *,
    replace: bool = True,
) -> Path | None:
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        print(
            f"Error: Failed to create output directory {output_dir}: {error}",
            file=sys.stderr,
        )
        return None

    start_str = segment.start_dt.strftime("%d%m%Y %H%M%S")
    stem = f"{start_str} ({segment.source_file_index:05d}-{segment.raw.start_offset})"
    jpg_file = output_dir / f"{stem}.jpg"

    if jpg_file.exists() and not replace:
        return jpg_file

    try:
        with open(camera_dir / segment.source_file_name, "rb") as fh:
            fh.seek(segment.raw.start_offset)
            raw = fh.read(segment.raw.end_offset - segment.raw.start_offset)
        jpg_file.write_bytes(raw)
        return jpg_file
    except OSError as error:
        print(
            f"Error: Failed to extract picture {segment.start_dt}: {error}",
            file=sys.stderr,
        )
        return None


def extract_all_pictures(
    segments: list[RecordingSegment],
    camera_dir: Path,
    *,
    from_time: str | None = None,
    to_time: str | None = None,
    output_dir: Path = Path("extracted"),
    replace: bool = True,
) -> None:
    from rich.progress import Progress, BarColumn, TextColumn, TimeRemainingColumn

    to_process = segments
    if from_time or to_time:
        fmt = "%Y-%m-%d %H:%M:%S"
        try:
            start_dt = (
                datetime.strptime(from_time, fmt).replace(tzinfo=timezone.utc)
                if from_time
                else None
            )
        except ValueError:
            raise ValueError(
                f"Invalid --from time format. Expected 'YYYY-MM-DD HH:MM:SS', got '{from_time}'"
            )
        try:
            end_dt = (
                datetime.strptime(to_time, fmt).replace(tzinfo=timezone.utc)
                if to_time
                else None
            )
        except ValueError:
            raise ValueError(
                f"Invalid --to time format. Expected 'YYYY-MM-DD HH:MM:SS', got '{to_time}'"
            )

        # For pictures, start_dt and end_dt are equal, so standard overlap check works:
        to_process = [
            s
            for s in segments
            if (start_dt is None or s.end_dt >= start_dt)
            and (end_dt is None or s.start_dt < end_dt)
        ]

    print(f"{len(to_process)} of {len(segments)} pictures will be extracted")
    if not to_process:
        return

    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise OSError(f"Failed to create output directory '{output_dir}': {error}")

    with Progress(
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TextColumn("({task.completed}/{task.total} pictures)"),
        TimeRemainingColumn(),
    ) as progress:
        task_id = progress.add_task("Extracting pictures...", total=len(to_process))

        max_workers = min(8, os.cpu_count() or 1)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(
                    extract_picture_segment,
                    seg,
                    camera_dir,
                    output_dir,
                    replace=replace,
                )
                for seg in to_process
            ]
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as error:
                    progress.console.print(
                        f"[bold red]Error extracting picture: {error}[/bold red]",
                    )
                progress.advance(task_id)


def log_available_pictures(segments: list[RecordingSegment]) -> None:
    for i, seg in enumerate(segments):
        start = seg.start_dt.strftime("%Y-%m-%d %H:%M:%S")
        print(
            f"{i:>4}  {seg.source_file_name}  {start}  "
            f"({seg.raw.start_offset:09d} – {seg.raw.end_offset:09d})"
        )
