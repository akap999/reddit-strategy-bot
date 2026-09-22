"""FU241 — a walled source is skipped, and a readable source has to actually say it.

Measured on the fourteen sources of one semaglutide safety article, through our own ladder:

    [S 3] LiverTox   readable ONLY via web fetch (24,221 chars) — contains "optic neuropathy",
                     contains no "dose-limiting" anywhere
    [S 8] CF trial protocol        404
    [S10] [S11] PMC reviews        376 chars of error page
    [S12] alcohol trial protocol   138,895 chars, read fine

Every unsupported claim in that article rested on a source nothing had read — a pancreatitis rate
pinned to a 404, a characterisation pinned to a page that returns an error stub — while the one
document that WAS readable, an alcohol-use-disorder protocol, collected the citations instead. And
the one claim whose source read perfectly well ("the NIH LiverTox database specifically lists
fatigue as a dose-limiting side effect") was never checked, because only figures were ever checked
and that sentence has no number in it.

$0, no network.
"""
import pytest

from generators import brand_enrichment as BE
from generators.blog_gen import BlogGenerator
from tests.stubs import StubClaude

# real LiverTox substance: it DOES carry optic neuropathy, it does NOT carry "dose-limiting"
LIVERTOX = ("Semaglutide is a glucagon-like peptide-1 receptor agonist used for type 2 diabetes and "
            "obesity. Side effects include nausea, vomiting, diarrhea, constipation and fatigue. "
            "Rare adverse events include pancreatitis and, with long term use beyond two years, "
            "ischemic optic neuropathy, particularly in persons with diabetes and hypertension. ") * 6


def _gen(pages):
    g = BlogGenerator(StubClaude(), None)
    g._claim_pages = dict(pages)
    return g


BLOCKS = [{"label": "third-party · LiverTox", "url": "https://x/livertox", "text": "LiverTox entry."},
          {"label": "third-party · CFRD protocol", "url": "https://x/protocol", "text": "Protocol."}]
PAGES = {"https://x/livertox": (LIVERTOX, "web fetch"), "https://x/protocol": ("", "not-found")}


# ── a source nothing could read is skipped ───────────────────────────────────────────────────────
def test_a_source_that_404s_is_identified_as_unreadable():
    g = _gen(PAGES)
    body = "Pancreatitis was confirmed in 8 patients (0.27 per 100 patient-years) [S2].\n"
    assert g._probe_cited_sources(body, BLOCKS) == {2}


def test_a_claim_resting_only_on_it_is_removed():
    g = _gen(PAGES)
    body = "Pancreatitis was confirmed in 8 patients (0.27 per 100 patient-years) [S2].\n"
    out, note = g._walled_source_check(body, BLOCKS, {2})
    assert "0.27" not in out
    assert "skipped 1 source" in note


def test_the_skipped_source_stops_being_cited():
    """It should not sit in the Sources list advertising a page the article never opened."""
    g = _gen(PAGES)
    body = "Semaglutide is prescribed under supervision [S2].\n"
    out, _ = g._walled_source_check(body, BLOCKS, {2})
    assert "[S2]" not in out
    assert "Semaglutide is prescribed under supervision" in out, "the prose itself is not the problem"


def test_a_readable_source_is_not_skipped():
    g = _gen(PAGES)
    body = "LiverTox identifies ischemic optic neuropathy as a rare risk of long term use [S1].\n"
    assert g._probe_cited_sources(body, BLOCKS) == set()
    out, note = g._walled_source_check(body, BLOCKS, set())
    assert "optic neuropathy" in out and not note


# ── a readable source has to contain what the sentence says it contains ──────────────────────────
def test_the_dose_limiting_claim_is_removed():
    """LiverTox reads fine at 24,221 characters and the phrase is not in it."""
    g = _gen(PAGES)
    body = "The NIH LiverTox database specifically lists fatigue as a dose-limiting side effect [S1].\n"
    out, note = g._walled_source_check(body, BLOCKS, set())
    assert "dose-limiting" not in out
    assert "does not say" in note


def test_the_claim_its_source_DOES_carry_survives():
    """The same shape of sentence, the same source — kept, because the label really is in there.
    Without this the check is just a sentence shredder."""
    g = _gen(PAGES)
    body = "LiverTox identifies the risk as ischemic optic neuropathy in long term use [S1].\n"
    out, _ = g._walled_source_check(body, BLOCKS, set())
    assert "ischemic optic neuropathy" in out


def test_an_unreadable_source_is_never_judged_on_what_it_says():
    """Absence of a phrase from a page we could not open proves nothing — that is the WALLED rule's
    job, not this one's."""
    g = _gen(PAGES)
    body = "The protocol classifies the event as a serious adverse reaction [S2].\n"
    out, note = g._walled_source_check(body, BLOCKS, set())     # not reported walled here
    assert "serious adverse reaction" in out and not note


def test_ordinary_prose_is_untouched():
    g = _gen(PAGES)
    body = "Semaglutide is considered broadly safe under medical supervision [S1].\n"
    out, note = g._walled_source_check(body, BLOCKS, set())
    assert out == body and not note


def test_a_body_needing_mass_deletion_is_left_alone():
    g = _gen(PAGES)
    body = "\n\n".join(f"Finding {i}: the rate was {i}.{i}% at 12 months [S2]." for i in range(1, 12))
    out, note = g._walled_source_check(body, BLOCKS, {2})
    assert out == body and "too many to remove safely" in note


def test_the_sources_section_is_never_edited():
    g = _gen(PAGES)
    body = ("A claim [S1].\n\n## Sources\n\n- [S2] protocol — https://x/protocol\n")
    out, _ = g._walled_source_check(body, BLOCKS, {2})
    assert "- [S2] protocol" in out


# ── only sources carrying a SPECIFIC are probed ──────────────────────────────────────────────────
def test_every_cited_source_is_probed_including_a_general_statement():
    """FU266 — this used to assert the opposite: a citation backing a general statement was NOT
    probed, on the reasoning that its snippet proves the source exists and is on topic.

    Measured on a reported article, that reasoning cost 2 of 20 cited sources a read, and what was
    stored for them was a ~230-character search snippet. The operator's rule is that a page
    unreachable by every method is not a source at all — and that cannot be applied to a page
    nobody tried to open. `[S2]` here 404s, so probing it is what finds that out."""
    g = _gen(PAGES)
    body = "Semaglutide is prescribed under medical supervision [S1][S2].\n"
    assert g._probe_cited_sources(body, BLOCKS) == {2}, "the 404 is found; the readable one is not"


# ── the host wall: one challenge is a strike, not a conviction ────────────────────────────────────
def test_one_challenge_does_not_wall_the_whole_host(monkeypatch):
    """NCBI's block is rate-based: the same PMC article read fine on one run and challenged on the
    next. One challenge used to disable direct fetching of the host for thirty minutes, so the next
    page — readable at that moment — was recorded as walled too."""
    BE.forget_walled_domains()
    monkeypatch.setattr(BE, "_mark_walled", BE._mark_walled)
    BE._mark_walled("https://ncbi.nlm.nih.gov/a")
    assert not BE._walled("https://ncbi.nlm.nih.gov/b"), "the second page still gets a look"
    assert BE._soft_walled("https://ncbi.nlm.nih.gov/b")
    BE._mark_walled("https://ncbi.nlm.nih.gov/b")
    assert BE._walled("https://ncbi.nlm.nih.gov/c"), "twice walled is walled"
    BE.forget_walled_domains()


def test_the_same_page_twice_is_one_strike(monkeypatch):
    """Retrying one URL must not convict the host on its own."""
    BE.forget_walled_domains()
    BE._mark_walled("https://ncbi.nlm.nih.gov/a")
    BE._mark_walled("https://ncbi.nlm.nih.gov/a")
    assert not BE._walled("https://ncbi.nlm.nih.gov/b")
    BE.forget_walled_domains()
