"""FU154 — writer 'Test connection' probe + on-demand watermark-free REWRITE of an existing blog
(original kept in body_markdown; the reworded copy is stored in rewritten_body). $0, no network."""
import os
import tempfile

import requests as _requests

from generators.base import WriterClient
from generators.blog_gen import BlogGenerator
from db import Database
from tests.stubs import StubClaude


class StubWriter:
    def __init__(self, responder):
        self.responder = responder
        self.prompts = []
        self.temps = []

    def call_text(self, prompt, system_prompt=None, max_tokens=6000, temperature=0.7, timeout=300):
        self.prompts.append(prompt)
        self.temps.append(temperature)
        return self.responder(prompt, len(self.prompts) - 1)


CLAUDE_BODY = (
    "# Best widgets for teams\n\n## Quick answer\n"
    "The Acme widget is the strongest pick for most teams because it ships quickly and costs "
    "noticeably less than the alternatives [S1], and it connects to your stack without any custom glue [S2].\n\n"
    "## Is the Acme widget reliable?\n"
    "Yes — independent benchmarking consistently reports high uptime across a wide range of workloads [S1].\n"
)
REWRITE_BODY = (
    "# Best widgets for teams\n\n## Quick answer\n"
    "Acme leads the pack for the majority of squads: rollout is rapid and the price undercuts rival "
    "options [S1], while hooking it into an existing toolchain needs zero bespoke plumbing [S2].\n\n"
    "## Is the Acme widget reliable?\n"
    "Third-party stress tests keep showing dependable availability even when demand surges hard [S1].\n"
)


# --- Feature A: WriterClient.probe ---------------------------------------------------------------
class _Resp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def test_probe_ok(monkeypatch):
    import generators.base as base
    monkeypatch.setattr(base.requests, "post",
                        lambda *a, **k: _Resp(200, {"choices": [{"message": {"content": "OK"}}]}))
    r = WriterClient("https://x/v1", "k", "m").probe()
    assert r["state"] == "ok" and r["sample"] == "OK"


def test_probe_warming_on_redirect(monkeypatch):
    import generators.base as base
    monkeypatch.setattr(base.requests, "post", lambda *a, **k: _Resp(303, text="redirect"))
    assert WriterClient("https://x/v1", "k", "m").probe()["state"] == "warming"


def test_probe_auth(monkeypatch):
    import generators.base as base
    monkeypatch.setattr(base.requests, "post", lambda *a, **k: _Resp(401, text="unauthorized"))
    assert WriterClient("https://x/v1", "k", "m").probe()["state"] == "auth"


def test_probe_warming_on_timeout(monkeypatch):
    import generators.base as base

    def _raise(*a, **k):
        raise _requests.exceptions.Timeout()
    monkeypatch.setattr(base.requests, "post", _raise)
    assert WriterClient("https://x/v1", "k", "m").probe(timeout=1)["state"] == "warming"


def test_probe_unreachable(monkeypatch):
    import generators.base as base

    def _raise(*a, **k):
        raise _requests.exceptions.ConnectionError("no route to host")
    monkeypatch.setattr(base.requests, "post", _raise)
    assert WriterClient("https://x/v1", "k", "m").probe()["state"] == "unreachable"


def test_probe_missing_config():
    assert WriterClient("", "k", "m").probe()["state"] == "error"
    assert WriterClient("https://x/v1", "k", "").probe()["state"] == "error"


# --- Feature B: the store-vs-fallback decision the /rewrite endpoint keys on ----------------------
def _gen(writer):
    return BlogGenerator(StubClaude(), db=None, writer=writer, writer_mode="rewrite")


def test_rewrite_existing_blog_stores_when_valid():
    gen = _gen(StubWriter(lambda p, i: REWRITE_BODY))
    art = {"body_markdown": CLAUDE_BODY}
    out = gen._apply_writer_pass(art, CLAUDE_BODY, {"name": "Acme"}, "best widgets for teams")
    assert out == REWRITE_BODY
    assert art["writer_mode_used"] == "rewrite"          # endpoint WILL store rewritten_body
    assert art.get("writer_overlap", 1.0) < 0.15


def test_rewrite_uses_higher_temperature_and_records_secs():
    w = StubWriter(lambda p, i: REWRITE_BODY)
    gen = _gen(w)
    art = {"body_markdown": CLAUDE_BODY}
    gen._apply_writer_pass(art, CLAUDE_BODY, {"name": "Acme"}, "seed")
    assert w.temps and w.temps[0] > 0.7                 # FU155: bumped sampling temperature (was 0.7)
    assert art.get("writer_secs") is not None and art["writer_secs"] >= 0   # FU155: timed for cost


def test_rewrite_existing_blog_falls_back_when_citation_dropped():
    gen = _gen(StubWriter(lambda p, i: REWRITE_BODY.replace(" [S2]", "")))
    art = {"body_markdown": CLAUDE_BODY}
    out = gen._apply_writer_pass(art, CLAUDE_BODY, {"name": "Acme"}, "seed")
    assert out == CLAUDE_BODY                            # original preserved
    assert art["writer_mode_used"] == "fallback"         # endpoint will NOT store; returns ok:false


# --- heading restore (the fix: a reworded heading no longer fails the gate) ----------------------
def test_restore_headings_positional_and_count_guard():
    restored = BlogGenerator._restore_headings(
        ["# A", "## B"], "# X reworded\ntext\n## Y reworded\nmore")
    assert restored == "# A\ntext\n## B\nmore"                       # headings put back exactly
    # a genuine section add/drop (count mismatch) → None (caller rejects)
    assert BlogGenerator._restore_headings(["# A", "## B", "## C"], "# X\ntext\n## Y") is None


REWORDED_HEADS_BODY = (
    "# Top widgets for teams\n\n## The short answer\n"
    "Acme is the standout choice for the majority of squads because deployment is rapid and its pricing "
    "sits well under the alternatives [S1], and wiring it into an existing toolchain requires no bespoke "
    "integration work whatsoever [S2].\n\n## How dependable is the Acme widget?\n"
    "Independent stress testing consistently reports solid, reliable uptime across a broad spread of "
    "workloads and sudden traffic spikes [S1].\n"
)


def test_rewrite_reworded_headings_now_succeeds_with_original_headings():
    # The open model reworded the headings (same count) but reworded the prose — this used to FAIL
    # the gate; now the original headings are restored and the rewrite ships.
    gen = _gen(StubWriter(lambda p, i: REWORDED_HEADS_BODY))
    art = {"body_markdown": CLAUDE_BODY}
    out = gen._apply_writer_pass(art, CLAUDE_BODY, {"name": "Acme"}, "best widgets for teams")
    assert art["writer_mode_used"] == "rewrite"                     # shipped, not a fallback
    assert "## Quick answer" in out and "## Is the Acme widget reliable?" in out   # originals restored
    assert "## The short answer" not in out                         # the model's reworded heading is gone
    assert "[S1]" in out and "[S2]" in out


# --- prose-only overlap (structural parts don't count against watermark removal) ----------------
def test_prose_for_overlap_strips_structure():
    body = (
        "# Heading one\n\nReal prose sentence lives here for the metric [S1].\n\n"
        "| Tool | Price |\n|---|---|\n| Acme | $9 |\n\n"
        "## Sources\n- [S1] Acme pricing — https://acme.com/pricing\n"
    )
    prose = BlogGenerator._prose_for_overlap(body)
    assert "Real prose sentence lives here" in prose
    assert "Heading one" not in prose          # heading stripped
    assert "| Acme |" not in prose             # table row stripped
    assert "acme.com/pricing" not in prose     # whole ## Sources section stripped
    assert "[S1]" not in prose                 # inline citation markers stripped


# --- db: the 4 new columns migrate + round-trip (original body untouched) -------------------------
def test_rewritten_columns_migrate_and_roundtrip():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        db = Database(path)
        db.connect()
        db.initialize()
        sub = db.ensure_live_subreddit("test")
        brand_id = db.add_brand(sub["id"], "Test Brand")
        bid = db.save_blog(brand_id=brand_id, seed="s", body_markdown="orig body")
        db.update_blog(bid, rewritten_body="new body", rewritten_overlap=0.04,
                       rewritten_at="2026-09-07T00:00:00Z", rewritten_warning="",
                       rewritten_cost=0.03)
        blog = db.get_blog(bid)
        assert blog["body_markdown"] == "orig body"      # original never touched
        assert blog["rewritten_body"] == "new body"
        assert abs((blog["rewritten_overlap"] or 0) - 0.04) < 1e-6
        assert abs((blog["rewritten_cost"] or 0) - 0.03) < 1e-6   # FU155
        assert blog["rewritten_at"] == "2026-09-07T00:00:00Z"
        db.close()
    finally:
        os.remove(path)
