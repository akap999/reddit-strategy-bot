"""Put the repo root on sys.path so `from generators... import` works regardless of how
pytest is invoked (`python3 -m pytest` already adds cwd; this covers a bare `pytest` too)."""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


# ── FU267: the suite does not touch the network ──────────────────────────────────────────────────
# `_source_block` READS every source the generator keeps, so a dozen gather paths that used to
# store a search gloss now open a URL. Under test that turned a 70-second suite into 5m43s of real
# fetches — and a unit test whose result depends on whether a remote host answers is not a unit
# test. This stubs the one reader they all go through.
#
# Opt out with `@pytest.mark.network` for a test that genuinely means to fetch.
import pytest  # noqa: E402

# An un-primed url reads as UNREACHABLE, not as a full page. A stub that made every fetch succeed
# would silently rewrite the premise of every test about what happens when a source cannot be read.
_UNREACHABLE = ("", "blocked")


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "network: drives the real fetch stack — exempt from the no-network stub")


@pytest.fixture(autouse=True)
def _no_network_page_reads(request, monkeypatch):
    if "network" in request.keywords:
        return
    # Stub the NETWORK LAYER, not `read_page` itself. `read_page` then runs for real — so its own
    # cache handling, its wall detection and its ladder are still under test, and a test that
    # patches `read_page` or `_fetch_page` itself still wins, because its patch is applied after
    # this one. Stubbing `read_page` instead broke eighteen tests that reach beneath it.
    from generators import research as _research
    monkeypatch.setattr(_research, "_fetch_page",
                        lambda url, **kw: _UNREACHABLE, raising=False)
