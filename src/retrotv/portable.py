from __future__ import annotations

import os
import json
import shutil
import sys
import threading
import traceback
from pathlib import Path

from retrotv.branding import portable_executable


def portable_root() -> Path:
    override = os.environ.get("RETROTV_PORTABLE_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))


def _seed_user_data(root: Path) -> None:
    source = root / "portable-defaults" / "user-data"
    destination = root / "user-data"
    destination.mkdir(parents=True, exist_ok=True)
    if not source.is_dir():
        return
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        target = destination / relative
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


def _previous_build_root(root: Path) -> Path | None:
    pointer = root / "previous-build.path"
    if not pointer.is_file():
        return None
    for raw in pointer.read_text(encoding="utf-8-sig").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        candidate = Path(raw)
        candidate = candidate if candidate.is_absolute() else root / candidate
        candidate = candidate.resolve()
        if (_same_path(candidate, root)
                or portable_executable(candidate) is None
                or not (candidate / "user-data").is_dir()):
            continue
        return candidate
    return None


def _import_previous_user_data(root: Path) -> None:
    """Clone the prior portable installation once so the test build is isolated."""
    destination = root / "user-data"
    marker = destination / ".internet-media-imported"
    from retrotv.onboarding import existing_profile
    if marker.is_file() or existing_profile(root):
        return
    previous = _previous_build_root(root)
    destination.mkdir(parents=True, exist_ok=True)
    if previous is not None and (previous / "user-data").is_dir():
        shutil.copytree(previous / "user-data", destination, dirs_exist_ok=True)
    marker.write_text(str(previous or "new installation") + "\n", encoding="utf-8")


def _refresh_managed_defaults(root: Path) -> None:
    """Seed defaults without replacing a legacy profile's inherited choices."""
    source = root / "portable-defaults" / "user-data" / "config" / "base.example.toml"
    destination = root / "user-data" / "config" / "base.example.toml"
    if not source.is_file() or destination.exists():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _portable_path_replacement(key: str, value: str) -> str | None:
    normalized = value.strip().replace("\\", "/")
    drive_relative = normalized[2:] if len(normalized) >= 2 and normalized[1] == ":" else ""
    expected = {
        "media": ("/Videos/media", "../Videos/media"),
        "originals_backup": (
            "/Videos/retrotv-originals-backup",
            "../Videos/retrotv-originals-backup",
        ),
    }
    suffix, replacement = expected.get(key, (None, None))
    if suffix and drive_relative.rstrip("/").casefold() == suffix.casefold():
        return replacement
    return None


def _portable_tool_replacement(key: str, value: str) -> str | None:
    normalized = value.strip().replace("\\", "/").casefold()
    if key == "mpv" and normalized in {
        "tools/mpv.exe", "./tools/mpv.exe", "/tools/mpv.exe"
    }:
        return "./tools/mpv/mpv.exe"
    return None


def _migrate_portable_paths(root: Path) -> None:
    """Convert known drive-letter paths without disturbing custom locations."""
    config_dir = root / "user-data" / "config"
    profile = config_dir / "portable.windows.toml"
    if profile.is_file():
        lines = profile.read_text(encoding="utf-8-sig").splitlines()
        section = ""
        changed = False
        for index, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                section = stripped[1:-1]
                continue
            if "=" not in line:
                continue
            key, _, raw = line.partition("=")
            name = key.strip()
            value = raw.strip().strip('"').strip("'")
            replacement = None
            if not section and name == "profile" and value == "windows-development":
                replacement = "windows-portable"
            # Library and backup paths in a legacy profile are user choices.
            # Do not reinterpret them just because their spelling resembles
            # the development installation's old drive layout.
            elif section == "player":
                replacement = _portable_tool_replacement(name, value)
            if replacement:
                lines[index] = f'{name} = "{replacement}"'
                changed = True
        if changed:
            profile.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Dashboard settings are explicit user choices and must always win over
    # portable defaults. In particular, never turn an external absolute media
    # path such as R:\Videos\media into a path relative to the application.
    settings = config_dir / "portable.windows.settings.json"
    if settings.is_file():
        try:
            payload = json.loads(settings.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return
        changed = False
        player = payload.get("player") if isinstance(payload, dict) else None
        if isinstance(player, dict) and isinstance(player.get("mpv"), str):
            replacement = _portable_tool_replacement("mpv", player["mpv"])
            if replacement:
                player["mpv"] = replacement
                changed = True
        if changed:
            settings.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )


def _redirect_output(root: Path) -> None:
    if not getattr(sys, "frozen", False):
        return
    log_dir = root / "user-data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = (log_dir / "retrotv.log").open("a", encoding="utf-8", buffering=1)
    sys.stdout = log
    sys.stderr = log


def _show_error(message: str) -> None:
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(0, message, "CRTV Sim", 0x10)
            return
        except (AttributeError, OSError):
            pass
    print(message, file=sys.stderr)


def _configure_portable_tools(root: Path) -> None:
    ffmpeg_dir = root / "tools" / "ffmpeg"
    mpv_dir = root / "tools" / "mpv"
    existing = os.environ.get("PATH", "")
    os.environ["PATH"] = os.pathsep.join(
        [os.fspath(ffmpeg_dir), os.fspath(mpv_dir), existing]
    )
    if os.name == "nt":
        try:
            import ctypes

            # Child tool failures should be reported to CRTV Sim, never as a
            # cascade of modal Windows application-error dialogs.
            ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002)
        except (AttributeError, OSError):
            pass


def _mark_production_storage(config, root: Path) -> None:
    """Best-effort marker work that must never hold dashboard startup."""
    try:
        from retrotv.production_guard import mark_production_volumes

        failures = mark_production_volumes(config, portable_root=root)
        if failures:
            detail = "\n".join(
                f"{item['volume']}: {item['error']}" for item in failures
            )
            print(
                "CRTV Sim could not mark every configured drive as production storage. "
                "Development builds may not be able to warn before using these paths:\n\n"
                + detail,
                file=sys.stderr,
            )
    except BaseException:
        # A disconnected or unhealthy storage device can hold or fail a marker
        # operation. The dashboard and its diagnostics must remain available.
        traceback.print_exc()


def _start_production_marking(config, root: Path) -> None:
    threading.Thread(
        target=_mark_production_storage,
        args=(config, root),
        name="retrotv-production-marker",
        daemon=True,
    ).start()


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    root = portable_root()
    # Marks this process (and its managed children) as a portable runtime.
    # Playback uses this stable root to require the bundled mpv instead of
    # opening a new ffplay window for every scheduled item.
    os.environ["RETROTV_PORTABLE_ROOT"] = str(root)
    _configure_portable_tools(root)
    profile = root / "user-data" / "config" / "portable.windows.toml"
    if arguments[:1] == ["--retroweather"]:
        from retroweather.cli import main as weather_main

        return weather_main(arguments[1:])

    try:
        from retrotv.onboarding import existing_profile, atomic_json, serve_setup

        _import_previous_user_data(root)
        needs_setup = not existing_profile(root)
        if (not needs_setup and (root / "user-data/database/retrotv.sqlite3").exists()
                and not profile.exists()
                and not profile.with_suffix(".settings.json").exists()):
            raise FileNotFoundError(
                "An existing database was found but its profile configuration is missing. "
                "Restore user-data/config from your profile backup; this is not a new installation.")
        pending = root / "user-data/state/setup-pending.json"
        if needs_setup:
            atomic_json(pending, {"schema_version": 1})
        _seed_user_data(root)
        _refresh_managed_defaults(root)
        _migrate_portable_paths(root)
        _redirect_output(root)
        # CLI diagnostics do not need a browser setup session. The ordinary
        # launcher must commit the choices before opening a database or scanner.
        if needs_setup and not any(arg in {"--help", "-h", "--version"} for arg in arguments):
            if not serve_setup(root):
                return 0
            pending.unlink(missing_ok=True)
        elif not needs_setup and not any(arg in {"--help", "-h", "--version"} for arg in arguments):
            from retrotv.runtime_terms import confirm_upgrade
            if not confirm_upgrade(root):
                return 0
        os.chdir(root)
        if not profile.is_file():
            raise FileNotFoundError(f"Portable configuration is missing: {profile}")
        from retrotv import __version__
        from retrotv.config import load_config

        print(f"[portable] launcher {__version__}; root={root}", file=sys.stderr)
        config = load_config(profile)
        print(f"[portable] configuration loaded: {profile}", file=sys.stderr)
        # Drive roots can be slow, disconnected, or protected. Marker creation
        # is useful bookkeeping, but it cannot precede the dashboard listener.
        _start_production_marking(config, root)
        print("[portable] production marking started in background", file=sys.stderr)
        from retrotv.cli import main as retrotv_main

        if not arguments:
            arguments = ["run"]
        if "--config" not in arguments:
            arguments = ["--config", str(profile), *arguments]
        print("[portable] starting CRTV Sim application", file=sys.stderr)
        return retrotv_main(arguments)
    except SystemExit as exc:
        # argparse uses SystemExit for successful --help output and ordinary
        # command-line usage errors. Preserve that exit code without showing
        # the portable launcher's fatal-error dialog.
        return int(exc.code) if isinstance(exc.code, int) else 1
    except BaseException as exc:
        traceback.print_exc()
        _show_error(
            "CRTV Sim could not start.\n\n"
            f"{exc}\n\n"
            f"See {root / 'user-data' / 'logs' / 'retrotv.log'} for details."
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
