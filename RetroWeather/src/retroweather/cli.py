from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from retroweather.config import AppPaths, ConfigError, initialize, load_settings
from retroweather.locations import save_selected_location
from retroweather.models import NormalizedWeatherSnapshot, SnapshotValidationError
from retroweather.planning import PlanError, build_forecast_plan
from retroweather.providers.open_meteo import (
    GEOCODING_ATTRIBUTION,
    LocationQueryError,
    OpenMeteoGeocodingClient,
    ProviderRequestError,
    ProviderResponseError,
)
from retroweather.rendering import RenderError, render_and_publish
from retroweather.weather import build_weather_status, fetch_and_store_weather
from retroweather.web.jobs import record_render_success
from retroweather.web.server import serve


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="retroweather",
        description="Generate and publish a local retro Weather channel.",
    )
    parser.add_argument(
        "--home",
        help=(
            "Portable application-data root. Defaults to RETROWEATHER_HOME, "
            "then the current directory."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "init",
        help="Create settings, cache, media, and synthetic-channel directories.",
    )
    subparsers.add_parser(
        "fetch",
        help="Fetch provider weather data (implemented in Step 5).",
    )
    subparsers.add_parser(
        "show-weather",
        help="Print the latest normalized weather snapshot.",
    )
    subparsers.add_parser(
        "show-plan",
        help="Build and print the planned two-minute channel timeline.",
    )
    subparsers.add_parser(
        "render",
        help="Render, validate, and publish the two-minute forecast loop.",
    )
    location_search = subparsers.add_parser(
        "location-search", help="Search Open-Meteo for a city or postal code."
    )
    location_search.add_argument("query", help="City, postal code, or qualified location.")
    location_search.add_argument("--count", type=int, default=10)
    location_search.add_argument("--country-code", help="Optional two-letter country filter.")
    location_search.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    location_set = subparsers.add_parser(
        "location-set", help="Select an exact Open-Meteo location ID."
    )
    location_set.add_argument("location_id", type=int)
    dashboard = subparsers.add_parser("serve", help="Run the localhost dashboard.")
    dashboard.add_argument("--host", help="Override the configured bind address.")
    dashboard.add_argument("--port", type=int, help="Override the configured port.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    paths = AppPaths.discover(args.home)
    try:
        if args.command == "init":
            result = initialize(paths)
            action = "created" if result.settings_created else "preserved"
            print(f"RetroWeather home: {result.home}")
            print(f"Settings {action}: {result.settings}")
            print(f"Directories created: {result.directories_created}")
            return 0

        if args.command == "serve":
            serve(paths, host=args.host, port=args.port)
            return 0

        if args.command == "show-weather":
            return _show_weather(paths.latest_snapshot)

        if args.command == "show-plan":
            settings = load_settings(paths)
            snapshot = NormalizedWeatherSnapshot.load(paths.latest_snapshot)
            print(json.dumps(build_forecast_plan(snapshot, settings).to_dict(), indent=2))
            return 0

        if args.command == "location-search":
            settings = load_settings(paths)
            client = _geocoding_client(settings)
            results = client.search(
                args.query,
                count=args.count,
                country_code=args.country_code,
            )
            if args.json:
                print(
                    json.dumps(
                        {
                            "results": [item.to_dict() for item in results],
                            "attribution": GEOCODING_ATTRIBUTION,
                        },
                        indent=2,
                        ensure_ascii=False,
                    )
                )
            elif not results:
                print("No matching locations found.")
            else:
                for item in results:
                    print(
                        f"{item.provider_location_id}: {item.display_name} "
                        f"({item.latitude:.4f}, {item.longitude:.4f}) · {item.timezone}"
                    )
                print(GEOCODING_ATTRIBUTION)
            return 0

        if args.command == "location-set":
            settings = load_settings(paths)
            result = _geocoding_client(settings).get(args.location_id)
            save_selected_location(paths, result.to_selection())
            print(f"Selected location: {result.display_name}")
            print(f"Timezone: {result.timezone}")
            return 0

        if args.command == "fetch":
            snapshot = fetch_and_store_weather(paths)
            temperature = (
                "unknown"
                if snapshot.current.temperature is None
                else f"{snapshot.current.temperature:g}°"
            )
            print(f"Weather updated: {snapshot.location.display_name}")
            print(f"Current: {temperature} · {snapshot.current.condition_text}")
            print(
                f"Forecast: {len(snapshot.hourly)} hourly periods, "
                f"{len(snapshot.daily)} days"
            )
            print(f"Saved: {paths.latest_snapshot}")
            return 0

        if args.command == "render":
            result = render_and_publish(paths)
            record_render_success(paths, result)
            print(f"Published channel: {result.generation_id}")
            print(f"Video: {result.video}")
            print(f"Duration: {result.duration_seconds:.3f} seconds")
            print(f"Manifest: {result.manifest}")
            return 0
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except LocationQueryError as exc:
        print(f"Location error: {exc}", file=sys.stderr)
        return 2
    except (ProviderRequestError, ProviderResponseError, SnapshotValidationError, PlanError) as exc:
        print(f"Provider error: {exc}", file=sys.stderr)
        try:
            status = build_weather_status(paths)
            if status.available:
                print(
                    "The last valid weather snapshot remains available.",
                    file=sys.stderr,
                )
        except (ConfigError, OSError):
            pass
        return 3
    except RenderError as exc:
        print(f"Render error: {exc}", file=sys.stderr)
        return 5
    except OSError as exc:
        print(f"Storage error: {exc}", file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        print("\nRetroWeather stopped.")
        return 130
    return 2


def _geocoding_client(settings: dict[str, object]) -> OpenMeteoGeocodingClient:
    refresh = settings.get("refresh")
    timeout = refresh.get("provider_timeout_seconds", 20) if isinstance(refresh, dict) else 20
    return OpenMeteoGeocodingClient(timeout=float(timeout))


def _show_weather(snapshot: Path) -> int:
    if not snapshot.is_file():
        print("No normalized weather snapshot is available yet.")
        return 1
    try:
        weather = NormalizedWeatherSnapshot.load(snapshot)
    except (OSError, SnapshotValidationError) as exc:
        print(f"Unable to read normalized weather: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(weather.to_dict(), indent=2, ensure_ascii=False))
    return 0
