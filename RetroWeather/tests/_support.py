from __future__ import annotations

import shutil
import unittest
import uuid
from pathlib import Path


class WorkspaceTestCase(unittest.TestCase):
    """Provide a per-test directory inside the repository's writable test root."""

    def setUp(self) -> None:
        project = Path(__file__).resolve().parents[2]
        self.temporary = project / ".test-tmp" / f"retroweather-{uuid.uuid4().hex}"
        self.temporary.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.temporary, ignore_errors=True)
