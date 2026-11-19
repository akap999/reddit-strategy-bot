"""FU298 — the UI was still sending the per-sub cap the backend had already abandoned.

Live Search returned 20 results while the log showed 46 available:

    (arctic 40 sub×term queries) ✓ (+55 passed filters out of 56 raw, 98% pass rate)
    Keyword-presence filter ('direct|booking|str|website|airbnb'): dropped 9, 46 remain
    Computing scrutiny for 2 subreddits...

The last step is `balance_posts_by_subreddit(filtered, limit, subreddits, max_per_sub)`,
and `max_per_sub` caps what ANY single sub may contribute. The UI's "Max / subreddit" input
shipped with value="5", so the ceiling was 5 × (distinct subs) — 10 with two subs, 20 with
four — regardless of the limit of 50 or how many posts were found.

`balance_posts_by_subreddit`'s own docstring records that this was already diagnosed once:

    "Historically this defaulted to 5, which capped multi-sub searches far below the
     requested limit (e.g. 3 subs × 5 = 15 even when 130+ posts were available) — hence
     the default is now None."

The default was changed in the FUNCTION. The input that overrides it was left at 5, so
every search sent the old value explicitly and the fix never took effect. The input already
advertised `placeholder="blank = no cap"`, which is what it now actually does.

Low distinct-sub counts made it bite harder than it used to: Arctic only searches the named
subs, and the Reddit-wide top-up that would discover new ones is returning nothing while
/subreddits/search.json is dead (FU297).
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from search.reddit_bot import balance_posts_by_subreddit  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _html():
    return open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8").read()


def test_the_ui_no_longer_ships_a_hard_cap():
    m = re.search(r'<input id="ls-maxpersub"[^>]*>', _html())
    assert m, "the max-per-sub input is gone"
    tag = m.group(0)
    assert 'value=""' in tag, f"still ships a default cap: {tag}"
    assert "blank = no cap" in tag, "the placeholder no longer describes the behaviour"


def test_a_blank_input_sends_no_cap_at_all():
    """`parseInt('') || undefined` must leave the field OFF the request, so the backend's
    own default (None) applies rather than a coerced 0 or NaN."""
    html = _html()
    assert ("max_per_sub: parseInt(document.getElementById('ls-maxpersub')?.value) || undefined"
            in html)


def test_uncapped_balancing_reaches_the_limit():
    """The behaviour the cap was suppressing: with two subs and 46 posts, a limit of 50 should
    return everything available, not 2 × 5."""
    posts = ([{"id": f"a{i}", "subreddit": "hotels", "score": 100 - i} for i in range(30)]
             + [{"id": f"b{i}", "subreddit": "airbnb", "score": 50 - i} for i in range(16)])
    out = balance_posts_by_subreddit(posts, 50, ["hotels", "airbnb"], max_per_sub=None)
    assert len(out) == 46


def test_the_old_default_is_what_produced_twenty():
    """Pins the arithmetic in the bug report so the regression is recognisable: the cap, not
    the limit and not the filters, is what produced the number the operator saw."""
    posts = ([{"id": f"a{i}", "subreddit": "hotels", "score": 100 - i} for i in range(30)]
             + [{"id": f"b{i}", "subreddit": "airbnb", "score": 50 - i} for i in range(16)])
    capped = balance_posts_by_subreddit(posts, 50, ["hotels", "airbnb"], max_per_sub=5)
    assert len(capped) == 10
    four_subs = posts + [{"id": f"c{i}", "subreddit": "travel", "score": 40 - i} for i in range(10)] \
        + [{"id": f"d{i}", "subreddit": "str", "score": 30 - i} for i in range(10)]
    assert len(balance_posts_by_subreddit(
        four_subs, 50, ["hotels", "airbnb", "travel", "str"], max_per_sub=5)) == 20


def test_the_function_default_is_still_uncapped():
    """Every other test passes max_per_sub explicitly, so reverting the FUNCTION's default
    would slip through — and that default reverting is precisely the regression this file is
    about, one layer down from the UI."""
    posts = ([{"id": f"a{i}", "subreddit": "hotels", "score": 100 - i} for i in range(30)]
             + [{"id": f"b{i}", "subreddit": "airbnb", "score": 50 - i} for i in range(16)])
    out = balance_posts_by_subreddit(posts, 50, ["hotels", "airbnb"])      # no cap argument
    assert len(out) == 46, "the uncapped default no longer reaches the limit"


def test_an_explicit_cap_still_works():
    """Removing the DEFAULT must not remove the feature — typing a number still constrains a
    busy sub, which is what the input is for."""
    posts = ([{"id": f"a{i}", "subreddit": "hotels", "score": 100 - i} for i in range(30)]
             + [{"id": f"b{i}", "subreddit": "airbnb", "score": 50 - i} for i in range(16)])
    out = balance_posts_by_subreddit(posts, 50, ["hotels", "airbnb"], max_per_sub=8)
    assert len(out) == 16
    assert sum(1 for p in out if p["subreddit"] == "hotels") == 8
