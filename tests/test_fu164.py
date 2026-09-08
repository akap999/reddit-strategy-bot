"""FU164 — self-hosted writer keep-warm + raised timeout + visibility columns. $0, no network."""
import os
import tempfile

import generators.base as base
from generators.base import WriterClient
from generators.blog_gen import BlogGenerator
from db import Database
from tests.stubs import StubClaude


class _Resp:
    def __init__(self, sc):
        self.status_code = sc


# --- WriterClient.warm (keep-alive ping) ------------------------------------------------------
def test_writer_warm(monkeypatch):
    w = WriterClient("https://x/v1", "k", "m")
    monkeypatch.setattr(base.requests, "post", lambda *a, **k: _Resp(200))
    assert w.warm() == "ok"
    monkeypatch.setattr(base.requests, "post", lambda *a, **k: _Resp(503))
    assert w.warm() == "warming"

    def _raise(*a, **k):
        raise RuntimeError("cold-start / timeout")
    monkeypatch.setattr(base.requests, "post", _raise)
    assert w.warm() == "warming"                 # a boot-triggering timeout is 'warming', never fatal
    assert WriterClient("", "", "").warm() == "error"   # unconfigured → error, never raises


# --- _apply_writer_pass passes the RAISED timeout (600 default, up from 300) -------------------
class _RecWriter:
    def __init__(self, out):
        self.out = out
        self.timeouts = []

    def call_text(self, prompt, system_prompt=None, max_tokens=6000, temperature=0.7, timeout=300):
        self.timeouts.append(timeout)
        return self.out


CLAUDE_BODY = ("# T\n\n## Quick answer\nAcme ships fast and costs less [S1], and connects with no glue [S2].\n\n"
               "## Is it reliable?\nYes, uptime stays high across many workloads and traffic spikes [S1].\n")
REWRITE_BODY = ("# T\n\n## Quick answer\nAcme deploys rapidly and undercuts rivals on price [S1]; it hooks into a stack with zero bespoke plumbing [S2].\n\n"
                "## Is it reliable?\nOutside stress tests keep reporting dependable availability under heavy demand [S1].\n")


def test_writer_pass_passes_raised_timeout():
    w = _RecWriter(REWRITE_BODY)
    gen = BlogGenerator(StubClaude(), db=None, writer=w, writer_mode="rewrite")
    gen._evidence_blocks = [{"label": "x", "url": "https://x", "text": "t"}]
    art = {"body_markdown": CLAUDE_BODY}
    gen._apply_writer_pass(art, CLAUDE_BODY, {"name": "Acme"}, "best widgets")
    assert w.timeouts and w.timeouts[0] == 600       # 300 → 600 (WRITER_CALL_TIMEOUT default)
    assert art.get("writer_mode_used") == "rewrite"  # succeeded (not 'fallback')
    assert isinstance(art.get("writer_secs"), (int, float))


# --- db: writer_secs / writer_mode_used migrate + round-trip ----------------------------------
def test_writer_cols_roundtrip():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        db = Database(path)
        db.connect()
        db.initialize()
        sub = db.ensure_live_subreddit("t")
        bid = db.add_brand(sub["id"], "Acme")
        blog_id = db.save_blog(bid, "seed", "title")
        db.update_blog(blog_id, writer_secs=123.4, writer_mode_used="compose")
        b = db.get_blog(blog_id)
        db.close()
        assert abs((b.get("writer_secs") or 0) - 123.4) < 0.01
        assert b.get("writer_mode_used") == "compose"
    finally:
        os.remove(path)


# --- the app-level keep-warm touch updates activity + starts the loop once --------------------
def test_writer_touch(monkeypatch):
    import app
    monkeypatch.setattr(app, "_writer_warm_loop", lambda: None)   # don't spawn a real pinging loop
    app._writer_warm_started = False
    before = app._writer_last_active
    app._writer_touch()
    assert app._writer_last_active >= before
    assert app._writer_warm_started is True
    app._writer_touch()                                           # idempotent — no second thread flag flip
    assert app._writer_warm_started is True
