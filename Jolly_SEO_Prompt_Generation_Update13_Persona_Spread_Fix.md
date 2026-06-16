# Jolly SEO — Prompt Generation Update #13: Persona→Region Spread Fix (cap restored, fit-scoped)

**Build specification (standalone) — thirteenth deliverable.** A correctness fix to persona→region
assignment. Same stack. **No schema/UI change.** Touches `generators/post_gen.py`
(`_assign_personas_to_regions`).

---

## Why (a fresh cluster assigned ONE persona to every region)

Update #12 made assignment *fit-respecting* (a region only ever gets a genuinely-fitting persona) and
**removed the old per-persona cap** — because that cap backfilled from ALL personas and so spilled a
budget region onto a luxury persona ("affordable…under $1000" → "luxury comfort seeker").

The #12 replacement asked the LLM, per region, for *"ONLY the personas that GENUINELY fit"* and then
spread with a least-used pick. In production (Amerisleep, 4+ diverse personas) the LLM read "only
genuine fits" strictly and returned the **single dominant persona per region** → each region's
candidate list was one label, the same one → the least-used spread had nothing to distribute →
**every region got the same persona.**

So the two failure modes are mirror images: a *forced full ranking* → wrong matches; *single-best
only* → collapse. The fix is the middle ground + restoring a spread guarantee that can't misfire.

---

## The fix (`_assign_personas_to_regions`)

1. **Ranker returns DIVERSE candidates, not single-best.** The prompt now asks each region to RANK the
   personas that *plausibly* fit (best→worst, usually 2–4), **EXCLUDING only clear price-tier/intent
   mismatches** (budget region never lists a premium persona, and vice-versa), empty only when truly
   none fit. Added an explicit goal line: different regions should be covered by different personas, so
   include each region's genuine + secondary fits — don't return the same single persona for every
   region. (Breadth comes from the candidate lists; the spread itself stays in code.)

2. **Per-persona cap restored — but FIT-SCOPED.** `cap = max(1, ceil(regions / fit_personas))`. For
   each region (candidates ranked): take the first candidate still under the cap; if ALL of that
   region's candidates are capped, fall back to the region's **own least-used candidate** (exceed the
   cap rather than ever assign outside its fit list). This is the spread guarantee the old cap gave —
   without the all-persona backfill that caused the luxury bug: a capped region only moves to ANOTHER
   of ITS genuine fits, never a wrong one.

3. **Tolerant result parsing** (`_parse_region_persona_ranking`). Normalizes list-of-lists,
   list-of-objects (`{region, personas|fits|ranked}`), and dict-keyed-by-region into per-region label
   lists. Prevents a model JSON-shape change (we just moved to Sonnet 4.6) from silently emptying every
   region → mass persona-growth/collapse.

The no-fit → grow-a-new-persona path (Update #12) is unchanged.

## Behavior
- Diverse regions now spread across the brand's diverse personas; no single persona dominates beyond
  `ceil(regions/personas)` unless a region genuinely has only one fitting persona.
- Price-tier safety preserved: an "affordable / under $X" region is never assigned a premium persona
  (it's excluded from that region's candidate list).

## Verification
Unit tests (temp, monkeypatched ranker): (1) 4 diverse regions → 4 distinct correct personas,
affordable→budget; (2) all regions ranking the same persona first → cap breaks the collapse into a
spread; (3) list/object/dict/garbage shapes all parse; (4) affordable region with only a budget fit
never receives luxury. `ast.parse` clean. **PASS.**
