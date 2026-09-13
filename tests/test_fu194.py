"""FU194 — two spellings of ONE billing span were separate cadence tags, so respelling failed the gate.

A live rewrite fell back with:

    ⚠ watermark NOT stripped — fell back to Claude
    (price cadence changed: ["$897 lost its cadence (TERM → ['N_WEEK'])"])

Claude's own body wrote the same price BOTH ways: "$897 per 12-week recurring subscription" three
times and "$897 for 12 weeks recurring" once. The rewrite phrased the odd one out like the other
three, which is the correct edit, and the gate called it a lost cadence. Fifth instance of the class
where a spelling difference false-fails a rewrite the meaning gate would pass. $0, no network."""
from generators.blog_gen import BlogGenerator

CLAUDE = ("Noom Med: $149 to get started, then $299/month billed as $897 for 12 weeks recurring "
          "[S15]. The plan is $897 per 12-week recurring subscription [S16].")
REWRITE = ("Noom Med charges $149 up front, then $299/month billed as $897 per 12-week recurring "
           "subscription [S15]. The plan is $897 per 12-week recurring subscription [S16].")


def test_the_reported_failure_now_passes():
    ok, changed = BlogGenerator._price_cadence_ok(CLAUDE, REWRITE)
    assert ok and changed == [], changed


def test_the_two_spellings_are_the_same_cadence():
    for a, b in [("$897 for 12 weeks", "$897 per a 12-week supply"),
                 ("$897 over 12 weeks", "$897 billed as a 12-week plan"),
                 ("$897 for a 12-week supply", "$897 across 12 weeks")]:
        assert BlogGenerator._price_cadence_ok(a, b)[0], (a, b)


def test_dropping_the_span_entirely_still_fails():
    """The gate's actual job. Respelling is free; losing the span is not."""
    for src in ("$897 for 12 weeks", "$897 per 12-week supply", "$897 over a 6-month term"):
        ok, changed = BlogGenerator._price_cadence_ok(src, "$897")
        assert not ok and changed, src


def test_every_other_cadence_is_untouched():
    for a, b, why in [
        ("$149 per month", "$149 per quarter", "month became quarter"),
        ("$149 per month", "$149", "monthly dropped"),
        ("$149 first month then $249 per month", "$149 then $249 per month", "intro tier dropped"),
        ("$99 per seat", "$99", "per-seat dropped"),
        ("$500 annually", "$500", "annual dropped"),
    ]:
        assert not BlogGenerator._price_cadence_ok(a, b)[0], why


def test_a_matching_body_is_still_clean():
    body = ("Plans start at $149 per month, then $249 per month billed quarterly, and the "
            "$897 12-week option covers the full course.")
    assert BlogGenerator._price_cadence_ok(body, body)[0]
