"""Kept for older imports: the fixture now lives in nlcl/bench/fixture.py."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from nlcl.bench.fixture import FILES, build  # noqa: E402,F401
