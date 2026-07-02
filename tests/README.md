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
