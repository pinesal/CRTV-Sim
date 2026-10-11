from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout

from _support import WorkspaceTestCase
from retroweather.cli import main
from retroweather.config import AppPaths


class CommandTests(WorkspaceTestCase):
    def test_init_command_creates_home(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            result = main(["--home", str(self.temporary), "init"])

        self.assertEqual(result, 0)
        self.assertTrue(AppPaths.discover(self.temporary).settings.is_file())
        self.assertIn("Settings created", output.getvalue())

    def test_fetch_requires_a_selected_location(self) -> None:
        with redirect_stdout(io.StringIO()):
            main(["--home", str(self.temporary), "init"])
        errors = io.StringIO()
        with redirect_stderr(errors):
            result = main(["--home", str(self.temporary), "fetch"])

        self.assertEqual(result, 2)
        self.assertIn("Select a weather location", errors.getvalue())


if __name__ == "__main__":
    import unittest

    unittest.main()
