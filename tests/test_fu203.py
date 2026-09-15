"""FU203 — a genuinely SHORT (1.5-3 min) YouTube cut.

Three things, and the FIRST committed tests this path has ever had (FU80/FU82/FU97 shipped none):
  1. `duration_min` is a FLOAT clamped 0.5-30, so 1.5 minutes is expressible end-to-end (it used to
     `int()` to 1 in four places).
  2. At <= 3 minutes the prompt carries a real five-beat structure plus the two rules it was missing:
     compress by cutting filler, never content; and the DESCRIPTION-support fields (mini_answer, tags,
     captions, pinned comment) do NOT shrink with the runtime — they are what an engine indexes.
  3. Two deterministic guards for failures that shipped silently: chapter timestamps were never
     validated anywhere (read raw in three places), and nothing ever checked the script against its
     own word budget.

The load-bearing assertion is the INERT PROOF: `duration_min=0` behaves exactly as it does today.
$0, no network (StubClaude).
"""
import os
import tempfile

import pytest

from db import Database
from generators.blog_gen import BlogGenerator
from tests.stubs import StubClaude

BRAND = {"name": "Acme", "domain_url": "https://acme.com"}
ARTICLE = {"title": "Which widget wins?", "body_markdown": "Acme vs Bolt. Acme is $49/mo."}

_N = BlogGenerator._normalise_chapters
_L = BlogGenerator._script_length_note
_TS = BlogGenerator._parse_ts


class _Recording(StubClaude):
    """StubClaude + max_tokens capture — the real client takes it and FU97 scales it with duration."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.max_tokens = []

    def call(self, prompt, max_tokens=1024, max_retries=3, temperature=None, system_prompt=None):
        self.max_tokens.append(max_tokens)
        return super().call(prompt, max_tokens=max_tokens, max_retries=max_retries,
                            temperature=temperature, system_prompt=system_prompt)


def _reply(script=None, chapters=None):
    return {"title": "Which widget wins?", "demo_title": "", "mini_answer": "Acme, then Bolt.",
            "script_markdown": script if script is not None else ("word " * 290),
            "chapters": chapters if chapters is not None else [
                {"question": "What is it?", "ts": "00:00"},
                {"question": "How do they compare?", "ts": "00:40"},
                {"question": "Which one?", "ts": "01:20"}],
            "captions_transcript": "c", "shot_list": ["a"], "thumbnail_text": "Best",
            "cta": "Read the comparison", "pinned_comment": "Details", "tags": ["x"],
            "category": "Education"}


def _run(duration_min=0, reply=None, **kw):
    rep = reply if reply is not None else _reply()
    stub = _Recording(call_handler=lambda p: rep)
    out = BlogGenerator(stub, db=None).generate_youtube_script(
        BRAND, ARTICLE, target_query="which widget wins", duration_min=duration_min, **kw)
    return out, stub


# --- THE INERT PROOF: duration_min=0 is today's behaviour, untouched ------------------------------

def test_a_model_decided_length_gets_no_new_prompt_text_and_the_same_token_cap():
    """`duration_rule` is the only interpolation FU203 changed, and at dm=0 it is still empty — so
    the prompt is byte-identical to today's."""
    out, stub = _run(0)
    p = stub.calls[0]
    for marker in ("TARGET LENGTH", "SHORT-FORM BEAT SHEET", "DENSITY, NOT OMISSION",
                   "THE DESCRIPTION SUPPORT IS NOT COMPRESSED", "CHAPTER COUNT"):
        assert marker not in p, marker
    assert stub.max_tokens[0] == 6000
    assert "duration_min" not in out["meta"]
    assert "length_warning" not in out["meta"]


def test_a_model_decided_length_leaves_even_broken_chapter_timestamps_alone():
    """No target means nothing to check against — the guard must not invent one."""
    out, _ = _run(0, reply=_reply(chapters=[{"question": "a", "ts": "later"},
                                            {"question": "b", "ts": "09:00"}]))
    assert [c["ts"] for c in out["meta"]["chapters"]] == ["later", "09:00"]


# --- Change 1: fractional minutes ----------------------------------------------------------------

def test_one_and_a_half_minutes_survives_and_never_prints_as_one():
    out, stub = _run(1.5)
    assert out["meta"]["duration_min"] == 1.5
    p = stub.calls[0]
    assert "about 1.5 minutes spoken" in p
    assert "218 words" in p                      # round(1.5 * 145), not 145
    assert "about 1 minute" not in p


def test_a_whole_number_still_prints_without_a_decimal_point():
    _, stub = _run(2)
    p = stub.calls[0]
    assert "about 2 minutes spoken" in p and "about 2.0" not in p
    assert "290 words" in p


def test_the_clamp_covers_too_small_too_big_and_garbage():
    assert _run(0.2)[0]["meta"]["duration_min"] == 0.5
    assert _run(99)[0]["meta"]["duration_min"] == 30
    assert "duration_min" not in _run(-3)[0]["meta"]
    assert "duration_min" not in _run("nonsense")[0]["meta"]      # falls back to model-decided


def test_the_token_cap_still_scales_with_a_fractional_duration():
    """FU97's guard: captions duplicate the script, so a fixed 6000 would truncate the JSON."""
    assert _run(1.5)[1].max_tokens[0] == 6000                    # floor
    assert _run(20)[1].max_tokens[0] > 6000


# --- Change 2: a real short-form structure --------------------------------------------------------

def test_a_short_target_gets_the_beat_sheet_and_the_two_rules_that_were_missing():
    p = _run(2)[1].calls[0]
    for s in ("SHORT-FORM BEAT SHEET", "THE LIFTABLE CHUNK", "THE COMPARISON", "HONEST TRADEOFFS",
              "THE RECOMMENDATION", "DENSITY, NOT OMISSION",
              "THE DESCRIPTION SUPPORT IS NOT COMPRESSED", "CHAPTER COUNT: 3-5"):
        assert s in p, s
    # the two rules must say the thing that actually protects retrievability
    assert "NEVER by dropping a named option, a figure, a price or a tradeoff" in p
    assert "the runtime shrinks; the indexed text does not" in p


def test_the_beats_are_scaled_to_the_chosen_duration():
    p90 = _run(1.5)[1].calls[0]
    assert "00:00-00:09" in p90 and "01:19-01:30" in p90
    p120 = _run(2)[1].calls[0]
    assert "00:00-00:12" in p120 and "01:45-02:00" in p120


def test_a_long_target_keeps_todays_rule_verbatim_and_gets_no_beat_sheet():
    p = _run(10)[1].calls[0]
    assert "about 10 minutes spoken" in p and "1450 words" in p
    assert "SHORT-FORM BEAT SHEET" not in p
    assert "tightest viable structure: answer" in p              # today's wording, unchanged


@pytest.mark.parametrize("dm", [0, 1.5, 2, 3, 10])
def test_the_mandatory_package_rules_survive_at_every_duration(dm):
    """FU80/FU82/FU91 regression: length may compress segments, never the purpose."""
    p = _run(dm, geo="the US")[1].calls[0]
    for s in ("ANSWER-FIRST", "SAY THE PROMPT LANGUAGE", "CLAIMS DISCIPLINE", "HONEST-TRADEOFFS",
              "NO MANUFACTURED SOCIAL PROOF", "GEOGRAPHIC FOCUS", "RETRIEVAL ANCHOR"):
        assert s in p, (dm, s)


# --- Change 3a: chapter timestamps ----------------------------------------------------------------

def test_chapters_timed_for_a_longer_video_are_respread_inside_the_target_losing_none():
    """The reported failure shape: a 5-minute chapter list on a 2-minute script."""
    ch = [{"question": "a", "ts": "00:00"}, {"question": "b", "ts": "02:30"},
          {"question": "c", "ts": "05:00"}]
    out = _N(ch, 2)
    secs = [_TS(c["ts"]) for c in out]
    assert len(out) == 3                                    # nothing lost
    assert secs == sorted(secs) and max(secs) <= 120        # monotonic, inside the runtime
    assert [c["question"] for c in out] == ["a", "b", "c"]


def test_a_well_formed_in_range_list_is_returned_untouched():
    ch = [{"question": "a", "ts": "00:00"}, {"question": "b", "ts": "00:45"}]
    assert _N(ch, 2) is ch


def test_an_unparseable_timestamp_is_dropped_and_the_good_ones_keep_their_times():
    ch = [{"question": "a", "ts": "00:00"}, {"question": "b", "ts": "soon"},
          {"question": "c", "ts": "01:00"}]
    out = _N(ch, 2)
    assert [(c["question"], c["ts"]) for c in out] == [("a", "00:00"), ("c", "01:00")]


def test_a_non_monotonic_entry_is_dropped():
    ch = [{"question": "a", "ts": "00:00"}, {"question": "b", "ts": "01:00"},
          {"question": "c", "ts": "00:30"}]
    assert [c["question"] for c in _N(ch, 2)] == ["a", "b"]


def test_when_nothing_parses_every_chapter_is_retimed_rather_than_lost():
    ch = [{"question": "a", "ts": "x"}, {"question": "b", "ts": "y"}]
    out = _N(ch, 2)
    assert [c["question"] for c in out] == ["a", "b"]
    assert [_TS(c["ts"]) for c in out] == [0, 60]


def test_a_question_less_entry_is_dropped_it_is_junk_the_description_already_skips():
    ch = [{"question": "a", "ts": "00:00"}, {"ts": "00:30"}]
    assert len(_N(ch, 2)) == 1


def test_normalising_is_idempotent():
    ch = [{"question": "a", "ts": "00:00"}, {"question": "b", "ts": "04:00"}]
    once = _N(ch, 2)
    assert _N(once, 2) == once


def test_a_zero_duration_is_a_no_op():
    ch = [{"question": "a", "ts": "99:99"}]
    assert _N(ch, 0) is ch


def test_the_corrected_chapters_reach_the_pasted_description_not_just_the_meta():
    """All three consumers read the one stored list, so fixing it here fixes the description too."""
    out, _ = _run(2, reply=_reply(chapters=[{"question": "What is it?", "ts": "00:00"},
                                            {"question": "Which one?", "ts": "05:00"}]))
    assert "05:00" not in out["description"]
    assert all(_TS(c["ts"]) <= 120 for c in out["meta"]["chapters"])


# --- Change 3b: the length check ------------------------------------------------------------------

def test_a_script_that_overshoots_its_budget_is_flagged():
    note = _L("word " * 900, 2)
    assert note and "2 min target" in note and "900 words" in note


def test_an_on_budget_script_is_silent():
    assert _L("word " * 290, 2) == ""


def test_a_fractional_target_reports_itself_as_fractional():
    assert "1.5 min target" in _L("word " * 900, 1.5)


def test_stage_directions_and_headings_are_not_counted_as_spoken():
    spoken = "word " * 200
    padded = "## A segment heading\n[B-roll: the dashboard, 40 seconds of it]\n" + spoken
    assert _L(padded, 1.5) == _L(spoken, 1.5)


def test_no_target_means_no_length_check():
    assert _L("word " * 900, 0) == ""


def test_the_warning_reaches_the_package_meta():
    out, _ = _run(2, reply=_reply(script="word " * 900))
    assert "length_warning" in out["meta"]
    out2, _ = _run(2, reply=_reply(script="word " * 290))
    assert "length_warning" not in out2["meta"]


# --- the export checklist (what the operator actually reads before uploading) ----------------------

@pytest.fixture()
def client_and_blog():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(path)
    db.connect()
    db.initialize()
    sub = db.ensure_live_subreddit("t")
    bid = db.add_brand(sub["id"], "Acme")
    blog_id = db.save_blog(bid, "which widget wins", title="Which widget wins?")
    db.close()

    import app as appmod
    appmod.DB_PATH = path
    appmod._db_initialized = True

    def _set(meta):
        d = Database(path)
        d.connect()
        d.update_blog(blog_id, youtube_title="T", youtube_script="word " * 50,
                      youtube_description="d", youtube_captions="c", youtube_meta=meta)
        d.close()

    yield appmod.app.test_client(), blog_id, _set
    os.unlink(path)


def test_the_export_checklist_prints_a_fractional_target_as_fractional(client_and_blog):
    client, blog_id, _set = client_and_blog
    _set({"chapters": [], "duration_min": 1.5})
    body = client.get(f"/api/blogs/{blog_id}/export?format=youtube").get_data(as_text=True)
    assert "Target length: ~1.5 min" in body


def test_the_export_checklist_prints_a_whole_target_without_a_decimal(client_and_blog):
    client, blog_id, _set = client_and_blog
    _set({"chapters": [], "duration_min": 2.0})
    body = client.get(f"/api/blogs/{blog_id}/export?format=youtube").get_data(as_text=True)
    assert "Target length: ~2 min" in body and "~2.0 min" not in body


def test_the_export_checklist_surfaces_the_length_warning(client_and_blog):
    client, blog_id, _set = client_and_blog
    _set({"chapters": [], "duration_min": 2, "length_warning": "the script runs ~6.2 min"})
    body = client.get(f"/api/blogs/{blog_id}/export?format=youtube").get_data(as_text=True)
    assert "Length check" in body and "the script runs ~6.2 min" in body


def test_an_old_package_without_a_duration_renders_exactly_as_before(client_and_blog):
    client, blog_id, _set = client_and_blog
    _set({"chapters": []})
    body = client.get(f"/api/blogs/{blog_id}/export?format=youtube").get_data(as_text=True)
    assert "Target length" not in body and "Length check" not in body
