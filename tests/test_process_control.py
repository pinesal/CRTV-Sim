from __future__ import annotations

import sys
import threading
import time
import unittest

from retrotv.process_control import ProcessCancelled, run_cancellable


class ProcessControlTests(unittest.TestCase):
    def test_completed_process_is_returned(self):
        event = threading.Event()
        result = run_cancellable(
            [sys.executable, "-c", "print('ready')"], 10.0, event
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "ready")

    def test_cancel_event_stops_child(self):
        event = threading.Event()
        timer = threading.Timer(0.1, event.set)
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaises(ProcessCancelled):
                run_cancellable(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    60.0,
                    event,
                )
        finally:
            timer.cancel()
        self.assertLess(time.monotonic() - started, 5.0)


if __name__ == "__main__":
    unittest.main()
