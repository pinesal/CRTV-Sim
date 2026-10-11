"""Create a resumable, checksummed raw image of a Windows volume.

This is an emergency recovery helper.  It only opens the source volume for
reading, writes to ``<destination>.partial``, and promotes that file after the
expected byte count has been copied successfully.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import sys
import time


CHUNK_SIZE = 8 * 1024 * 1024
FLUSH_INTERVAL = 1024 * 1024 * 1024


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", help=r"Raw Windows volume, for example \\.\D:")
    parser.add_argument("destination", type=Path)
    parser.add_argument("size", type=int, help="Exact number of source bytes")
    return parser.parse_args()


def _format_bytes(value: float) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


def main() -> int:
    args = _arguments()
    if args.size <= 0:
        raise ValueError("size must be positive")

    destination = args.destination.resolve()
    partial = destination.with_name(destination.name + ".partial")
    metadata = destination.with_name(destination.name + ".json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"Completed image already exists: {destination}")

    offset = partial.stat().st_size if partial.exists() else 0
    if offset > args.size:
        raise RuntimeError("Partial image is larger than the source volume")
    if offset % 512:
        raise RuntimeError("Partial image does not end on a sector boundary")

    digest = hashlib.sha256()
    if offset:
        print(f"Resuming at {_format_bytes(float(offset))}; hashing existing data...")
        with partial.open("rb") as existing:
            while chunk := existing.read(CHUNK_SIZE):
                digest.update(chunk)

    mode = "r+b" if partial.exists() else "x+b"
    started = time.monotonic()
    last_report = started
    last_flush = offset
    copied_this_run = 0

    print(f"Source: {args.source}")
    print(f"Partial image: {partial}")
    print(f"Expected size: {args.size} bytes")
    sys.stdout.flush()

    with open(args.source, "rb", buffering=0) as source, partial.open(mode) as output:
        source.seek(offset)
        output.seek(offset)
        position = offset
        while position < args.size:
            wanted = min(CHUNK_SIZE, args.size - position)
            chunk = source.read(wanted)
            if not chunk:
                raise OSError(
                    f"Unexpected end of source at byte {position}; expected {args.size}"
                )
            output.write(chunk)
            digest.update(chunk)
            position += len(chunk)
            copied_this_run += len(chunk)

            now = time.monotonic()
            if position - last_flush >= FLUSH_INTERVAL:
                output.flush()
                os.fsync(output.fileno())
                last_flush = position
            if now - last_report >= 15:
                elapsed = max(now - started, 0.001)
                rate = copied_this_run / elapsed
                remaining = args.size - position
                eta = remaining / rate if rate else 0
                print(
                    f"{position / args.size:6.2%}  "
                    f"{_format_bytes(float(position))} / {_format_bytes(float(args.size))}  "
                    f"{_format_bytes(rate)}/s  ETA {eta / 60:.1f} min"
                )
                sys.stdout.flush()
                last_report = now

        output.flush()
        os.fsync(output.fileno())

    if partial.stat().st_size != args.size:
        raise RuntimeError("Completed image size does not match the source volume")

    os.replace(partial, destination)
    record = {
        "source": args.source,
        "destination": str(destination),
        "bytes": args.size,
        "sha256": digest.hexdigest(),
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    metadata.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(record, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
