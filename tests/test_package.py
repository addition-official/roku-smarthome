import re
from pathlib import Path

import roku_smarthome

ROOT = Path(__file__).resolve().parent.parent


def test_star_import_exports_everything():
    namespace = {}
    exec("from roku_smarthome import *", namespace)
    for name in roku_smarthome.__all__:
        assert name in namespace, f"{name} is in __all__ but not importable"


def test_version_matches_pyproject():
    pyproject = (ROOT / "pyproject.toml").read_text()
    version = re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1)
    assert roku_smarthome.__version__ == version
