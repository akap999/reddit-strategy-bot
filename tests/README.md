# Offline blog tests ($0)

These tests exist so blog-pipeline changes can be validated **without a live blog generation**
(a real gen costs ~$2 in web-search + LLM tokens and used to be the only way regressions surfaced).
They pin the FU54 guarantees that stop quality declining round-over-round.

## Run

```bash
python3 -m pytest tests/ -q
```

(`python3 -m pytest` puts the repo root on `sys.path`; `tests/conftest.py` also handles a bare `pytest`.)

## What they cover (all deterministic, no network)

- **`test_blog_guards.py`** (Tier 0 — pure functions):
  - substance guard restores a dropped `##`/`###` section and flags a dropped stat;
  - `_rebuild_sources` force-keeps an authoritative `official ·` source (and a community/Reddit source)
    in `## Sources` even when uncited, but drops an uncited third-party review;
  - `build_blog_jsonld` excludes brand-promo FAQ questions from FAQPage schema (asserted on v8's exact
    FAQ set — proving that schema regression can't ship again);
  - `_norm_domain` normalizes scheme/path/`www.` so vendor pages match their resolved domain;
  - `usage_cost()` cost math;
  - a **golden anchor** that loads the real v8 blog from `strategy_bot.db` (skipped if absent) and asserts
    no brand-promo FAQ leaks into its schema.

- **`test_blog_pipeline_stub.py`** (Tier 1 — the later pipeline with a stubbed LLM via `stubs.StubClaude`):
  reproduces v8's failure (the reconcile drops the core-topic section) and asserts that after the substance
  guard + `_rebuild_sources` the section is restored, the competitor is sourced from its own vendor page,
  and the authoritative `official ·` source survives into `## Sources`.

## Cost tiers (why this is the gate, not a live gen)

| Tier | What | Cost |
|------|------|------|
| 0 | pure functions (`_restore_dropped_sections`, `_rebuild_sources`, `build_blog_jsonld`, `_norm_domain`, `usage_cost`) | $0 |
| 1 | later pipeline with a stubbed `ClaudeClient` | $0 |
| — | a full live `generate_blog` | ~$2 |

**Rule:** only do a live gen AFTER `python3 -m pytest tests/` is green — the suite, not a paid generation,
is the regression gate.

## Standing rule (FU204): assert against a REAL exported body, not only a hand-written string

FU204 shipped four defects through a green 588-test suite. The reason was the same every time: the
tests measured what the author imagined the body looked like, not what the pipeline produces.

- FU202's `marker-space` rule had **28 tests**, all over synthetic inputs (`-item`, `1.item`). Not one
  ran the formatter over a real article body — every one of which begins with FU152's
  `*[Add author byline before publishing]*`. The rule rewrote that byline into a bullet on **every blog**
  and the suite never noticed.
- FU185's table fixture was built around the old ">50% empty" column rule, so it silently encoded a
  threshold rather than a guarantee.
- `v9_blog.html` was committed by FU54 as a permanent golden anchor and then never added to, so every
  later round verified itself against fixtures written by whoever wrote the code under test.

**So: every formatting, scrub, citation or table rule must be asserted against a real exported body.**
The anchors are `tests/fixtures/v9_blog.html` and `tests/fixtures/thyseed_blog.html` (see
`test_v9_fixture.py`, `test_fu204_golden.py`). Add to them; do not let them go stale.

The specific traps a synthetic fixture will not contain:

1. **Every article starts with an italic line.** FU152 prepends the byline placeholder, and FU84 can add
   `*Reviewed by …*` and `*Disclosure: …*`. Any rule that reads a leading `*` must be tested against them.
2. **A new rule reverses old fixtures on purpose.** When it does, update the assertion WITH a comment
   saying which guarantee changed and where the old one is still covered — never loosen it to pass.
3. **Demonstrate the ordering.** A regression test must FAIL on the code as it was and PASS after. Prove
   it (`git stash` the source changes, run the new test, restore) rather than asserting it. FU204's
   suite went 21 failed / 4 passed → 25 passed.
