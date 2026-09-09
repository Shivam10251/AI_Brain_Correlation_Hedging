"""
Run the two original suites under pytest (Phase 1).

Phase 1 asks for "pytest with both suites". The two existing files are
standalone scripts with their own runner and 79 assertions between
them, and they are the only regression coverage this project has.

Rewriting them into pytest functions would mean re-expressing ~700
lines of assertions by hand, where a single dropped check would be
invisible. So instead each script is executed as a subprocess and its
exit code becomes the test result: every one of the 79 checks still
runs, unchanged, and CI gets a single command.

New tests from Phase 1 onward are written as native pytest.
"""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

LEGACY_SUITES = [
    "tests/test_restart_recovery.py",
    "tests/test_api.py",
]


@pytest.mark.parametrize("suite", LEGACY_SUITES)
def test_legacy_suite_passes(suite):
    """
    Each legacy suite exits 0 only when every one of its checks passed.

    Run as a subprocess because the scripts set os.environ and install
    the MT5 shim at import time, and they call sys.exit() when done -
    all of which would leak into the rest of the session in-process.
    """

    completed = subprocess.run(
        [sys.executable, suite],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )

    if completed.returncode != 0:
        pytest.fail(
            f"{suite} failed (exit {completed.returncode})\n\n"
            f"--- stdout ---\n{completed.stdout[-6000:]}\n"
            f"--- stderr ---\n{completed.stderr[-3000:]}"
        )

    # The suites print their own tally; surface it so `pytest -s` shows
    # how many checks actually ran rather than just "1 passed".
    for line in completed.stdout.splitlines():
        if line.startswith("PASSED:"):
            print(f"{suite}: {line}")
