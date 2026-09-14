"""FU197 — a blog must never point at a sibling that was never published, plus two Drive trims.

The operator found a generated blog saying "PeterMD has published a dedicated guide on muscle
preservation on tirzepatide" and "refer to PeterMD's published guidance on what labs to get before
starting a GLP-1". Both guides are blogs we GENERATED but never published, so the article sent the
reader (and the answer engine) to a page that exists at no URL.

Root cause: the sibling context list was built from every blog row, drafts included, and the prompt
told the writer to "defer the depth to it". Their rule: only refer or link to assets that already
exist and have hosted links.

Also here: the Drive doc drops the "(watermark-free)" suffix, and the Docs surface drops the `---`
section dividers (the stored markdown and the web export keep them). $0, no network.
"""
import os
import tempfile

import pytest

from db import Database
from generators.blog_gen import BlogGenerator
from tests.stubs import StubClaude

LIVE = {"title": "Muscle preservation on tirzepatide",
        "meta_description": "How to keep lean mass.",
        "url": "https://petermd.com/muscle-guide"}
DRAFT_TITLE = "What labs to get before starting a GLP-1"

_ART = {"title": "T", "meta_description": "m", "keywords": [], "body_markdown": "## Quick answer\nx",
        "disclosure": "d"}


def _capture(sibling_titles):
    """Run generate_article and hand back the prompt the writer actually saw."""
    stub = StubClaude(call_handler=lambda p: _ART)
    gen = BlogGenerator(stub, db=None)
    gen.generate_article({"name": "PeterMD", "domain_url": "https://petermd.com"},
                         "can you take trt and a glp-1", sibling_titles=sibling_titles)
    return stub.calls[0]


# ── the reported bug, locked ─────────────────────────────────────────────────────────────────
def test_a_published_sibling_reaches_the_prompt_with_its_live_url():
    p = _capture([LIVE])
    assert LIVE["title"] in p
    assert LIVE["url"] in p, "the writer must be given the URL it is allowed to point at"


def test_the_prompt_no_longer_licenses_deferring_to_another_page():
    """This clause is what produced 'PeterMD has published a dedicated guide on ...'."""
    p = _capture([LIVE])
    assert "defer the depth to it" not in p


def test_the_prompt_forbids_promising_an_unlisted_page():
    p = _capture([LIVE])
    assert "REFERENCE RULE" in p
    assert "NEVER state or imply" in p
    assert "only by the exact URL shown" in p


def test_no_published_siblings_means_no_sibling_block_at_all():
    p = _capture([])
    assert "REFERENCE RULE" not in p and "ALREADY LIVE" not in p


# ── the app-side filter: what counts as "already exists and has a hosted link" ────────────────
def test_only_a_published_website_platform_counts_as_live():
    import app as appmod
    f = appmod._blog_published_url
    assert f({"platforms": [{"platform": "website", "published_url": "https://x.com/a"}]}) == "https://x.com/a"
    assert f({"platforms": [{"platform": "website", "published_url": "  "}]}) == ""   # generated, unpublished
    assert f({"platforms": [{"platform": "linkedin", "published_url": "https://li/x"}]}) == ""
    assert f({"platforms": []}) == "" and f({}) == "" and f(None) == ""


# ── the reconcile is the last writer of the body ──────────────────────────────────────────────
def test_the_reconcile_carries_the_same_rule():
    src = open("generators/blog_gen.py", encoding="utf-8").read()
    i = src.index("def _reconcile_and_finish")
    j = src.index("\n    def ", i + 10)
    block = src[i:j]
    assert "NO PHANTOM SELF-REFERENCE" in block
    assert "DELETE such a sentence" in block


# ── the deterministic backstop (warning only, never rewrites) ────────────────────────────────
def _note(body, live=("https://petermd.com/muscle-guide",)):
    gen = BlogGenerator(StubClaude(), db=None)
    gen._sibling_urls = {u.rstrip("/").lower() for u in live}
    return gen._self_reference_note(body, {"name": "PeterMD"})


@pytest.mark.parametrize("body", [
    "PeterMD has published a dedicated guide on muscle preservation on tirzepatide.",
    "For a detailed breakdown, refer to PeterMD's published guidance on what labs to get.",
    "Read our guide on tirzepatide dosing.",
    "See [our muscle-preservation guide](https://petermd.com/never-published) for the protocol.",
])
def test_a_dangling_promise_is_flagged(body):
    assert _note(body).startswith("self-reference:")


@pytest.mark.parametrize("body", [
    "See [our muscle guide](https://petermd.com/muscle-guide) for the protocol.",
    "PeterMD offers injectable and oral TRT through a fully online platform.",
    "The FDA published a guide on testosterone labelling.",
    "Our clinicians review your labs before prescribing.",
])
def test_clean_prose_is_silent(body):
    assert _note(body) == ""


def test_the_note_never_rewrites_the_body():
    gen = BlogGenerator(StubClaude(), db=None)
    gen._sibling_urls = set()
    body = "PeterMD has published a dedicated guide on X."
    gen._self_reference_note(body, {"name": "PeterMD"})
    assert body == "PeterMD has published a dedicated guide on X."


# ── the Docs surface: no dividers ────────────────────────────────────────────────────────────
DIVIDED = "# Which Agency?\n\nFirst section.\n\n---\n\n## Second\n\nSecond section.\n"


@pytest.fixture()
def env(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(path)
    db.connect()
    db.initialize()
    sub = db.ensure_live_subreddit("t")
    brand_id = db.add_brand(sub["id"], "Acme")
    blog_id = db.save_blog(brand_id, "which agency", title="Which Agency?", body_markdown=DIVIDED)
    db.meta_set("gdoc_script_url", "https://script.example/exec")
    db.meta_set("gdoc_secret", "s3cret")
    db.close()

    import app as appmod
    appmod.DB_PATH = path
    appmod._db_initialized = True
    sent = {}

    class _Resp:
        status_code = 200
        text = '{"ok":true,"url":"u"}'

        def json(self):
            return {"ok": True, "url": "https://docs.google.com/document/d/abc"}

    def _fake_post(url, json=None, timeout=None, **kw):
        sent.clear()
        sent.update(json or {})
        return _Resp()

    import requests
    monkeypatch.setattr(requests, "post", _fake_post)
    yield appmod.app.test_client(), blog_id, sent
    os.unlink(path)


def test_the_drive_doc_has_no_section_dividers(env):
    client, blog_id, sent = env
    r = client.post(f"/api/blogs/{blog_id}/upload-gdoc", json={})
    assert r.status_code == 200, r.get_json()
    assert "<hr" not in sent["html"]
    assert "Second section." in sent["html"], "only the rule is dropped, never the content"


def test_the_web_and_markdown_exports_keep_their_dividers(env):
    """Operator chose 'Docs surface only' — FU191's thematic-break handling stays intact."""
    client, blog_id, sent = env
    assert "<hr" in client.get(f"/api/blogs/{blog_id}/export?format=html").get_data(as_text=True)
    assert "---" in client.get(f"/api/blogs/{blog_id}/export?format=md").get_data(as_text=True)


def test_the_drive_doc_is_named_with_the_plain_title(env):
    client, blog_id, sent = env
    client.post(f"/api/blogs/{blog_id}/upload-gdoc", json={"use": "rewritten"})
    assert "(watermark-free)" not in sent["title"]
    assert sent["title"] == "Which Agency?"


def test_both_sibling_collection_sites_gate_on_the_published_url():
    """The prompt half is tested above; this is the wiring half — generate AND regenerate must
    filter the list with `_blog_published_url`, or an unpublished draft reaches the writer again."""
    src = open("app.py", encoding="utf-8").read()
    for var in ("sibling_titles = [", "sib_titles = ["):
        i = src.index(var)
        block = src[i:src.index("][:10]", i)]
        assert "_blog_published_url(b)" in block, f"{var} is not filtered to published blogs"
