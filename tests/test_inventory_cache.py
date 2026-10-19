import io
import json
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from retrotv.inventory_cache import InventoryCache
from retrotv.web import make_handler


class InventoryCacheTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.signature = ("unchanged",)
        self.loader = Mock(return_value={"items": [{"title": "Show"}], "summary": {"review": 4}})
        self.cache = InventoryCache(Path("unused.sqlite3"), self.loader, clock=lambda: self.now)
        self.cache._signature = lambda: self.signature

    def test_revisits_reuse_snapshot_without_rebuilding_and_time_expiry_refreshes(self):
        first = self.cache.get()
        self.assertTrue(first["cache"]["ready"])
        self.assertFalse(first["cache"]["stale"])
        self.assertIsNotNone(first["cache"]["updated_at"])
        self.assertEqual(self.cache.get(), first)
        self.loader.assert_called_once()
        self.now += 301
        expired = self.cache.get()
        self.assertTrue(expired["cache"]["refresh_due"])
        self.assertEqual(expired["items"], first["items"])
        self.loader.assert_called_once()
        self.cache.get("auto")
        self.assertEqual(self.loader.call_count, 2)

    def test_database_changes_are_detected_but_automatic_rebuilds_are_throttled(self):
        self.cache.get()
        self.signature = ("changed",)
        self.now += 1
        status = self.cache.get()["cache"]
        self.assertTrue(status["stale"])
        self.assertFalse(status["refresh_due"])
        self.cache.get("auto")
        self.loader.assert_called_once()
        self.now += 60
        self.assertTrue(self.cache.get()["cache"]["refresh_due"])
        self.assertFalse(self.cache.get("auto")["cache"]["stale"])
        self.assertEqual(self.loader.call_count, 2)

    def test_explicit_edits_and_manual_refresh_do_not_wait_for_cache_expiry(self):
        self.cache.get()
        self.cache.invalidate()
        self.assertTrue(self.cache.get()["cache"]["refresh_due"])
        self.assertFalse(self.cache.get("auto")["cache"]["stale"])
        self.cache.get("force")
        self.assertEqual(self.loader.call_count, 3)

    def test_failed_refresh_preserves_previous_results_and_backs_off_before_retry(self):
        original = self.cache.get()
        self.loader.side_effect = RuntimeError("database is locked")
        failed = self.cache.get("force")
        self.assertEqual(failed["items"], original["items"])
        self.assertEqual(failed["cache"]["updated_at"], original["cache"]["updated_at"])
        self.assertEqual(failed["cache"]["revision"], original["cache"]["revision"])
        self.assertEqual(failed["cache"]["error"], "database is locked")
        self.assertFalse(failed["cache"]["refresh_due"])
        self.cache.get("auto")
        self.assertEqual(self.loader.call_count, 2)
        self.now += 31
        self.loader.side_effect = None
        self.assertTrue(self.cache.get()["cache"]["refresh_due"])
        recovered = self.cache.get("auto")
        self.assertIsNone(recovered["cache"]["error"])
        self.assertEqual(recovered["cache"]["revision"], 2)

    def test_initial_failure_can_be_manually_retried_without_fake_empty_inventory(self):
        self.loader.side_effect = RuntimeError("unavailable")
        failed = self.cache.get()
        self.assertFalse(failed["cache"]["ready"])
        self.assertNotIn("items", failed)
        self.cache.get()
        self.loader.assert_called_once()
        self.loader.side_effect = None
        self.assertTrue(self.cache.get("force")["cache"]["ready"])

    def test_concurrent_refreshes_share_work_and_cached_reads_do_not_block(self):
        original = self.cache.get()
        entered = threading.Event()
        finish = threading.Event()

        def slow_refresh():
            entered.set()
            if not finish.wait(5):
                raise RuntimeError("Test did not release refresh")
            return {"items": [{"title": "New show"}]}

        self.loader.side_effect = slow_refresh
        worker = threading.Thread(target=lambda: self.cache.get("force"))
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            for mode in ("", "auto", "force"):
                result = self.cache.get(mode)
                self.assertEqual(result["items"], original["items"])
                self.assertTrue(result["cache"]["refreshing"])
            self.assertEqual(self.loader.call_count, 2)
            self.cache.invalidate()
        finally:
            finish.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        result = self.cache.get()
        self.assertEqual(result["items"], [{"title": "New show"}])
        self.assertTrue(result["cache"]["stale"])
        self.assertTrue(result["cache"]["refresh_due"])

    def test_database_and_wal_signature_changes_do_not_require_open_connections(self):
        stats = [Mock(st_ino=1, st_size=10, st_mtime_ns=10),
                 Mock(st_ino=2, st_size=20, st_mtime_ns=20)]
        cache = InventoryCache(Path("unused.sqlite3"), self.loader)
        with patch.object(Path, "stat", side_effect=stats):
            first = cache._signature()
        stats[1].st_mtime_ns += 1
        with patch.object(Path, "stat", side_effect=stats):
            self.assertNotEqual(first, cache._signature())
        with patch.object(Path, "stat", side_effect=OSError("offline")):
            self.assertEqual(cache._signature(), (None, None))

    def test_http_handler_reuses_cache_and_successful_post_invalidates_it(self):
        config = Mock(database_path=Path("unused.sqlite3"))
        handler_type = make_handler(config)

        def request(path, command="GET", status=200):
            handler = object.__new__(handler_type)
            handler.path = path
            handler.command = command
            handler.wfile = io.BytesIO()
            handler.send_response = Mock()
            handler.send_header = Mock()
            handler.end_headers = Mock()
            if command == "GET":
                handler.do_GET()
            else:
                handler._json({"ok": True}, status)
            return json.loads(handler.wfile.getvalue())

        with patch("retrotv.web.connect_database") as connect, \
                patch("retrotv.web._media_inventory", self.loader), \
                patch.object(InventoryCache, "_signature", return_value=("same",)):
            request("/api/inventory")
            request("/api/inventory")
            self.loader.assert_called_once()
            self.assertEqual(connect.return_value.close.call_count, 1)
            request("/api/assets/1", "POST", 400)
            self.assertFalse(request("/api/inventory")["cache"]["stale"])
            request("/api/runtime/tune", "POST")
            self.assertFalse(request("/api/inventory")["cache"]["stale"])
            request("/api/assets/1", "POST")
            self.assertTrue(request("/api/inventory")["cache"]["refresh_due"])
            self.assertFalse(request("/api/inventory?refresh=auto")["cache"]["stale"])
            request("/api/inventory?refresh=1")
            self.assertEqual(self.loader.call_count, 3)


if __name__ == "__main__":
    unittest.main()
