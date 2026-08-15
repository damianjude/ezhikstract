import os
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import IO

import imageio_ffmpeg

from .parser import (
    MAX_SEGMENTS_PER_SOURCE_FILE,
    IndexHeader,
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


MIN_VALID_TIMESTAMP: int = 1577836800  # 2020-01-01 00:00:00 UTC


def _find_mpeg_ps_start_offset(path: Path, offset: int) -> int | None:
    """
    Peek at up to 2KB at offset and return the exact stream start offset of the MPEG Program Stream
    Start Code (0x000001), or None if invalid or stale.
    """
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            buffer = fh.read(2048)
    except (OSError, ValueError):
        return None

    if len(buffer) < 4:
        return None

    pos = buffer.find(b"\x00\x00\x01")
    if pos != -1:
        return offset + pos
    return None


def _is_valid_mpeg_ps(path: Path, offset: int) -> bool:
    """Check if an MPEG Program Stream Start Code (0x000001) exists within 2KB of offset."""
    return _find_mpeg_ps_start_offset(path, offset) is not None


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

    for flat_idx, seg in enumerate(raw_segments):
        if seg.end_time_raw == 0:
            continue

        # Filter out corrupted records with inverted offsets, invalid timestamps (< 2020), or start > end
        if (
            seg.start_offset >= seg.end_offset
            or (seg.start_time_raw & _DATE_MASK) < MIN_VALID_TIMESTAMP
            or (seg.start_time_raw & _DATE_MASK) > (seg.end_time_raw & _DATE_MASK)
        ):
            skipped += 1
            continue

        seg_ts = seg.start_time_raw & _DATE_MASK
        seg_dt = datetime.fromtimestamp(seg_ts, tz=timezone.utc)
        logical_file_index = flat_idx // MAX_SEGMENTS_PER_SOURCE_FILE
        source_file_segment_index = flat_idx % MAX_SEGMENTS_PER_SOURCE_FILE

        candidates: list[Path] = []

        # 1. Match against binary FileRecords in index00.bin header
        for rec in header.file_records:
            if rec.start_time_raw > 0 and rec.end_time_raw > 0:
                rec_s = rec.start_time_raw & _DATE_MASK
                rec_e = rec.end_time_raw & _DATE_MASK
                if (
                    rec_s >= MIN_VALID_TIMESTAMP
                    and rec_e >= rec_s
                    and (rec_s - 600) <= seg_ts <= (rec_e + 600)
                ):
                    cand = camera_dir / f"hiv{rec.file_index:05d}.mp4"
                    if cand.exists() and cand not in candidates:
                        candidates.append(cand)

        # 2. Check active container file (last_file_no)
        if header.last_file_no >= 0:
            act_cand = camera_dir / f"hiv{header.last_file_no:05d}.mp4"
            if act_cand.exists() and act_cand not in candidates:
                candidates.append(act_cand)

        # 3. Fallback to logical file path and modulo path
        log_path = camera_dir / f"hiv{logical_file_index:05d}.mp4"
        if log_path.exists() and log_path not in candidates:
            candidates.append(log_path)

        if header.av_files > 0:
            mod_path = camera_dir / f"hiv{logical_file_index % header.av_files:05d}.mp4"
            if mod_path.exists() and mod_path not in candidates:
                candidates.append(mod_path)

        resolved_source_name: str | None = None
        resolved_aligned_start: int | None = None

        for cand_path in candidates:
            try:
                if seg.end_offset > cand_path.stat().st_size:
                    continue
            except OSError:
                continue

            aligned_start = _find_mpeg_ps_start_offset(cand_path, seg.start_offset)
            if aligned_start is not None:
                resolved_source_name = cand_path.name
                resolved_aligned_start = aligned_start
                break

        if resolved_source_name is None or resolved_aligned_start is None:
            skipped += 1
            continue

        seg.start_offset = resolved_aligned_start

        # Apply date mask to extract lower 32-bit Unix epoch timestamp in UTC time
        segments.append(
            RecordingSegment(
                raw=seg,
                start_dt=seg_dt,
                end_dt=datetime.fromtimestamp(
                    seg.end_time_raw & _DATE_MASK, tz=timezone.utc
                ),
                source_file_index=logical_file_index,
                source_file_segment_index=source_file_segment_index,
                source_file_name=resolved_source_name,
            )
        )

    segments.sort(key=lambda s: s.start_dt)  # sort by datetime

    summary = f"Found {len(segments)} recordings"
    if skipped:
        summary += f", skipped {skipped} invalid"
    print(summary)

    return header, segments


def _feed_ffmpeg_stdin(
    stdin: IO[bytes] | None,
    source_path: Path,
    start_offset: int,
    length: int,
) -> None:
    """Stream segment data from disk into FFmpeg stdin pipe in 64KB chunks."""
    if stdin is None:
        return
    chunk_size = 64 * 1024
    remaining = length
    try:
        with open(source_path, "rb") as fh:
            fh.seek(start_offset)
            while remaining > 0:
                to_read = min(remaining, chunk_size)
                chunk = fh.read(to_read)
                if not chunk:
                    break
                stdin.write(chunk)
                remaining -= len(chunk)
    except (OSError, ValueError):
        pass
    finally:
        try:
            stdin.close()
        except (OSError, ValueError):
            pass


def _run_ffmpeg_extract(
    segment: RecordingSegment,
    camera_dir: Path,
    mp4_file: Path,
    *,
    input_format: str | None = "mpeg",
    include_audio: bool = True,
) -> tuple[bool, str]:
    cmd = [
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-loglevel",
        "error",
        "-err_detect",
        "ignore_err",
    ]
    if input_format:
        cmd.extend(["-f", input_format])

    cmd.extend(
        [
            "-i",
            "pipe:0",
            "-c:v",
            "copy",
            "-tag:v",
            "hvc1",
        ]
    )
    if include_audio:
        cmd.extend(
            [
                "-c:a",
                "libopus",
                "-b:a",
                "64k",
            ]
        )
    else:
        cmd.extend(["-an"])
    cmd.extend(["-y", str(mp4_file)])

    source_path = camera_dir / segment.source_file_name
    segment_len = segment.raw.end_offset - segment.raw.start_offset

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        writer_thread = threading.Thread(
            target=_feed_ffmpeg_stdin,
            args=(proc.stdin, source_path, segment.raw.start_offset, segment_len),
            daemon=True,
        )
        writer_thread.start()

        _, stderr_bytes = proc.communicate(timeout=30)
        writer_thread.join(timeout=5)
        return_code = proc.returncode
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            _, stderr_bytes = proc.communicate(timeout=5)
        except (subprocess.SubprocessError, OSError):
            stderr_bytes = b"FFmpeg process timed out"
        return_code = -1
    except (subprocess.SubprocessError, OSError) as error:
        return False, str(error)

    stderr_text = (stderr_bytes or b"").decode("utf-8", errors="replace").strip()
    is_ok = return_code == 0 and mp4_file.exists() and mp4_file.stat().st_size > 0
    return is_ok, stderr_text


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
      - Audio: re-encoded to Opus at 64 kbps, or fallback to video-only if audio is missing/invalid.

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

    try:
        # Attempt 1: MPEG-PS container with audio
        success, stderr1 = _run_ffmpeg_extract(
            segment, camera_dir, mp4_file, input_format="mpeg", include_audio=True
        )

        # Attempt 2: MPEG-PS container video-only (-an)
        if not success:
            try:
                mp4_file.unlink(missing_ok=True)
            except OSError:
                pass
            success, _stderr2 = _run_ffmpeg_extract(
                segment, camera_dir, mp4_file, input_format="mpeg", include_audio=False
            )

        # Attempt 3: Raw HEVC elementary stream (-f hevc)
        if not success:
            try:
                mp4_file.unlink(missing_ok=True)
            except OSError:
                pass
            success, stderr3 = _run_ffmpeg_extract(
                segment, camera_dir, mp4_file, input_format="hevc", include_audio=False
            )

        if not success:
            print(
                f"Warning: Failed to extract segment {segment.start_dt}.\n"
                f"ffmpeg stderr: {stderr3 or stderr1}",
                file=sys.stderr,
            )
            try:
                mp4_file.unlink(missing_ok=True)
            except OSError:
                pass
            return None

        return mp4_file
    except Exception as error:  # noqa: BLE001
        print(f"Failed to extract segment {segment.start_dt}: {error}", file=sys.stderr)
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
    from rich.progress import BarColumn, Progress, TextColumn, TimeRemainingColumn

    from .merger import merge_day  # local import avoids circular dependency

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
                        except Exception as error:  # noqa: BLE001
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

    for source_file_index, seg in raw_segments:
        # Filter out corrupted records with inverted offsets, invalid timestamps (< 2020), or start > end
        if (
            seg.start_offset >= seg.end_offset
            or (seg.start_time_raw & _DATE_MASK) < MIN_VALID_TIMESTAMP
            or (seg.start_time_raw & _DATE_MASK) > (seg.end_time_raw & _DATE_MASK)
        ):
            skipped += 1
            continue

        seg_ts = seg.start_time_raw & _DATE_MASK
        seg_dt = datetime.fromtimestamp(seg_ts, tz=timezone.utc)

        candidates: list[Path] = []

        # 1. Match against binary FileRecords in index00p.bin header
        for rec in header.file_records:
            if rec.start_time_raw > 0 and rec.end_time_raw > 0:
                rec_s = rec.start_time_raw & _DATE_MASK
                rec_e = rec.end_time_raw & _DATE_MASK
                if (
                    rec_s >= MIN_VALID_TIMESTAMP
                    and rec_e >= rec_s
                    and (rec_s - 600) <= seg_ts <= (rec_e + 600)
                ):
                    cand = camera_dir / f"hiv{rec.file_index:05d}.pic"
                    if cand.exists() and cand not in candidates:
                        candidates.append(cand)

        # 2. Check active container file (last_file_no)
        if header.last_file_no >= 0:
            act_cand = camera_dir / f"hiv{header.last_file_no:05d}.pic"
            if act_cand.exists() and act_cand not in candidates:
                candidates.append(act_cand)

        # 3. Fallback to logical file path and modulo path
        log_path = camera_dir / f"hiv{source_file_index:05d}.pic"
        if log_path.exists() and log_path not in candidates:
            candidates.append(log_path)

        if header.av_files > 0:
            mod_path = camera_dir / f"hiv{source_file_index % header.av_files:05d}.pic"
            if mod_path.exists() and mod_path not in candidates:
                candidates.append(mod_path)

        resolved_source_name: str | None = None

        for cand_path in candidates:
            try:
                if seg.end_offset > cand_path.stat().st_size:
                    continue
            except (OSError, ValueError):
                continue

            try:
                with open(cand_path, "rb") as fh:
                    fh.seek(seg.start_offset)
                    header_bytes = fh.read(16)
                    if b"\xff\xd8" in header_bytes:
                        resolved_source_name = cand_path.name
                        break
            except (OSError, ValueError):
                continue

        if resolved_source_name is None:
            skipped += 1
            continue

        segments.append(
            RecordingSegment(
                raw=seg,
                start_dt=seg_dt,
                end_dt=datetime.fromtimestamp(
                    seg.end_time_raw & _DATE_MASK, tz=timezone.utc
                ),
                source_file_index=source_file_index,
                source_file_segment_index=0,
                source_file_name=resolved_source_name,
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

        # Trim leading alignment padding up to JPEG SOI marker (0xFFD8)
        soi_idx = raw.find(b"\xff\xd8")
        if soi_idx != -1 and soi_idx > 0:
            raw = raw[soi_idx:]

        # Trim trailing alignment zero-padding past JPEG EOI marker (0xFFD9)
        eoi_idx = raw.rfind(b"\xff\xd9")
        if eoi_idx != -1:
            raw = raw[: eoi_idx + 2]

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
    from rich.progress import BarColumn, Progress, TextColumn, TimeRemainingColumn

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
                except Exception as error:  # noqa: BLE001
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
