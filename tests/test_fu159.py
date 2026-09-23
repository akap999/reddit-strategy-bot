"""FU159 — blog-gen latency: the serial web-search fan-out in `_source_for_completion` (Pass B rescue +
dim-rescue) and the YMYL `_gather_authoritative_sources` legs now run CONCURRENTLY. These tests prove
(a) concurrency actually happens (>1 in-flight search) and (b) the output is DETERMINISTIC under the
thread-pool (results merged in original order → byte-identical to the old serial run). $0, no network."""
import threading
import time

from generators.blog_gen import BlogGenerator
from tests.stubs import StubClaude


class _Tracker:
    """Records the max number of `search_sources` calls in flight at once."""
    def __init__(self):
        self._lock = threading.Lock()
        self.inflight = 0
        self.max_inflight = 0

    def enter(self):
        with self._lock:
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)

    def leave(self):
        with self._lock:
            self.inflight -= 1


class _CannedPages(dict):
    """FU267 — `_gather_authoritative_sources` now READS each source it keeps, and `read_page`
    short-circuits on a cache hit. A cache that answers for ANY url keeps these unit tests off the
    network. Before this the gather stored a search gloss and never opened the URL, so no test
    needed one."""

    def __contains__(self, _url):
        return True

    def __getitem__(self, _url):
        return ("Prescribing information. Indications, dosage and administration. " * 40, "direct")


def _tracking_search(tracker, names, official=False):
    """A deterministic search_handler that names a block after whichever `names` entry is in the brief,
    sleeping briefly so concurrent calls actually overlap (revealing max in-flight)."""
    def h(brief, allowed, blocked):
        tracker.enter()
        try:
            time.sleep(0.03)
            bl = (brief or "").lower()
            for nm in names:
                if nm.lower() in bl:
                    if official:
                        return [{"url": f"https://www.fda.gov/{nm.lower()}-label",
                                 "fact": f"{nm} prescribing information", "title": f"{nm} label"}]
                    return [{"url": f"https://{nm.lower().replace(' ', '')}.com/pricing",
                             "fact": f"{nm} pricing $9/mo billed monthly", "title": f"{nm} pricing"}]
            return []
        finally:
            tracker.leave()
    return h


def _brand():
    return {"name": "Acme", "domain_url": "https://acme.com", "category": "telehealth"}


# --- Change 1 + 2: _source_for_completion parallel + deterministic --------------------------------
def _run_source(tracker):
    tools = ["Noom", "Ro", "Hims"]

    def call_h(p):
        if '"peer_tools"' in p and '"dimensions"' in p:
            return {"tools": tools, "peer_tools": tools, "dimensions": ["pricing", "eligibility",
                    "delivery", "coverage"], "products": [], "claims": [], "core_topic": "telehealth"}
        return {}   # domain-resolve etc. → no domains, so Pass B + dim-rescue both fire

    stub = StubClaude(call_handler=call_h,
                      search_handler=_tracking_search(tracker, tools))
    gen = BlogGenerator(stub, db=None)
    body = ("| Clinic | Pricing | Eligibility | Delivery | Coverage |\n|---|---|---|---|---|\n"
            "| Noom | ? | ? | ? | ? |\n| Ro | ? | ? | ? | ? |\n| Hims | ? | ? | ? | ? |\n")
    return gen._source_for_completion(_brand(), "best telehealth clinics",
                                      {"body_markdown": body}, ymyl=None)


def test_source_for_completion_runs_concurrently():
    tr = _Tracker()
    sourcing = _run_source(tr)
    assert sourcing is not None
    assert tr.max_inflight >= 2   # Pass B rescue / dim-rescue searches overlapped (not one-at-a-time)


def test_source_for_completion_output_is_deterministic():
    # Two runs through the thread-pool must produce the IDENTICAL `fresh` (labels+urls+order) — proving
    # the merge is order-preserving, so parallelizing did not change the [S#] sequence.
    a = _run_source(_Tracker())
    b = _run_source(_Tracker())
    key = lambda s: [(x.get("label"), x.get("url")) for x in (s.get("fresh") or [])]
    assert key(a) == key(b)
    assert len(a["fresh"]) > 0


# --- Change 3: YMYL _gather_authoritative_sources per-subject prefetch concurrent + deterministic ---
def _run_auth(tracker):
    subs = ["tirzepatide", "semaglutide", "testosterone"]
    stub = StubClaude(call_handler=lambda p: {},
                      search_handler=_tracking_search(tracker, subs, official=True))
    gen = BlogGenerator(stub, db=None)
    gen._claim_pages = _CannedPages()   # FU267: the gather READS now
    return gen._gather_authoritative_sources(_brand(), "men's GLP-1 + TRT", "GLP-1 therapy",
                                             "medical", products=subs)


def test_ymyl_authoritative_prefetch_runs_concurrently():
    tr = _Tracker()
    blocks = _run_auth(tr)
    # the 3 per-subject pinned leg-A searches were pre-fetched concurrently
    assert tr.max_inflight >= 2
    assert any("official ·" in (b.get("label") or "") for b in blocks)


def test_ymyl_authoritative_output_is_deterministic():
    a = _run_auth(_Tracker())
    b = _run_auth(_Tracker())
    key = lambda blocks: [(x.get("label"), x.get("url")) for x in blocks]
    assert key(a) == key(b)
    assert len(a) > 0
