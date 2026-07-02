"""Put the repo root on sys.path so `from generators... import` works regardless of how
pytest is invoked (`python3 -m pytest` already adds cwd; this covers a bare `pytest` too)."""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
