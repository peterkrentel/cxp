"""tests/run_tests.py is invoked as a bare script by the CronJob
(`python -u /app/tests/run_tests.py`), which only puts the script's own
directory on sys.path -- not its parent. A `from src...`/`from tests...`
import at module scope before the sys.path.insert() fix below silently
fails with ModuleNotFoundError in that exact invocation, even though it
imports fine under pytest (where `src`/`tests` are already top-level
packages via the repo root). Confirmed live 2026-08-23: the CronJob's
first real run after this PR's code was ever deployed failed immediately
with `ModuleNotFoundError: No module named 'src'` at import time.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_sys_path_fix_runs_before_any_src_or_tests_import():
    source = (ROOT / "tests/run_tests.py").read_text()

    path_fix_index = source.index("sys.path.insert(")
    # Match only an actual import statement at the start of a line, not this
    # module's own explanatory comments/docstrings mentioning "from src." --
    # a naive substring search matched inside a comment here once already.
    import_match = re.search(r"^from src\.", source, re.MULTILINE)
    assert import_match is not None

    assert path_fix_index < import_match.start()
