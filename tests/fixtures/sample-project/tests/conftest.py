"""Put the fixture's `src` on sys.path.

The fixture is not an installable package on purpose: `install: -e .[dev]` is only one of
four install shapes the reusable serves, and the self-test should not quietly assume the
one that happens to be most common.
"""

import pathlib
import sys

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))
