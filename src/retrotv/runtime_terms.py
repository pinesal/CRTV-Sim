"""One-time acknowledgement scoped to the bundled Microsoft runtime only."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

NOTICE = "Microsoft-Visual-C-Runtime.txt"


def terms(root: Path) -> str | None:
    record = root / "MICROSOFT-RUNTIME.json"
    if not record.is_file():
        return None  # Source runs and earlier distributions have no such bundle.
    metadata = json.loads(record.read_text(encoding="utf-8"))
    content = (root / "licenses/microsoft" / NOTICE).read_bytes()
    if hashlib.sha256(content).hexdigest() != metadata["notices"][NOTICE]["sha256"]:
        raise ValueError("Bundled Microsoft runtime terms are missing or changed")
    return content.decode("utf-8")


def accepted(root: Path) -> bool:
    text = terms(root)
    if text is None:
        return True
    try:
        record = json.loads((root / "user-data/state/microsoft-runtime-terms.json").read_text(encoding="utf-8"))
        return record.get("accepted") is True and record.get("sha256") == hashlib.sha256(text.encode("utf-8")).hexdigest()
    except (OSError, ValueError):
        return False


def acknowledge(root: Path) -> None:
    from retrotv.onboarding import atomic_json

    text = terms(root)
    if text is not None:
        atomic_json(root / "user-data/state/microsoft-runtime-terms.json",
                    {"sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "accepted": True})


def confirm_upgrade(root: Path) -> bool:
    """Existing profiles keep their setup; display only the new runtime terms."""
    if accepted(root):
        return True
    import ctypes

    while True:
        result = ctypes.windll.user32.MessageBoxW(
            0,
            "This build includes Microsoft's Visual C++ runtime. Its terms apply only "
            "to that runtime, not to CRTV Sim or the other bundled components.\n\n"
            "Yes: accept the Microsoft runtime terms and continue.\n"
            "No: exit without starting CRTV Sim.\n"
            "Cancel: open the complete terms, then return here.\n\n"
            "Terms: " + str(root / "licenses/microsoft" / NOTICE),
            "CRTV Sim — bundled runtime terms", 0x00000003 | 0x00000020)
        if result == 6:
            acknowledge(root)
            return True
        if result != 2:
            return False
        os.startfile(root / "licenses/microsoft" / NOTICE)
