from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path

from retrotv.announcements import UpNextRenderer
from retrotv.branding import portable_executable
from retrotv.audio_normalization import AudioNormalizationService
from retrotv.ai_announcements import (
    AnnouncementProfileService,
    OpenAIHTTPClient,
    OpenAITTSProvider,
)
from retrotv.breakpoints import BreakpointAnalyzer
from retrotv.cleanup import GeneratedAssetCleanup
from retrotv.config import ConfigError, load_config
from retrotv.db import apply_migrations, connect_database
from retrotv.guide import GuideRenderer, GuideService
from retrotv.graphics_lab import serve_graphics_lab
from retrotv.normalization import Crop, MODES, NormalizationService
from retrotv.promotion import PromotionService
from retrotv.playback import (
    PlaybackController,
    PlaybackError,
    ScheduleResolver,
    create_player,
    start_windows_keyboard,
)
from retrotv.production_guard import (
    confirm_development_access,
    production_conflicts,
)
from retrotv.restore_point import RestorePointService
from retrotv.scanner import FFmpegInspector, LibraryScanner
from retrotv.scheduling import ScheduleBuilder
from retrotv.synthetic import SyntheticChannelReader
from retrotv.runtime import ApplicationRuntime
from retrotv.web import serve


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crtvsim",
        description="CRTV Sim media library and normalization tools",
    )
    parser.add_argument(
        "--config",
        help="Path to an external TOML profile",
    )
    parser.add_argument(
        "--allow-production-storage",
        action="store_true",
        help="Allow this development process to use a production-marked drive",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-db", help="Create or migrate the SQLite database")
    subparsers.add_parser(
        "create-internet-restore-point",
        help="Create the one-time pre-internet-media database and settings checkpoint",
    )
    rollback = subparsers.add_parser(
        "rollback-internet-media",
        help="Restore the pre-internet-media database, settings, and previous build",
    )
    rollback.add_argument("--no-launch", action="store_true")

    scan = subparsers.add_parser("scan", help="Scan and validate a media root")
    scan.add_argument("--root", default="media", help="Logical storage root")
    scan.add_argument("--limit", type=int)
    scan.add_argument("--force", action="store_true")
    scan.add_argument("--skip-samples", action="store_true")
    scan.add_argument("--fingerprint", choices=("quick", "full"))
    scan.add_argument(
        "--progress-every",
        type=int,
        default=10,
        help="Print progress every N files; zero disables progress",
    )

    subparsers.add_parser("stats", help="Show indexed library totals")

    weather_status = subparsers.add_parser(
        "weather-status",
        help="Validate the published RetroWeather channel without indexing it",
    )
    weather_status.add_argument(
        "--root",
        type=Path,
        help=(
            "Weather publication root (defaults to "
            "RetroWeather/synthetic-channels/weather)"
        ),
    )

    review = subparsers.add_parser("review", help="List open review items")
    review.add_argument("--limit", type=int, default=100)
    review.add_argument("--json", action="store_true")

    analyze = subparsers.add_parser(
        "analyze-normalization",
        help="Analyze indexed video geometry without changing source media",
    )
    analyze.add_argument("--asset", type=int, action="append")
    analyze.add_argument("--limit", type=int)
    analyze.add_argument("--force", action="store_true")
    analyze.add_argument(
        "--reuse-samples",
        action="store_true",
        help="Reapply current rules to stored crop samples without FFmpeg decoding",
    )

    breaks = subparsers.add_parser(
        "analyze-breakpoints",
        help="Detect natural mid-program breakpoints without changing media",
    )
    breaks.add_argument("--asset", type=int, action="append")
    breaks.add_argument("--limit", type=int)
    breaks.add_argument("--force", action="store_true")
    breaks.add_argument(
        "--progress-every", type=int, default=1,
        help="Print progress every N assets; zero disables progress",
    )

    set_break = subparsers.add_parser(
        "set-breakpoint", help="Set a manual act-break timestamp for one episode"
    )
    set_break.add_argument("asset", type=int)
    set_break.add_argument("timestamp", help="HH:MM:SS, MM:SS, or seconds")
    analyze.add_argument(
        "--progress-every", type=int, default=1,
        help="Print progress every N assets; zero disables progress",
    )

    override = subparsers.add_parser(
        "set-normalization",
        help="Set a manual normalization mode for one asset",
    )
    override.add_argument("asset", type=int)
    override.add_argument("mode", choices=sorted(MODES))
    override.add_argument(
        "--crop",
        help="Custom crop as width:height:x:y (required for custom mode)",
    )

    preview = subparsers.add_parser(
        "preview-normalization",
        help="Create original and corrected preview frames",
    )
    preview.add_argument("asset", type=int)

    normalize = subparsers.add_parser(
        "normalize",
        help="Deprecated legacy command; use replace-with-backup",
    )
    normalize.add_argument("--asset", type=int, action="append")
    normalize.add_argument("--limit", type=int)
    normalize.add_argument(
        "--progress-every", type=int, default=1,
        help="Print progress every N assets; zero disables progress",
    )

    analyze_audio = subparsers.add_parser(
        "analyze-audio",
        help="Measure integrated loudness without changing media files",
    )
    analyze_audio.add_argument("--asset", type=int, action="append")
    analyze_audio.add_argument("--limit", type=int)
    analyze_audio.add_argument("--force", action="store_true")
    analyze_audio.add_argument("--progress-every", type=int, default=1)

    normalize_audio = subparsers.add_parser(
        "normalize-audio",
        help="Normalize approved audio while stream-copying the video",
    )
    normalize_audio.add_argument("--asset", type=int, action="append")
    normalize_audio.add_argument("--limit", type=int)
    normalize_audio.add_argument(
        "--confirm-replace",
        action="store_true",
        help="Required acknowledgement that working audio will be replaced",
    )

    replace = subparsers.add_parser(
        "replace-with-backup",
        help="Validate correction, archive original, and replace the working copy",
    )
    replace.add_argument("asset", type=int)
    replace.add_argument(
        "--confirm-replace",
        action="store_true",
        help="Required acknowledgement that the working media file will be replaced",
    )

    generate = subparsers.add_parser(
        "generate-schedule",
        help="Generate and activate persistent daily channel timelines",
    )
    generate.add_argument("--channel", default="fox")
    generate.add_argument("--date", help="Local schedule date (YYYY-MM-DD)")
    generate.add_argument("--days", type=int, default=1)

    show = subparsers.add_parser(
        "show-schedule",
        help="Show the active timeline for a channel and date",
    )
    show.add_argument("--channel", default="fox")
    show.add_argument("--date", help="Local schedule date (YYYY-MM-DD)")
    show.add_argument("--programs-only", action="store_true")
    show.add_argument("--json", action="store_true")

    validate = subparsers.add_parser(
        "validate-schedule",
        help="Revalidate one persisted schedule generation",
    )
    validate.add_argument("generation", type=int)

    play = subparsers.add_parser(
        "play-channel",
        help="Tune a scheduled or synthetic channel and join at wall-clock time",
    )
    play.add_argument("--channel", default="fox")
    play.add_argument("--backend", choices=("auto", "mpv", "ffplay"))
    play.add_argument("--dry-run", action="store_true")
    play.add_argument(
        "--at",
        help="Resolve a specific ISO timestamp for dry-run testing",
    )
    play.add_argument("--no-keyboard", action="store_true")

    show_guide = subparsers.add_parser(
        "show-guide",
        help="Show the resolved Now/Next guide projection",
    )
    show_guide.add_argument("--at", help="Optional local ISO timestamp")
    show_guide.add_argument("--json", action="store_true")

    render_guide = subparsers.add_parser(
        "render-guide",
        help="Render and validate the looping 640x480 Guide channel video",
    )
    render_guide.add_argument("--at", help="Optional local ISO timestamp")
    render_guide.add_argument("--output", type=Path)
    render_guide.add_argument(
        "--loop-seconds",
        "--page-seconds",
        dest="page_seconds",
        type=float,
        help="Override the duration of one seamless scrolling loop",
    )
    render_guide.add_argument(
        "--if-needed",
        action="store_true",
        help="Skip rendering while the current validated guide window is still fresh",
    )

    up_next = subparsers.add_parser(
        "render-up-next",
        help="Render a deterministic schedule-based coming-up-next screen",
    )
    up_next.add_argument("--channel", default="nickelodeon")
    up_next.add_argument("--at", help="Optional local ISO timestamp")
    up_next.add_argument("--listings", type=int)
    up_next.add_argument("--output", type=Path)
    up_next.add_argument(
        "--no-tts",
        action="store_true",
        help="Render silent audio instead of using an available local TTS voice",
    )
    up_next.add_argument("--force", action="store_true")

    generic_announcement = subparsers.add_parser(
        "generate-generic-announcement",
        help="Generate or replace one channel's reusable OpenAI TTS fallback",
    )
    generic_announcement.add_argument("--channel", required=True)

    cleanup = subparsers.add_parser(
        "cleanup-generated",
        help="Prune expired generated Guide, announcement, and graphics files",
    )
    cleanup.add_argument(
        "--if-due",
        action="store_true",
        help="Honor the configured cleanup interval instead of running immediately",
    )

    web = subparsers.add_parser("serve", help="Run the local library interface")
    web.add_argument("--host")
    web.add_argument("--port", type=int)

    graphics_lab = subparsers.add_parser(
        "graphics-lab",
        help="Run the source-only AI channel-graphics experiment lab",
    )
    graphics_lab.add_argument("--host", default="127.0.0.1")
    graphics_lab.add_argument("--port", type=int, default=8786)

    run = subparsers.add_parser(
        "run",
        help="Run the Windows application dashboard, automation, and TV controller",
    )
    run.add_argument("--host")
    run.add_argument("--port", type=int)
    run.add_argument("--no-browser", action="store_true")
    run.add_argument("--start-tv", action="store_true")
    return parser


def _prepare(config_path: str | None, *, allow_production_storage: bool = False):
    config = load_config(config_path)
    conflicts = production_conflicts(config)
    if conflicts and not allow_production_storage and not confirm_development_access(conflicts):
        raise ConfigError(
            "Development startup cancelled because configured storage is marked as production. "
            "Use an unmarked test drive or --allow-production-storage for this process."
        )
    connection = connect_database(config.database_path)
    restore = RestorePointService(config)
    if restore.migration_pending(connection):
        restore.create(connection)
    migrations = apply_migrations(
        connection,
        config.project_root / "migrations",
    )
    return config, connection, migrations


def _timestamp_ms(value: str) -> int:
    parts = value.strip().split(":")
    try:
        if len(parts) == 1:
            seconds = float(parts[0])
        elif len(parts) == 2:
            seconds = int(parts[0]) * 60 + float(parts[1])
        elif len(parts) == 3:
            seconds = int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        else:
            raise ValueError
    except ValueError as exc:
        raise ValueError("expected HH:MM:SS, MM:SS, or seconds") from exc
    if seconds <= 0:
        raise ValueError("timestamp must be positive")
    return round(seconds * 1000)


def _stats(connection) -> dict[str, int]:
    row = connection.execute(
        """
        SELECT
          COUNT(*) AS assets,
          SUM(review_status = 'ready') AS ready,
          SUM(review_status = 'open') AS needs_review,
          SUM(review_status = 'ignored') AS ignored,
          SUM(enabled_for_scheduling = 1) AS enabled
        FROM assets
        """
    ).fetchone()
    return {key: int(row[key] or 0) for key in row.keys()}


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "weather-status":
        try:
            config = load_config(args.config)
            root = args.root or (
                config.project_root
                / "RetroWeather"
                / "synthetic-channels"
                / "weather"
            )
            status = SyntheticChannelReader(root, expected_slug="weather").refresh()
        except (ConfigError, OSError, ValueError) as exc:
            print(f"Configuration error: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(status.as_dict(), indent=2))
        return 0 if status.ready else 1

    if args.command in {
        "create-internet-restore-point", "rollback-internet-media"
    }:
        try:
            config = load_config(args.config)
            conflicts = production_conflicts(config)
            if (
                conflicts
                and not args.allow_production_storage
                and not confirm_development_access(conflicts)
            ):
                raise ConfigError(
                    "Development startup cancelled because configured storage is marked as production. "
                    "Use an unmarked test drive or --allow-production-storage for this process."
                )
            service = RestorePointService(config)
            if args.command == "create-internet-restore-point":
                connection = connect_database(config.database_path)
                try:
                    if not service.migration_pending(connection):
                        raise ValueError(
                            "Internet-media migration is already installed; use the existing restore point"
                        )
                    result = service.create(connection)
                finally:
                    connection.close()
                print(json.dumps(result, indent=2))
                return 0
            result = service.restore()
            print(json.dumps(result.as_dict(), indent=2))
            if result.previous_build and not args.no_launch:
                executable = portable_executable(Path(result.previous_build))
                if executable is None:
                    raise ValueError("The previous build's launcher could not be found")
                subprocess.Popen([str(executable)], cwd=str(executable.parent))
                restored_config = load_config(args.config)
                web_settings = restored_config.data.get("web", {})
                health_host = str(web_settings.get("host", "127.0.0.1"))
                if health_host in {"0.0.0.0", "::"}:
                    health_host = "127.0.0.1"
                health_url = (
                    f"http://{health_host}:{int(web_settings.get('port', 8765))}/api/status"
                )
                deadline = time.monotonic() + 30
                healthy = False
                while time.monotonic() < deadline:
                    try:
                        with urllib.request.urlopen(health_url, timeout=2) as response:
                            healthy = response.status == 200
                        if healthy:
                            break
                    except (OSError, urllib.error.URLError):
                        time.sleep(0.5)
                if not healthy:
                    raise RuntimeError(
                        "The previous build launched, but its runtime health check did not pass"
                    )
            return 0
        except (ConfigError, OSError, ValueError, RuntimeError) as exc:
            print(f"Rollback error: {exc}", file=sys.stderr)
            return 2

    if args.command == "graphics-lab":
        try:
            config = load_config(args.config)
            serve_graphics_lab(config, host=args.host, port=args.port)
            return 0
        except (ConfigError, OSError, ValueError, RuntimeError) as exc:
            print(f"Graphics Lab error: {exc}", file=sys.stderr)
            return 2

    try:
        config, connection, migrations = _prepare(
            args.config,
            allow_production_storage=bool(args.allow_production_storage),
        )
    except (ConfigError, OSError, ValueError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    try:
        if args.command == "init-db":
            print(f"Database: {config.database_path}")
            print(
                "Applied migrations: "
                + (", ".join(migrations) if migrations else "none; already current")
            )
            return 0

        if args.command == "scan":
            inspector = FFmpegInspector(config)
            if args.fingerprint:
                inspector.fingerprint_mode = args.fingerprint
            scanner = LibraryScanner(connection, config, inspector=inspector)

            def progress(count: int, path: Path, outcome: str) -> None:
                if args.progress_every and count % args.progress_every == 0:
                    print(f"[{count}] {outcome}: {path}")

            summary = scanner.scan(
                logical_root=args.root,
                limit=args.limit,
                force=args.force,
                skip_samples=args.skip_samples,
                progress=progress,
            )
            print(
                json.dumps(
                    {
                        "scan_run_id": summary.scan_run_id,
                        "discovered": summary.discovered,
                        "ready": summary.ready,
                        "unchanged": summary.unchanged,
                        "review": summary.review,
                        "errors": summary.errors,
                        "removed": summary.missing,
                        "channels_created": summary.channels_created,
                        "channels_enabled": summary.channels_enabled,
                        "channels_disabled": summary.channels_disabled,
                    },
                    indent=2,
                )
            )
            return 0 if summary.errors == 0 else 1

        if args.command == "stats":
            print(json.dumps(_stats(connection), indent=2))
            return 0

        if args.command == "cleanup-generated":
            summary = GeneratedAssetCleanup(connection, config).run(
                force=not args.if_due
            )
            print(json.dumps(summary.as_dict(), indent=2))
            return 1 if summary.errors else 0

        if args.command == "review":
            rows = connection.execute(
                """
                SELECT r.id, r.asset_id, r.reason_code, r.detail, r.created_at,
                       a.source_relative_path, a.display_title
                FROM review_items r
                JOIN assets a ON a.id = r.asset_id
                WHERE r.status = 'open'
                ORDER BY r.created_at DESC
                LIMIT ?
                """,
                (max(1, args.limit),),
            ).fetchall()
            values = [dict(row) for row in rows]
            if args.json:
                print(json.dumps(values, indent=2, ensure_ascii=False))
            elif not values:
                print("No open review items.")
            else:
                for item in values:
                    print(
                        f"#{item['id']} asset={item['asset_id']} "
                        f"{item['reason_code']}: {item['source_relative_path']}"
                    )
                    print(f"  {item['detail']}")
            return 0

        if args.command == "analyze-breakpoints":
            service = BreakpointAnalyzer(connection, config)
            if args.asset:
                asset_ids = list(dict.fromkeys(args.asset))
            elif args.force:
                values: list[object] = [service.minimum_duration_ms / 1000]
                limit_sql = ""
                if args.limit is not None:
                    limit_sql = " LIMIT ?"
                    values.append(max(0, args.limit))
                rows = connection.execute(
                    f"""
                    SELECT a.id FROM assets a
                    JOIN asset_media_info m ON m.asset_id=a.id
                    WHERE a.media_type='episode' AND a.enabled_for_scheduling=1
                      AND a.review_status='ready' AND a.metadata_status='ready'
                      AND m.duration_seconds > ? ORDER BY a.id {limit_sql}
                    """,
                    values,
                ).fetchall()
                asset_ids = [int(row["id"]) for row in rows]
            else:
                asset_ids = [
                    int(row["id"]) for row in service.pending_assets(limit=args.limit)
                ]
            totals = {"analyzed": 0, "midpoint": 0, "natural": 0, "error": 0}
            for index, asset_id in enumerate(asset_ids, 1):
                try:
                    result = service.analyze_asset(asset_id)
                    totals["analyzed"] += 1
                    totals["midpoint" if result.method == "midpoint" else "natural"] += 1
                    offsets = service.selected_offsets(asset_id)
                    times = ", ".join(f"{value / 1000:.3f}s" for value in offsets)
                    outcome = f"{result.method}; {len(offsets)} break(s) at {times}"
                except Exception as exc:
                    totals["error"] += 1
                    outcome = f"error: {exc}"
                if args.progress_every and index % args.progress_every == 0:
                    row = connection.execute(
                        "SELECT source_relative_path FROM assets WHERE id=?", (asset_id,)
                    ).fetchone()
                    source = config.storage_root("media") / Path(row["source_relative_path"])
                    print(f"[{index}/{len(asset_ids)}] {outcome} (asset {asset_id}): {source}")
            print(json.dumps({"processed": len(asset_ids), **totals}, indent=2))
            return 0 if totals["error"] == 0 else 1

        if args.command == "set-breakpoint":
            try:
                offset = _timestamp_ms(args.timestamp)
                result = BreakpointAnalyzer(connection, config).set_manual(
                    args.asset, offset
                )
            except ValueError as exc:
                print(f"Breakpoint error: {exc}", file=sys.stderr)
                return 2
            print(
                f"Asset {result.asset_id}: manual breakpoint="
                f"{result.offset_ms / 1000:.3f}s"
            )
            return 0

        if args.command == "analyze-normalization":
            where = [
                "a.metadata_status = 'ready'",
                "a.integrity_status = 'ready'",
            ]
            values: list[object] = []
            if args.asset:
                placeholders = ",".join("?" for _ in args.asset)
                where.append(f"a.id IN ({placeholders})")
                values.extend(args.asset)
            if not args.force:
                where.append(
                    "(n.asset_id IS NULL OR n.analysis_status IN ('pending', 'error'))"
                )
            limit_clause = ""
            if args.limit is not None:
                limit_clause = " LIMIT ?"
                values.append(max(0, args.limit))
            rows = connection.execute(
                f"""
                SELECT a.id, a.source_relative_path
                FROM assets a
                LEFT JOIN normalization_profiles n ON n.asset_id = a.id
                WHERE {" AND ".join(where)}
                ORDER BY a.id
                {limit_clause}
                """,
                values,
            ).fetchall()
            service = NormalizationService(connection, config)
            totals = {"analyzed": 0, "review": 0, "error": 0}
            for index, row in enumerate(rows, 1):
                try:
                    if args.reuse_samples:
                        result = service.reanalyze_asset_from_stored_samples(
                            int(row["id"])
                        )
                    else:
                        result = service.analyze_asset(int(row["id"]))
                    totals[result.status] = totals.get(result.status, 0) + 1
                    outcome = f"{result.status}/{result.detected_mode}"
                except Exception as exc:
                    totals["error"] += 1
                    outcome = f"error: {exc}"
                if args.progress_every and index % args.progress_every == 0:
                    source = (
                        config.storage_root("media")
                        / Path(row["source_relative_path"])
                    )
                    print(
                        f"[{index}/{len(rows)}] {outcome} "
                        f"(asset {row['id']}): {source}"
                    )
            print(json.dumps({"processed": len(rows), **totals}, indent=2))
            return 0 if totals["error"] == 0 else 1

        if args.command == "set-normalization":
            crop = None
            if args.crop:
                try:
                    width, height, x, y = (
                        int(value) for value in args.crop.split(":")
                    )
                    crop = Crop(width, height, x, y)
                except (ValueError, TypeError):
                    print(
                        "Invalid crop; expected width:height:x:y",
                        file=sys.stderr,
                    )
                    return 2
            service = NormalizationService(connection, config)
            try:
                service.set_override(args.asset, args.mode, crop=crop)
            except ValueError as exc:
                print(f"Normalization error: {exc}", file=sys.stderr)
                return 2
            print(f"Asset {args.asset}: override={args.mode}")
            return 0

        if args.command == "preview-normalization":
            service = NormalizationService(connection, config)
            try:
                source, normalized = service.create_preview(args.asset)
            except (ValueError, RuntimeError, OSError) as exc:
                print(f"Preview error: {exc}", file=sys.stderr)
                return 1
            print(f"Original:   {source}")
            print(f"Normalized: {normalized}")
            return 0

        if args.command == "normalize":
            print(
                "The normalize command created persistent playback copies and is no "
                "longer supported. Use replace-with-backup for one asset or the "
                "Dashboard's Convert approved media action.",
                file=sys.stderr,
            )
            return 2

        if args.command == "analyze-audio":
            service = AudioNormalizationService(connection, config)
            if args.asset:
                asset_ids = list(dict.fromkeys(args.asset))
            else:
                asset_ids = [
                    int(row["id"])
                    for row in service.pending_assets(
                        limit=args.limit, force=args.force
                    )
                ]
            totals = {
                "analyzed": 0,
                "review": 0,
                "not_applicable": 0,
                "correction_needed": 0,
                "error": 0,
            }
            for index, asset_id in enumerate(asset_ids, 1):
                try:
                    result = service.analyze_asset(asset_id)
                    totals[result.status] = totals.get(result.status, 0) + 1
                    totals["correction_needed"] += int(result.needs_correction)
                    outcome = (
                        f"{result.status}; {result.measured_i:.1f} LUFS"
                        if result.measured_i is not None
                        else result.status
                    )
                except Exception as exc:
                    totals["error"] += 1
                    outcome = f"error: {exc}"
                if args.progress_every and index % args.progress_every == 0:
                    row = connection.execute(
                        "SELECT source_relative_path FROM assets WHERE id=?",
                        (asset_id,),
                    ).fetchone()
                    path = row["source_relative_path"] if row else "missing asset"
                    print(f"[{index}/{len(asset_ids)}] asset={asset_id} {outcome}: {path}")
            print(json.dumps({"processed": len(asset_ids), **totals}, indent=2))
            return 0 if totals["error"] == 0 else 1

        if args.command == "normalize-audio":
            if not args.confirm_replace:
                print(
                    "Refusing audio replacement without --confirm-replace. "
                    "No media was changed.",
                    file=sys.stderr,
                )
                return 2
            service = AudioNormalizationService(connection, config)
            if args.asset:
                asset_ids = list(dict.fromkeys(args.asset))
            else:
                asset_ids = [
                    int(row["id"])
                    for row in service.correction_candidates(limit=args.limit)
                ]
            converted = failed = 0
            for index, asset_id in enumerate(asset_ids, 1):
                try:
                    result = service.normalize_asset(asset_id)
                    converted += 1
                    outcome = f"normalized: {result.source_path}"
                except Exception as exc:
                    failed += 1
                    outcome = f"failed: {exc}"
                print(f"[{index}/{len(asset_ids)}] asset={asset_id} {outcome}")
            print(
                json.dumps(
                    {"processed": len(asset_ids), "normalized": converted, "failed": failed},
                    indent=2,
                )
            )
            return 0 if failed == 0 else 1

        if args.command == "replace-with-backup":
            if not args.confirm_replace:
                print(
                    "Refusing replacement without --confirm-replace. "
                    "No media was changed.",
                    file=sys.stderr,
                )
                return 2
            try:
                result = PromotionService(
                    connection, config
                ).replace_with_backup(args.asset)
            except (ValueError, RuntimeError, OSError) as exc:
                print(f"Replacement failed: {exc}", file=sys.stderr)
                return 1
            print(f"Asset {result.asset_id} corrected: {result.source_path}")
            print(f"Verified original backup: {result.backup_path}")
            print(f"Promotion record: {result.promotion_id}")
            return 0

        if args.command == "generate-schedule":
            scheduler = ScheduleBuilder(connection, config)
            try:
                first_date = (
                    date.fromisoformat(args.date)
                    if args.date
                    else datetime.now(scheduler.timezone).date()
                )
            except ValueError:
                print("Invalid date; expected YYYY-MM-DD", file=sys.stderr)
                return 2
            if args.days < 1 or args.days > 31:
                print("--days must be between 1 and 31", file=sys.stderr)
                return 2
            results = []
            for offset in range(args.days):
                target = first_date + timedelta(days=offset)
                try:
                    result = scheduler.generate_day(args.channel, target)
                except ValueError as exc:
                    print(f"Schedule error: {exc}", file=sys.stderr)
                    return 1
                payload = {
                    "generation_id": result.generation_id,
                    "channel": result.channel_slug,
                    "date": result.schedule_date.isoformat(),
                    "entries": result.entry_count,
                    "programs": result.program_count,
                    "continuity_seconds": round(result.continuity_seconds, 3),
                    "validation": result.validation_status,
                    "detail": result.validation_detail,
                }
                results.append(payload)
                print(
                    f"{payload['date']} {payload['channel']}: "
                    f"generation {payload['generation_id']}, "
                    f"{payload['programs']} programs, "
                    f"{payload['entries']} entries, "
                    f"{payload['continuity_seconds']:.1f}s continuity, "
                    f"{payload['validation']}"
                )
            if len(results) > 1:
                print(json.dumps(results, indent=2))
            return 0 if all(item["validation"] == "valid" for item in results) else 1

        if args.command == "show-schedule":
            scheduler = ScheduleBuilder(connection, config)
            try:
                target = (
                    date.fromisoformat(args.date)
                    if args.date
                    else datetime.now(scheduler.timezone).date()
                )
            except ValueError:
                print("Invalid date; expected YYYY-MM-DD", file=sys.stderr)
                return 2
            generation = scheduler.active_generation(args.channel, target)
            if generation is None:
                print(
                    f"No active {args.channel} schedule for {target.isoformat()}.",
                    file=sys.stderr,
                )
                return 1
            rows = scheduler.entries(
                int(generation["id"]), programs_only=args.programs_only
            )
            values = [dict(row) for row in rows]
            if args.json:
                print(
                    json.dumps(
                        {"generation": dict(generation), "entries": values},
                        indent=2,
                        ensure_ascii=False,
                    )
                )
            else:
                print(
                    f"{generation['display_name']} — {target.isoformat()} "
                    f"(generation {generation['id']}, "
                    f"{generation['validation_status']})"
                )
                for row in rows:
                    starts = datetime.fromisoformat(row["starts_at"])
                    ends = datetime.fromisoformat(row["ends_at"])
                    duration = (ends - starts).total_seconds()
                    source = (
                        config.storage_root("media")
                        / Path(row["source_relative_path"])
                        if row["source_relative_path"]
                        else None
                    )
                    print(
                        f"{starts:%H:%M:%S}–{ends:%H:%M:%S} "
                        f"[{row['daypart_slug']}/{row['entry_type']}] "
                        f"{row['title']} ({duration:.0f}s)"
                        + (
                            f" <{row['guide_label'] or row['programming_owner']}>"
                            if row["programming_owner"] != "base"
                            or row["guide_label"]
                            else ""
                        )
                    )
                    explanation = json.loads(row["explanation_json"] or "{}")
                    active_rules = explanation.get("active_rules", [])
                    if active_rules:
                        print(
                            "  rules: "
                            + ", ".join(
                                f"{item['rule']} ({item['type']})"
                                for item in active_rules
                            )
                        )
                    if source:
                        print(f"  {source}")
            return 0

        if args.command == "validate-schedule":
            scheduler = ScheduleBuilder(connection, config)
            try:
                status, detail = scheduler.validate_generation(args.generation)
            except ValueError as exc:
                print(f"Schedule error: {exc}", file=sys.stderr)
                return 1
            print(f"Generation {args.generation}: {status} — {detail}")
            return 0 if status == "valid" else 1

        if args.command in {"show-guide", "render-guide"}:
            try:
                when = datetime.fromisoformat(args.at) if args.at else None
            except ValueError:
                print("Invalid --at timestamp; expected ISO format", file=sys.stderr)
                return 2
            if args.command == "show-guide":
                payload = GuideService(connection, config).snapshot(when)
                if args.json:
                    print(json.dumps(payload, indent=2, ensure_ascii=False))
                else:
                    print(
                        f"Guide channel {payload['guide_channel_number']} "
                        f"as of {payload['as_of']}"
                    )
                    for channel in payload["channels"]:
                        now = channel["now"]["title"] if channel["now"] else "No listing"
                        following = (
                            channel["next"]["title"]
                            if channel["next"]
                            else "No listing"
                        )
                        print(
                            f"{channel['channel_number'] or '--':>2} "
                            f"{channel['display_name']}: NOW {now} | NEXT {following}"
                        )
                return 0
            try:
                renderer = GuideRenderer(connection, config)
                if args.if_needed and not renderer.needs_refresh(when):
                    print("Guide render is still fresh; no replacement was needed.")
                    return 0
                result = renderer.render(
                    when,
                    output=args.output,
                    page_seconds=args.page_seconds,
                )
            except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
                print(f"Guide render failed: {exc}", file=sys.stderr)
                return 1
            print(
                f"Guide render {result['render_id']}: {result['channels']} channels, "
                f"continuous {result['duration_seconds']:.1f}s loop at "
                f"{result['scroll_pixels_per_second']:.2f}px/s"
            )
            print(result["output"])
            return 0

        if args.command == "render-up-next":
            try:
                when = datetime.fromisoformat(args.at) if args.at else None
            except ValueError:
                print("Invalid --at timestamp; expected ISO format", file=sys.stderr)
                return 2
            renderer = UpNextRenderer(connection, config)
            try:
                result = renderer.render(
                    args.channel,
                    when,
                    listing_count=args.listings,
                    output=args.output,
                    use_tts=not args.no_tts,
                    force=args.force,
                )
            except (
                ValueError,
                RuntimeError,
                OSError,
                subprocess.SubprocessError,
            ) as exc:
                print(f"Up-next render failed: {exc}", file=sys.stderr)
                return 1
            print(f"Coming up next on {result.plan.channel_name}:")
            for line in result.plan.lines:
                print(
                    f"  {line.title} - "
                    f"{renderer.service._display_time(line.starts_at)}"
                )
            print(f"  {result.plan.tagline}")
            print(
                f"TTS: {result.tts_provider}; {result.duration_seconds:.2f}s; "
                f"{'cached' if result.cached else 'rendered'}"
            )
            print(result.output)
            return 0

        if args.command == "generate-generic-announcement":
            try:
                profiles = AnnouncementProfileService(connection, config)
                profile = profiles.profile(args.channel)
                client = OpenAIHTTPClient(
                    base_url=str(
                        config.data.get("announcements", {}).get(
                            "openai_base_url", "https://api.openai.com/v1"
                        )
                    )
                )
                output = profiles.generate_generic(
                    args.channel,
                    OpenAITTSProvider(client, model=profile.speech_model),
                )
            except (ValueError, RuntimeError, OSError) as exc:
                print(f"Generic announcement generation failed: {exc}", file=sys.stderr)
                return 1
            print(f"Reusable fallback generated for {profile.channel_name}:")
            print(output)
            return 0

        if args.command == "play-channel":
            resolver = ScheduleResolver(connection, config)
            when = None
            if args.at:
                if not args.dry_run:
                    print("--at is only supported with --dry-run", file=sys.stderr)
                    return 2
                try:
                    when = datetime.fromisoformat(args.at)
                except ValueError:
                    print("Invalid --at timestamp; use ISO format", file=sys.stderr)
                    return 2
                if when.tzinfo is None:
                    when = when.replace(tzinfo=resolver.scheduler.timezone)
            if args.dry_run:
                try:
                    target = resolver.resolve(args.channel, when)
                except PlaybackError as exc:
                    print(f"Playback resolution failed: {exc}", file=sys.stderr)
                    return 1
                print(f"Channel:        {args.channel}")
                print(f"Entry:          {target.entry_id}")
                print(f"Title:          {target.title}")
                print(f"Type:           {target.entry_type}")
                print(f"Representation: {target.representation}")
                print(f"Path:           {target.path}")
                print(f"Media offset:   {target.media_offset_seconds:.3f}s")
                print(f"Remaining:      {target.remaining_seconds:.3f}s")
                return 0
            player_settings = config.data.get("player", {})
            backend = args.backend or str(player_settings.get("backend", "auto"))
            try:
                player = create_player(config, backend)
            except ValueError as exc:
                print(f"Player configuration error: {exc}", file=sys.stderr)
                return 2
            controller = PlaybackController(
                resolver,
                player,
                skip_short_remaining_seconds=float(
                    player_settings.get("skip_short_remaining_seconds", 1.0)
                ),
            )
            if not args.no_keyboard:
                start_windows_keyboard(controller)
            try:
                controller.run(args.channel)
            except (PlaybackError, OSError) as exc:
                print(f"Playback stopped: {exc}", file=sys.stderr)
                return 1
            except KeyboardInterrupt:
                controller.stop()
            return 0

        if args.command == "serve":
            web = config.data.get("web", {})
            host = args.host or str(web.get("host", "127.0.0.1"))
            port = args.port or int(web.get("port", 8765))
            connection.close()
            serve(config, host=host, port=port)
            return 0

        if args.command == "run":
            web = config.data.get("web", {})
            host = args.host or str(web.get("host", "127.0.0.1"))
            port = args.port or int(web.get("port", 8765))
            connection.close()
            runtime = ApplicationRuntime(config)
            runtime.set_control_endpoint(host, port)
            url_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
            url = f"http://{url_host}:{port}"
            started = False

            def start_runtime_after_dashboard_binds():
                nonlocal started
                runtime.start()
                started = True
                if args.start_tv:
                    runtime.start_tv()
                if not args.no_browser:
                    threading.Timer(0.75, webbrowser.open, args=(url,)).start()

            try:
                serve(
                    config,
                    host=host,
                    port=port,
                    runtime=runtime,
                    before_serve=start_runtime_after_dashboard_binds,
                )
            except RuntimeError as exc:
                print(f"Unable to start CRTV Sim: {exc}", file=sys.stderr)
                return 1
            finally:
                if started:
                    runtime.shutdown()
            return 0
    finally:
        try:
            connection.close()
        except Exception:
            pass
    return 1
