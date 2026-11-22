import re
from pathlib import Path

from version import __version__

ROOT = Path(__file__).resolve().parents[1]


def test_version_is_the_same_everywhere():
    pyproject = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M).group(1)
    assert pyproject == __version__
    assert f"## {__version__}" in (ROOT / "CHANGELOG.md").read_text()
