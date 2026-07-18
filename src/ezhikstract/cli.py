from pathlib import Path
from typing import Annotated, Optional

import typer

app = typer.Typer(
    name="ezhikstract",
    help=(
        "Extract playable video from EZVIZ / Hikvision SD cards' proprietary round-robin storage format. Works specifically for devices that use hiv<xxxxx>.mp4 and index00.bin files."
    ),
    no_args_is_help=True,
)


list_cmd = typer.Typer(help="List available data.")
app.add_typer(list_cmd, name="list", no_args_is_help=True)

extract_cmd = typer.Typer(help="Extract data.")
app.add_typer(extract_cmd, name="extract", no_args_is_help=True)


@list_cmd.command("videos")
def list_videos(
    input_dir: Annotated[
        Path,
        typer.Argument(
            help="Root directory of the SD card (contains index00.bin).",
            exists=True,
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
    ],
) -> None:
    """List all valid video segments found on the SD card."""
    from .extractor import log_available_recordings, process_segments

    # Parse index00.bin and print all valid video segments
    try:
        _, segments = process_segments(input_dir)
        log_available_recordings(segments)
    except (FileNotFoundError, OSError, ValueError) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=1)


@list_cmd.command("pictures")
def list_pictures(
    input_dir: Annotated[
        Path,
        typer.Argument(
            help="Root directory of the SD card (contains index00p.bin).",
            exists=True,
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
    ],
) -> None:
    """List all valid pictures/thumbnails found on the SD card."""
    from .extractor import log_available_pictures, process_picture_segments

    # Parse index00p.bin and print all valid picture segments
    try:
        _, segments = process_picture_segments(input_dir)
        log_available_pictures(segments)
    except (FileNotFoundError, OSError, ValueError) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=1)


@extract_cmd.command("videos")
def extract_videos(
    input_dir: Annotated[
        Path,
        typer.Argument(
            help="Root directory of the SD card (contains index00.bin).",
            exists=True,
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
    ],
    output_dir: Annotated[
        Path,
        typer.Option(
            "--output",
            "-o",
            help='Output directory for extracted and merged .mp4 files. Default: "./recordings".',
        ),
    ] = Path("./recordings"),
    from_time: Annotated[
        Optional[str],
        typer.Option(
            "--from",
            help='Inclusive start filter, UTC (format: "YYYY-MM-DD HH:MM:SS").',
            metavar="DATETIME",
        ),
    ] = None,
    to_time: Annotated[
        Optional[str],
        typer.Option(
            "--to",
            help='Exclusive end filter, UTC (format: "YYYY-MM-DD HH:MM:SS").',
            metavar="DATETIME",
        ),
    ] = None,
    replace: Annotated[
        bool,
        typer.Option(
            "--replace/--no-replace",
            help="Overwrite existing output files.",
        ),
    ] = True,
) -> None:
    """
    Extract video recording segments from an EZVIZ / Hikvision SD card to .mp4 files,
    merging each day's segments into a single file named by start time (DDMMYYYY HHMMSS.mp4).
    """
    from .extractor import extract_all_segments, process_segments

    # Extract all matching video segments and merge them daily
    try:
        output_dir = output_dir.expanduser().resolve()
        _, segments = process_segments(input_dir)
        extract_all_segments(
            segments,
            input_dir,
            from_time=from_time,
            to_time=to_time,
            output_dir=output_dir,
            replace=replace,
        )
    except (FileNotFoundError, OSError, ValueError) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=1)


@extract_cmd.command("pictures")
def extract_pictures(
    input_dir: Annotated[
        Path,
        typer.Argument(
            help="Root directory of the SD card (contains index00p.bin).",
            exists=True,
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
    ],
    output_dir: Annotated[
        Path,
        typer.Option(
            "--output",
            "-o",
            help='Output directory for extracted pictures. Default: "./pictures".',
        ),
    ] = Path("./pictures"),
    from_time: Annotated[
        Optional[str],
        typer.Option(
            "--from",
            help='Inclusive start filter, UTC (format: "YYYY-MM-DD HH:MM:SS").',
            metavar="DATETIME",
        ),
    ] = None,
    to_time: Annotated[
        Optional[str],
        typer.Option(
            "--to",
            help='Exclusive end filter, UTC (format: "YYYY-MM-DD HH:MM:SS").',
            metavar="DATETIME",
        ),
    ] = None,
    replace: Annotated[
        bool,
        typer.Option(
            "--replace/--no-replace",
            help="Overwrite existing output files.",
        ),
    ] = True,
) -> None:
    """
    Extract picture segments from an EZVIZ / Hikvision SD card to .jpg files.
    """
    from .extractor import extract_all_pictures, process_picture_segments

    # Extract picture segments and write them as JPEG files
    try:
        output_dir = output_dir.expanduser().resolve()
        _, segments = process_picture_segments(input_dir)
        extract_all_pictures(
            segments,
            input_dir,
            from_time=from_time,
            to_time=to_time,
            output_dir=output_dir,
            replace=replace,
        )
    except (FileNotFoundError, OSError, ValueError) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=1)


def main() -> None:
    app()
