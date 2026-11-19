"""FU300 — subreddits were accepted by the UI and then never searched.

The operator was adding many subreddits and still seeing 10-30 results. The log explains it
in one number, repeated on every single search:

    (arctic 40 sub×term queries) ✓ (+36 passed filters out of 36 raw, 100% pass rate)

Exactly 40, every time — the cap binding, not a coincidence of grid size:

    _ARCTIC_BUDGET = min(len(arctic_subs) * len(arctic_terms), 40)

With 10 keywords that buys 40 ÷ 10 = FOUR subreddits of coverage, however many are added.
The round-robin ordering spreads those 40 pairs fairly across subs and terms, so nothing
looks broken — the extra subs simply never get queried.

The flat 40 was reasonable when it was written: Arctic was one of four legs sharing a 40s
budget, and the comment's rationale ("concurrency is bounded by _arctic_sem(4) regardless,
so a bigger budget only costs latency") held. Arctic is now the only leg, the budget is
configurable and currently 120s, and FU299 gave the leg the whole of it. The cap is what
is left holding coverage down.

It now scales with the time available, bounded by the real grid size and a hard 300 so a
pathological combination still cannot run away. The leg deadline continues to truncate
mid-flight, so this raises a ceiling rather than removing a guard.

I had told the operator the remaining ceiling was a "2-subreddit pool" and that only
BRAVE_API_KEY could lift it. That was wrong, and their pushback is what surfaced this: the
"2 subreddits" in the log is the count of subs PRESENT IN THE RESULTS, not the number
searched.
"""
import os
import sys
import textwrap

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src():
    return open(os.path.join(ROOT, "search", "reddit_bot.py"), encoding="utf-8").read()


def _cap(budget, subs, terms):
    """Evaluate the REAL expression lifted out of reddit_bot.py.

    Reimplementing the formula here would make this file agree with itself rather than with
    the code: dropping the 300 ceiling or the grid-size bound from the source would leave
    every assertion passing. So the two assignment statements are extracted and executed.
    """
    src = _src()
    i = src.rindex("\n", 0, src.index("_pair_cap = max(")) + 1   # from the LINE start, so
    j = src.index("\n", src.index("_pair_cap, 300)", i))         # dedent can normalise it
    snippet = textwrap.dedent(src[i:j])
    scope = {"_budget": budget,
             "arctic_subs": ["s"] * subs,
             "arctic_terms": ["t"] * terms}
    exec(compile(snippet, "<cap>", "exec"), {}, scope)
    return scope["_ARCTIC_BUDGET"]


def test_the_flat_forty_is_gone():
    src = _src()
    assert "min(len(arctic_subs) * len(arctic_terms), 40)" not in src
    assert "_pair_cap = max(40, int(_budget * 1.5))" in src


def test_more_budget_buys_more_coverage():
    """The whole point: the operator raised REDDIT_SEARCH_BUDGET and coverage must follow."""
    assert _cap(40, 50, 10) == 60
    assert _cap(120, 50, 10) == 180
    assert _cap(120, 50, 10) > _cap(40, 50, 10)


def test_it_never_drops_below_the_old_behaviour():
    """A small or unset budget must not make coverage WORSE than the flat 40 it replaces."""
    for budget in (0, 1, 10, 26, 40):
        assert _cap(budget, 50, 10) >= 40


def test_the_real_grid_still_bounds_it():
    """Three subs and two terms is six pairs — never pad to the cap."""
    assert _cap(120, 3, 2) == 6
    assert _cap(120, 1, 1) == 1


def test_a_pathological_grid_still_cannot_run_away():
    """The guard the flat cap existed for has to survive: 200 subs × 50 terms is 10,000 pairs."""
    assert _cap(150, 200, 50) == 225
    assert _cap(10_000, 200, 50) == 300, "no absolute ceiling — a huge budget uncaps the grid"


def test_ten_keywords_now_reach_more_than_four_subs():
    """Pins the operator-visible symptom: with 10 keywords the old cap covered 4 subs."""
    assert _cap(40, 50, 10) // 10 == 6
    assert _cap(120, 50, 10) // 10 == 18


def test_the_deadline_still_truncates_the_grid():
    """Raising the cap is only safe because the leg deadline cancels in-flight pairs — if that
    check went away, a 300-pair grid would run to completion regardless of the budget."""
    src = _src()
    i = src.index("_pair_cap = max(40, int(_budget * 1.5))")
    following = src[i:i + 2500]
    assert "if time.time() > leg_deadline:" in following
    assert "_fut.cancel()" in following
