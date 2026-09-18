"""Plugin contract gate: this repo never imports host internals (src.* / kernel.*)."""
import re
from pathlib import Path

_PLUGIN = Path(__file__).resolve().parents[1] / "plugin"

_HOST_IMPORT = re.compile(r"^[ \t]*(?:from|import)\s+(?:src|kernel)(?:\.|\s|$)", re.MULTILINE)

_SAMPLES = [
    "from src.config import TTS_API_KEY",
    "import src.database",
    "from kernel.plugins.package import _norm_version",
]
_NOT_SAMPLES = [
    "from fastapi import APIRouter",
    "import asyncio",
    "    # see src.config for defaults",
]


def test_detector_catches_host_imports():
    for line in _SAMPLES:
        assert _HOST_IMPORT.search(line), f"detector missed: {line!r}"
    for line in _NOT_SAMPLES:
        assert not _HOST_IMPORT.search(line), f"detector false-positive: {line!r}"


def test_plugin_never_imports_host_internals():
    offenders = []
    for py in sorted(_PLUGIN.rglob("*.py")):
        text = py.read_text(encoding="utf-8")
        for m in _HOST_IMPORT.finditer(text):
            line_no = text.count("\n", 0, m.start()) + 1
            offenders.append(f"{py.relative_to(_PLUGIN)}:{line_no}: {m.group(0).strip()}")
    assert not offenders, "plugin imports host internals:\n" + "\n".join(offenders)
