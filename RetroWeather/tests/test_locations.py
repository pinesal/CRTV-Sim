from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from _support import WorkspaceTestCase
from retroweather.cli import main
from retroweather.config import AppPaths, initialize, load_settings
from retroweather.locations import load_selected_location, save_selected_location
from retroweather.providers.open_meteo import (
    LocationQueryError,
    LocationSearchResult,
    OpenMeteoGeocodingClient,
    ProviderRequestError,
    ProviderResponseError,
)


FIXTURES = Path(__file__).parent / "fixtures"
SEARCH_PAYLOAD = json.loads(
    (FIXTURES / "open-meteo-geocoding-search.json").read_text(encoding="utf-8")
)
GET_PAYLOAD = json.loads(
    (FIXTURES / "open-meteo-geocoding-get.json").read_text(encoding="utf-8")
)


class FixtureGeocoder:
    def __init__(self, timeout: float = 20.0) -> None:
        self.timeout = timeout

    def search(self, query: str, count: int = 10, country_code: str | None = None):
        return tuple(
            LocationSearchResult.from_api(item, f"fixture[{index}]")
            for index, item in enumerate(SEARCH_PAYLOAD["results"])
        )

    def get(self, provider_location_id: int):
        if provider_location_id != 5368361:
            raise LocationQueryError("Unknown fixture location ID.")
        return LocationSearchResult.from_api(GET_PAYLOAD)


class OpenMeteoGeocodingTests(WorkspaceTestCase):
    def test_search_builds_documented_query_and_parses_results(self) -> None:
        requests: list[tuple[str, float]] = []

        def transport(url: str, timeout: float):
            requests.append((url, timeout))
            return SEARCH_PAYLOAD

        results = OpenMeteoGeocodingClient(timeout=7, transport=transport).search(
            "Los Angeles, California", count=2, country_code="us"
        )

        parameters = parse_qs(urlparse(requests[0][0]).query)
        self.assertEqual(parameters["name"], ["Los Angeles, California"])
        self.assertEqual(parameters["count"], ["2"])
        self.assertEqual(parameters["format"], ["json"])
        self.assertEqual(parameters["language"], ["en"])
        self.assertEqual(parameters["countryCode"], ["US"])
        self.assertEqual(requests[0][1], 7.0)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].display_name, "Los Angeles, California, United States")
        self.assertEqual(results[0].timezone, "America/Los_Angeles")
        self.assertIsNone(results[1].population)

    def test_exact_location_lookup_and_normalized_conversion(self) -> None:
        client = OpenMeteoGeocodingClient(transport=lambda url, timeout: GET_PAYLOAD)
        result = client.get(5368361)
        location = result.to_location()

        self.assertEqual(result.provider_location_id, 5368361)
        self.assertEqual(location.display_name, "Los Angeles, California, United States")
        self.assertEqual(location.latitude, 34.05223)
        self.assertEqual(location.administrative_area, "California")

    def test_empty_queries_and_api_errors_are_clear(self) -> None:
        client = OpenMeteoGeocodingClient(
            transport=lambda url, timeout: {"error": True, "reason": "fixture failure"}
        )
        with self.assertRaises(LocationQueryError):
            client.search("x")
        with self.assertRaisesRegex(ProviderRequestError, "fixture failure"):
            client.search("valid")

    def test_malformed_provider_result_is_rejected(self) -> None:
        malformed = dict(GET_PAYLOAD)
        malformed["latitude"] = 200
        with self.assertRaisesRegex(ProviderResponseError, "location.latitude"):
            LocationSearchResult.from_api(malformed)

    def test_selection_persists_without_replacing_other_settings(self) -> None:
        paths = AppPaths.discover(self.temporary)
        initialize(paths)
        result = LocationSearchResult.from_api(GET_PAYLOAD)

        save_selected_location(paths, result.to_selection())
        selected = load_selected_location(paths)
        settings = load_settings(paths)

        self.assertIsNotNone(selected)
        self.assertEqual(selected.provider_location_id, 5368361)
        self.assertEqual(selected.location.timezone, "America/Los_Angeles")
        self.assertEqual(settings["refresh"]["data_minutes"], 15)

    def test_cli_search_and_exact_selection(self) -> None:
        paths = AppPaths.discover(self.temporary)
        initialize(paths)
        output = io.StringIO()

        with patch("retroweather.cli.OpenMeteoGeocodingClient", FixtureGeocoder):
            with redirect_stdout(output):
                search_result = main(
                    ["--home", str(self.temporary), "location-search", "Los Angeles"]
                )
                select_result = main(
                    ["--home", str(self.temporary), "location-set", "5368361"]
                )

        self.assertEqual(search_result, 0)
        self.assertEqual(select_result, 0)
        self.assertIn("Los Angeles, California, United States", output.getvalue())
        self.assertEqual(load_selected_location(paths).provider_location_id, 5368361)
