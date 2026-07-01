"""Post generation with storyline balancing, AI-query scoring, image strategy, and scheduling."""

import random
import math
import json
import requests

from generators.base import ClaudeClient, BANNED_PHRASES
from generators.comment_gen import classify_post_intent
from config import (
    STORYLINE_TYPES, AI_QUERY_PATTERNS, PROMPT_VERSION,
    POST_SPREAD_FACTOR, FILLER_LEAD_DAYS, REDDIT_USER_AGENT,
    POST_BATCH_SIZES, INTENT_TYPES,
    OPENAI_API_KEY, EMBED_MODEL, EMBED_THRESHOLD,
)
from db import Database

# The 6 standard recommendation-query axes (the "fixed regions") — the recurring
# dimensions of "recommend me an X" queries, marked so the user can prioritize them.
# Claude instantiates the ones that apply to a brand/seed and may add brand-specific
# regions on top.
FIXED_REGIONS = [
    "Category / best tool",
    "Comparison / alternative",
    "Constraint",            # the dominant buying constraint (copyright, budget, compliance…)
    "Use-case / workflow",
    "Persona / segment",
    "Adjacent platform",
]

# Hard ceiling on fan-out rewrites (one per region) — guarantees the cluster stays
# sparse + region-unique regardless of what the model returns.
MAX_FANOUT_REWRITES = 10

# Generic region labels that add no distinguishing qualifier beyond the rewrite query
# itself (bare axis names + placeholders). For these the query is authoritative, so we
# do NOT enforce region fidelity. A brand-specific DESCRIPTIVE region (e.g. "agency
# offering a free AI visibility audit") IS meaningful — for those we require the title
# to keep the region's qualifier so it doesn't collapse onto a sibling region.
_GENERIC_REGION_LABELS = {
    "category / best tool", "category / top pick", "comparison / alternative",
    "constraint", "use-case / workflow", "use case / workflow",
    "persona / segment", "adjacent platform",
}


def _is_meaningful_region(region):
    """True when a region label carries its own distinguishing intent (worth enforcing
    on the title), False for placeholders ("(unsorted)", "(from posts)", …) and bare
    axis names where the rewrite query already carries the specificity."""
    r = (region or "").strip()
    if not r or r.startswith("("):
        return False
    return r.lower() not in _GENERIC_REGION_LABELS

# Shared BODY composition rules — used by both the batch generator
# (_generate_candidates_for_intent) and the single-post regenerator (regenerate_body)
# so they never drift. The headline rule is ASK ONCE: a real person states the request
# in one sentence and spends the rest giving context. Retrieval coverage comes from
# concrete detail (each key term appearing once), NOT from re-asking the question in
# several phrasings (which reads spammy / AI-generated and hurts Reddit survival).
_BODY_GUIDANCE = """  • BODY — 2-4 short first-person paragraphs that pay off the title. ASK THE QUESTION
    EXACTLY ONCE: state the request in a single natural sentence, then spend the rest
    giving real context — the situation, constraints and specifics behind it. Do NOT
    re-ask or rephrase the same question multiple times; a real person asks once.
  • Coverage for LLM retrieval comes from CONCRETE DETAIL, not repetition: let the
    brand's category, audience, pain-points and use-cases surface ONCE EACH as the user
    describes their situation (specific numbers, time periods, context add authenticity
    and keyword overlap) — never as restated versions of the ask.
  • Conversational imperfections: occasional typos, incomplete thoughts, run-on
    sentences. It should sound like a real person asking, not pitching.
  • Do NOT look AI-generated. No marketing language, no excessive formatting, no emoji,
    no dashes. Never mention the target brand name in the body."""


# General ("natural human") style overlay — injected only when the opt-in `general`
# mode is on. It OVERRIDES voice/phrasing only; every rule above (recommendation
# question, target-prompt match, no target-brand) still fully applies. Deliberately
# principles + negatives ONLY — NO example title/body strings, so the model can't
# clone one shape.
_GENERAL_STYLE_BLOCK = """
GENERAL MODE — write like a REAL PERSON in a real thread, not a marketer. This is the
#1 lever for passing Reddit's spam/shill filters while staying just as retrievable for
the target query. It changes ONLY voice/phrasing; every rule above still applies (still
a recommendation question, still strongly matches the target prompt, still never names
the target brand).
  • TITLE — obey the title rules above, but phrase it the messy way a person types in a
    hurry, NEVER like a marketer's headline. Do NOT stack capabilities or write
    "all-in-one" titles (never ask which tool "does X AND Y together in one place",
    never cram 2-3 features/use-cases into one title) — ask about the ONE core need.
    Avoid landing-page / SEO / H1 framing and flawless Title Case; lowercase run-ons,
    sentence fragments and mild imperfection read as authentic. STILL name the exact
    category / need the query is about so the post stays retrievable for it.
  • BODY — a natural first-person story from someone actually in the situation. It MUST
    still contain ALL the relevant domain keywords / category nouns / use-case terms /
    concrete specifics (that is what makes it retrievable + citable by AI) — but woven
    into how the person explains their problem, NOT as a feature/spec list. In the post
    itself do NOT: name a specific product/tool/brand, enumerate an idealized feature
    bundle, or use overt buy-intent phrasing ("paid tier", "one subscription", "no
    watermark", "credit system"). End by genuinely asking what people use / recommend.
  • Do NOT reuse any fixed phrasing or structure; vary naturally across the batch.
"""


class PostGenerator:
    def __init__(self, claude: ClaudeClient, db: Database):
        self.claude = claude
        self.db = db
        self.last_coverage = None  # AI-Search gap-fill coverage summary (per run)

    def generate_posts(self, subreddit, brands, count=None, custom_topics=None,
                       intent_counts=None, context_only=False, seed=None,
                       ai_search=False, observed_queries=None, target_rewrites=None,
                       follow_persona=False, persona=None, general=False):
        """Generate GEO-style posts (posts NEVER mention target brands).

        `ai_search` (optional, default False): the new AI-Search semantic-coverage
        MODE. When True, runs ONE query fan-out pass (`_fanout_rewrites`) to derive
        the union rewrite cluster + a concept/phrasing checklist for the brand
        (optionally anchored on `seed`), then steers title/body generation to
        collectively cover that cluster. Posts are tagged
        `prompt_version=<v>-ai-search` and persist the checklist so the later HQ
        anchor can cover the whole cluster. ai_search=False is the unchanged
        standard path.

        `seed` (optional): an existing prompt/question, several of them, or a
        keyword/platform (e.g. "Instagram"). When set, every generated title is
        an expansion AROUND that seed's theme/platform — sibling recommendation
        questions that strengthen coverage of it — while still obeying all the
        normal title/brand rules. Threaded into `_generate_candidates_for_intent`
        as `seed_focus`.

        Two ways to size the batch:
          - `intent_counts`: an explicit per-intent map, e.g.
            {"commercial": 2, "comparison": 0, "informational": 3}. Generates
            exactly that many of each intent (the flexible mode used by the
            Live Subreddits generator). Intents with 0 are skipped.
          - `count`: legacy strict 1:1:1 batch — must be in POST_BATCH_SIZES
            (3, 6, 9). Each group of 3 = 1 commercial + 1 comparison + 1
            informational.

        Each post is written as a long-tail AI-model query a real user would
        type into ChatGPT/Perplexity. Competitor brand names ARE allowed in
        comparison posts; target brand names are never allowed.

        Args:
            subreddit: subreddit dict from DB. With `context_only=True` this is
                the SAVE TARGET only (e.g. the "unassigned" pool) — its
                name/domain are NOT injected into the prompt; posts are grounded
                purely in brand context so a real subreddit can be assigned
                later.
            brands: single brand dict OR list of brand dicts (never mentioned)
            count: legacy batch size (POST_BATCH_SIZES) — ignored if
                `intent_counts` is given
            custom_topics: optional list of custom title/topic strings
            intent_counts: optional {intent: n} map for flexible per-intent sizing
            context_only: when True, generate from brand context without scoping
                the prompt to a specific subreddit, and instruct the batch to
                collectively cover ALL the brand's offerings.

        Returns:
            list of saved post dicts with IDs
        """
        # Build the per-intent generation plan: [(intent, n), ...].
        if intent_counts:
            plan = []
            for it in INTENT_TYPES:
                try:
                    n = int(intent_counts.get(it, 0))
                except (TypeError, ValueError):
                    n = 0
                if n > 0:
                    plan.append((it, n))
            if not plan:
                raise ValueError("intent_counts must request at least one post")
        else:
            if count not in POST_BATCH_SIZES:
                raise ValueError(
                    f"count must be one of {POST_BATCH_SIZES}, got {count}. "
                    "GEO batches are strict 1:1:1 commercial/comparison/informational."
                )
            per_intent = count // 3  # 1, 2, or 3
            plan = [(it, per_intent) for it in INTENT_TYPES]

        # Normalize: accept single brand or list
        if isinstance(brands, dict):
            brands = [brands]

        brand_ids = [b["id"] for b in brands]
        primary_brand = brands[0]

        # AI-Search mode: build/reuse the fan-out cluster.
        #  - With a SEED: persist a STABLE cluster per (brand, seed) and only target
        #    the UNCOVERED gaps each run, so "generate more" completes the cluster
        #    instead of repeating. Exact X/N coverage.
        #  - Without a seed: today's behavior (full fan-out, no gap tracking).
        coverage_focus = None
        region_by_query = {}           # canonical rewrite query (lower) -> its region label
        checklist_json = None
        self.last_coverage = None
        remaining_gaps = None          # None => not gap-filling
        cluster_size = 0
        covered_before = 0
        # Persona lens (AI-Search only): ensure the brand has auto-generated personas
        # before the fan-out so it can ground/filter the regions. Cached + graceful.
        if ai_search:
            self._ensure_personas(brands)
        # Resolve a picked persona for the DIRECT (non-cluster) path into its full dict,
        # so its detail can ground the post writing when follow_persona is ON. Accepts a
        # dict, a label, or an index into the brand's fit personas. None for cluster mode
        # (cluster posts use their per-region assigned persona, not a single override).
        persona_override = None
        if follow_persona and persona not in (None, "", "(broad)") and not ai_search:
            if isinstance(persona, dict):
                persona_override = persona
            else:
                _pmap = self._persona_detail_map(brands)
                _key = str(persona).strip().lower()
                if _key in _pmap:
                    persona_override = _pmap[_key]
                else:
                    try:
                        persona_override = self._fit_personas(brands)[int(persona)]
                    except (ValueError, TypeError, IndexError):
                        persona_override = None
        seed_norm = self.db.normalize_seed(seed) if (ai_search and seed) else None
        if ai_search and seed_norm:
            bid = primary_brand["id"]
            # Anchor-scoped grounding: learn what THIS brand offers for this seed's
            # topic (from the brand's own site), persist it into the brand's
            # accumulating learned_context, and feed it into the fan-out + post
            # prompts below. Cached per (brand, seed) → only fetched on a NEW anchor.
            anchor_summary = self._ground_brand_for_anchor(brands, seed, seed_norm)
            # Everything already generated for this (brand, seed) — past posts count
            # as covered regardless of when they were made.
            covered = self.db.get_covered_target_queries(bid, seed_norm)
            cluster = self.db.get_ai_search_cluster(bid, seed_norm)
            rewrites = None        # list of region objects {query, region, source}
            anchor = checklist = None
            if cluster and not cluster.get("backfilled"):
                # A real cluster exists → reuse it (history already credited via covered).
                rewrites = self.db.normalize_rewrites(cluster.get("rewrites_json"))
                checklist = json.loads(cluster.get("checklist_json") or "[]")
                anchor = cluster.get("anchor") or None
                if observed_queries:
                    rewrites = self._merge_observed(rewrites, observed_queries)["rewrites"]
                    self.db.upsert_ai_search_cluster(bid, seed_norm, seed, anchor, rewrites, checklist, backfilled=0)
            else:
                # No cluster, OR a backfilled (covered-only) one → BUILD/UPGRADE it so
                # this run folds in the existing posts AND extends with new angles.
                past_posts = self.db.get_ai_search_posts_for_seed(bid, seed_norm)
                past_angles, seen_pa = [], set()
                for p in past_posts:
                    tq = (p.get("target_query") or "").strip()
                    if tq and tq.lower() not in seen_pa:
                        seen_pa.add(tq.lower()); past_angles.append(tq)
                fan = self._fanout_rewrites(brands, seed, prior_coverage=past_angles,
                                            anchor_summary=anchor_summary)
                if not fan and not past_angles:
                    fan = self._fanout_rewrites(brands, seed, anchor_summary=anchor_summary)  # nothing prior → plain fan-out
                anchor = (fan.get("anchor") if fan else None) or (cluster.get("anchor") if cluster else None)
                checklist = (fan.get("checklist") if fan else None) or (json.loads(cluster.get("checklist_json") or "[]") if cluster else [])
                # Past angles FIRST (so they're in the cluster + show covered), then the
                # new fan-out region objects (the gaps to fill). Dedup by query.
                merged, seen_m = [], set()
                for tq in past_angles:
                    if tq.lower() not in seen_m:
                        seen_m.add(tq.lower())
                        merged.append({"query": tq, "region": "(from posts)", "source": "manual"})
                for r in ((fan.get("rewrites") if fan else []) or []):
                    q = (r.get("query") if isinstance(r, dict) else str(r)).strip()
                    if q and q.lower() not in seen_m:
                        seen_m.add(q.lower())
                        merged.append(r if isinstance(r, dict) else {"query": q, "region": "(unsorted)", "source": "generated"})
                rewrites = merged
                if observed_queries:
                    rewrites = self._merge_observed(rewrites, observed_queries)["rewrites"]
                if rewrites:
                    self.db.upsert_ai_search_cluster(
                        bid, seed_norm, seed, anchor, rewrites, checklist, backfilled=0)
            if rewrites:
                rewrite_queries = [r["query"] for r in rewrites]
                # Per-region phrasings (body-side retrieval) + persona (attribution),
                # keyed by the rewrite query so generation/saving can look them up.
                variants_by_query = {r["query"].strip().lower(): (r.get("variants") or []) for r in rewrites}
                persona_by_query = {r["query"].strip().lower(): (r.get("persona") or "") for r in rewrites}
                region_by_query = {r["query"].strip().lower(): (r.get("region") or "") for r in rewrites}
                coverage_focus = {"anchor": anchor, "rewrites": rewrite_queries, "checklist": checklist,
                                  "variants_by_query": variants_by_query, "persona_by_query": persona_by_query}
                if checklist:
                    checklist_json = json.dumps(checklist)
                if target_rewrites:
                    # Explicit selection: target EXACTLY these rewrites (one post each),
                    # regardless of current coverage (lets you add depth to a region).
                    _sel = {str(t).strip().lower() for t in target_rewrites if str(t).strip()}
                    remaining_gaps = [q for q in rewrite_queries if q.strip().lower() in _sel]
                    if not remaining_gaps:  # selection not in cluster → take it verbatim
                        remaining_gaps = [str(t).strip() for t in target_rewrites if str(t).strip()]
                else:
                    # Order uncovered gaps MANUAL-first (then fixed, then generated) so a
                    # Fill-gaps run spends its post budget on the user's captured queries
                    # before the auto-generated regions.
                    _src = {r["query"].strip().lower(): (r.get("source") or "generated") for r in rewrites}
                    def _prio(q):
                        s = _src.get(q.strip().lower(), "generated")
                        return 0 if s == "manual" else (1 if s == "fixed" else 2)
                    remaining_gaps = sorted(
                        [q for q in rewrite_queries if q.strip().lower() not in covered], key=_prio)
                cluster_size = len(rewrite_queries)
                covered_before = cluster_size - len(remaining_gaps)
                if not remaining_gaps:
                    # Cluster already complete — nothing new to add.
                    self.last_coverage = {
                        "seed": seed, "cluster_size": cluster_size,
                        "covered_before": covered_before, "targeted_this_run": 0,
                        "covered_after": covered_before, "remaining_after": 0,
                        "complete": True}
                    return []
        elif ai_search:
            # No seed → full fan-out, no cluster/gap tracking. Normalize the
            # rewrite objects to query strings + per-region variant/persona maps.
            _fan = self._fanout_rewrites(brands, seed)
            if _fan and _fan.get("rewrites"):
                _rws = _fan["rewrites"]
                coverage_focus = {
                    "anchor": _fan.get("anchor"),
                    "rewrites": [r["query"] for r in _rws],
                    "checklist": _fan.get("checklist") or [],
                    "variants_by_query": {r["query"].strip().lower(): (r.get("variants") or []) for r in _rws},
                    "persona_by_query": {r["query"].strip().lower(): (r.get("persona") or "") for r in _rws},
                }
                region_by_query = {r["query"].strip().lower(): (r.get("region") or "") for r in _rws}
                if coverage_focus["checklist"]:
                    checklist_json = json.dumps(coverage_focus["checklist"])

        # Existing titles for dedup (shared across all intent calls).
        # Scoped to THIS subreddit only — we intentionally allow the
        # same title to be reused in a different subreddit, since
        # cross-sub reposts are a valid strategy. Within the same sub
        # we still block duplicates (Reddit rejects exact-title reposts
        # in many subs).
        existing_titles = set()
        for b in brands:
            existing_titles.update(
                self.db.get_post_titles_for_brand_in_subreddit(
                    b["name"], subreddit["id"]
                )
            )
        existing_titles = list(existing_titles)

        # Existing post count → day offset
        existing_posts = self.db.get_posts(subreddit["id"], primary_brand["id"])
        max_existing_day = max((p["suggested_post_day"] for p in existing_posts), default=-1)
        start_day = max(max_existing_day + 1, FILLER_LEAD_DAYS)

        # Merged storyline distribution (used inside each intent slice for secondary variety)
        merged_dist = {}
        for b in brands:
            dist = self.db.get_storyline_distribution(subreddit["id"], b["id"])
            for k, v in dist.items():
                merged_dist[k] = merged_dist.get(k, 0) + v

        # Generate per-intent per the plan built above.
        # In gap-fill mode, `run_gaps` is the shared, shrinking list of uncovered
        # rewrites; each intent targets it and removes what it covers so two intents
        # in the same run never cover the same gap.
        run_gaps = list(remaining_gaps) if remaining_gaps is not None else None
        # First-time / no-seed standard route: derive a coverage map from the brand's
        # enrichment so the batch deliberately spans distinct offerings (one facet per
        # post) instead of clustering. Off for AI-Search (it has its own cluster), and
        # when a seed or custom_topics already direct the batch. Empty when un-enriched
        # → graceful fallback to today's open-ended generation.
        facets_by_intent = None
        if (not ai_search) and not (seed and str(seed).strip()) and not custom_topics:
            _fm = self._brand_facets(brands)
            if any(_fm.values()):
                facets_by_intent = _fm
        targeted_count = 0
        selected = []
        for intent, n_intent in plan:
            if run_gaps is not None:
                if not run_gaps:
                    break
                n_intent = min(n_intent, len(run_gaps))

            # ---- AI-Search cluster mode (gap-fill / explicit selection): generate ONE
            # post per targeted rewrite, showing the model only that single rewrite, and
            # BIND the post to that exact rewrite (query + region) IN CODE. The post can
            # never drift to a sibling region and never lands nowhere: the binding is a
            # recorded fact from production, not re-derived from the model's wording. ----
            if ai_search and run_gaps is not None and coverage_focus is not None:
                _anchor = coverage_focus.get("anchor") or None
                _brand_kind = (primary_brand.get("category") or "").strip()
                for gap_q in list(run_gaps)[:n_intent]:
                    _region = region_by_query.get(gap_q.strip().lower(), "")
                    single_focus = {**coverage_focus, "rewrites": [gap_q],
                                    "target_region": _region}
                    cands = self._generate_candidates_for_intent(
                        subreddit, brands, intent,
                        self._select_storylines_from_dist(merged_dist, 1),
                        existing_titles, 2, context_only=context_only,
                        seed_focus=None, coverage_focus=single_focus, facet_targets=None,
                        follow_persona=follow_persona, persona_override=None, general=general)
                    if not cands:
                        print(f"[post_gen] AI-Search: no candidate for region «{gap_q[:60]}» — gap left open")
                        continue
                    # Bind every candidate to THIS rewrite by construction (not its
                    # self-report), so scoring/gate/selection all judge it against the
                    # region it was generated for.
                    for c in cands:
                        c["target_query"] = gap_q
                        c["ai_query_score"] = self._score_ai_query_relevance(
                            c["title"], c["body"], anchor=_anchor,
                            target_query=gap_q, brand_kind=_brand_kind, region=_region)
                    cands = self._embedding_gate(cands)
                    best = self._select_cluster_best(cands, 1)
                    if not best:
                        print(f"[post_gen] AI-Search: no candidate cleared the bar for region «{gap_q[:60]}» — gap left open")
                        continue
                    post = best[0]
                    post["intent"] = intent
                    post["target_query"] = gap_q                       # exact canonical
                    post["region"] = region_by_query.get(gap_q.strip().lower(), "")
                    existing_titles.append(post["title"])
                    run_gaps = [g for g in run_gaps if g.strip().lower() != gap_q.strip().lower()]
                    targeted_count += 1
                    selected.append(post)
                continue  # per-rewrite path done for this intent

            storylines_for_intent = self._select_storylines_from_dist(merged_dist, n_intent)
            # Gap-fill: steer this intent to the REMAINING gaps only.
            intent_focus = coverage_focus
            if run_gaps is not None and coverage_focus is not None:
                intent_focus = {**coverage_focus, "rewrites": run_gaps}
            # First-time coverage: hand this intent up to n_intent distinct facets
            # (consumed so no facet repeats across the batch). No oversampling in
            # facet mode — oversample + score-select could drop a facet to double
            # up another, defeating the spread.
            facet_targets = None
            req_count = n_intent * 2
            if facets_by_intent is not None:
                _avail = facets_by_intent.get(intent) or []
                if _avail:
                    facet_targets = _avail[:n_intent]
                    facets_by_intent[intent] = _avail[len(facet_targets):]
                    req_count = n_intent
            candidates = self._generate_candidates_for_intent(
                subreddit, brands, intent, storylines_for_intent,
                existing_titles, req_count, context_only=context_only,
                # In AI-Search mode the fan-out already consumed the seed, so
                # we steer via coverage_focus instead of the seed block.
                seed_focus=(None if ai_search else seed),
                coverage_focus=intent_focus,
                facet_targets=facet_targets,
                follow_persona=follow_persona, persona_override=persona_override,
                general=general,
            )
            if not candidates:
                print(f"[post_gen] WARNING: no candidates returned for intent={intent}")
                continue

            # Snap each candidate's (LLM-reported, often paraphrased) target_query to the
            # canonical rewrite it best matches — but ONLY among the rewrites THIS batch
            # was told to cover (`intent_focus["rewrites"]` = the current gaps), NOT the
            # whole cluster. Matching against every rewrite let a post produced for gap A
            # drift onto a textually-similar sibling region B. Scoping to the targeted
            # gaps keeps each post on the region it was actually generated from, while
            # still fixing the paraphrase→exact binding that coverage keys off.
            if ai_search and intent_focus and intent_focus.get("rewrites"):
                _canon = intent_focus["rewrites"]
                for c in candidates:
                    _m = self.db.match_query_to_rewrites(c.get("target_query"), _canon)
                    if _m:
                        c["target_query"] = _m

            # Score each for AI-query relevance. In AI-Search mode the scorer also
            # enforces anchor-retention + question-form (off-anchor/vent → low).
            # `brand_kind` (the brand's category) lets the scorer enforce ENTITY-TYPE
            # match (the answer must name a brand of this kind) — relative + gated, so
            # it only fires for an enriched brand and only on a clear mismatch.
            _anchor = (coverage_focus.get("anchor") if coverage_focus else None) or None
            _brand_kind = (primary_brand.get("category") or "").strip()
            for c in candidates:
                # target_query is now snapped to its canonical rewrite, so we can recover
                # the region it belongs to and enforce region fidelity in the scorer too.
                _c_region = region_by_query.get((c.get("target_query") or "").strip().lower(), "")
                c["ai_query_score"] = self._score_ai_query_relevance(
                    c["title"], c["body"],
                    anchor=_anchor, target_query=c.get("target_query"),
                    brand_kind=_brand_kind, region=_c_region)

            # AI-Search mode: deterministic embedding relevance gate (drops posts that
            # drifted off their target_query; no-op without an embeddings key), then
            # coverage-gated selection (one strong post per distinct rewrite).
            if ai_search:
                candidates = self._embedding_gate(candidates)
                picked = self._select_cluster_best(candidates, n_intent)
            else:
                picked = self._select_best(candidates, storylines_for_intent, n_intent)
            for c in picked:
                c["intent"] = intent
                # Dedup across intent calls — add picked titles to the seen set
                existing_titles.append(c["title"])
                # Remove the gap this post just covered so later intents skip it.
                if run_gaps is not None:
                    tq = (c.get("target_query") or "").strip().lower()
                    run_gaps = [g for g in run_gaps if str(g).strip().lower() != tq]
            targeted_count += len(picked)
            selected.extend(picked)

        # Coverage summary for gap-fill runs (exact X/N).
        if remaining_gaps is not None:
            covered_after = cluster_size - len(run_gaps)
            self.last_coverage = {
                "seed": seed, "cluster_size": cluster_size,
                "covered_before": covered_before,
                "targeted_this_run": targeted_count,
                "covered_after": covered_after,
                "remaining_after": len(run_gaps),
                "complete": len(run_gaps) == 0,
            }

        if not selected:
            return []

        # Append custom topics (intent-free, user-provided)
        if custom_topics:
            for topic in custom_topics:
                selected.append({
                    "title": topic,
                    "body": "",
                    "storyline": "question",
                    "intent": None,
                    "ai_query_score": 0,
                    "is_custom": 1,
                    "image_prompt": None,
                })

        # Scheduling
        total = len(selected)
        spread = max(total, int(total * POST_SPREAD_FACTOR))
        for i, post in enumerate(selected):
            post["suggested_post_day"] = start_day + int(i * spread / total)

        # Image prompts
        for post in selected:
            if post.get("is_custom"):
                continue
            post["image_prompt"] = self._generate_image_prompt(
                post["title"], post["body"], post["storyline"]
            )

        # Save to DB — link to all brands via junction table
        _anchor = (coverage_focus.get("anchor") if coverage_focus else None) or None
        saved = []
        for post in selected:
            # AI-Search posts persist their root reference (the seed/anchor the
            # cluster came from) + the specific rewrite this post targets, so the
            # post-detail view can show what prompt it was generated for.
            ai_search_meta = None
            if (not ai_search) and persona_override and persona_override.get("label"):
                # Direct (non-cluster) post written as a picked persona — stamp it so the
                # Live Subs persona tag shows and regenerate can reuse the same voice.
                ai_search_meta = json.dumps({"persona": persona_override["label"]})
            if ai_search:
                _pbq = (coverage_focus or {}).get("persona_by_query", {})
                _tq = (post.get("target_query") or "").strip().lower()
                # `region` is the STABLE identity coverage joins on. Per-rewrite mode
                # stamps post["region"] by construction; otherwise fall back to the
                # query→region map. So the post is credited to the exact region it was
                # generated for — never re-matched from text.
                ai_search_meta = json.dumps({
                    "seed": seed,
                    "anchor": _anchor,
                    "target_query": post.get("target_query"),
                    "persona": _pbq.get(_tq, ""),
                    "region": post.get("region") or region_by_query.get(_tq, ""),
                })
            post_id = self.db.save_post(
                subreddit_id=subreddit["id"],
                brand_id=primary_brand["id"],
                title=post["title"],
                body=post.get("body", ""),
                storyline=post.get("storyline", "question"),
                image_prompt=post.get("image_prompt"),
                image_url=post.get("image_url"),
                ai_query_score=post.get("ai_query_score", 0),
                is_custom=post.get("is_custom", 0),
                is_filler=0,
                status="complete",
                suggested_post_day=post.get("suggested_post_day", 0),
                prompt_version=(PROMPT_VERSION + "-ai-search") if ai_search else PROMPT_VERSION,
                brand_ids=brand_ids,
                intent=post.get("intent"),
                concept_checklist=checklist_json,
                ai_search_meta=ai_search_meta,
            )
            post["id"] = post_id
            saved.append(post)

        return saved

    def generate_post_from_topic(self, subreddit, brand, topic, existing_titles=None,
                                 general=False):
        """Live Subreddits — flesh out one full post from a user-supplied title.

        The user-supplied `topic` is the FINAL post title, used verbatim — UNLESS
        `general=True`, in which case the LLM REWRITES it into the natural-human
        "general mode" style (keeping its EXACT target + recommendation ask, just
        stripping markety/headline phrasing). The LLM writes the body, storyline,
        and intent (and, in general mode, the rewritten title too).

        Reuses the same brand-context scaffolding as `generate_posts` but:
        - num_posts = 1
        - title is the user's input, unchanged
        - returns one dict {title, body, storyline, intent, ai_query_score}
          (NOT saved — caller persists)
        """
        if isinstance(brand, dict):
            brands = [brand]
        else:
            brands = brand if isinstance(brand, list) else [brand]

        # The user's input IS the final title. Don't let the model touch it.
        final_title = (topic or "").strip()
        if not final_title:
            return None

        # Classify the user-supplied title so the BODY can adapt:
        # short answer-asking titles get a short focused body that gives
        # just-enough context for someone to recommend something; longer
        # experience-style titles get a richer narrative body. Without
        # this, every custom post got the same "2-4 paragraphs" body
        # regardless of whether the title was a 6-word question or a
        # 30-word personal-story setup.
        intent_info = classify_post_intent(final_title, post_body=None, stored_intent=None)
        intent_label = intent_info["intent_label"]
        if intent_info["target_length_band"] == "crisp":
            body_target = "1-2 short paragraphs (max ~100 words). Just enough context for someone to give a useful recommendation — your situation, what you've considered, what's pushing you to ask."
        elif intent_info["target_length_band"] == "long":
            body_target = "3-5 paragraphs (~150-280 words). Richer first-person narrative — context, what you've tried, where you are now, what's still bugging you."
        else:
            body_target = "2-3 short paragraphs (~100-180 words). First-person context with some detail — situation, options you've weighed, what you're stuck on."

        brand_block, target_names, competitors = self._build_enriched_brand_block(brands)
        target_names_str = ", ".join(target_names) if target_names else "(none)"
        competitors_str = ", ".join(competitors) if competitors else "(none known)"

        existing_text = ""
        if existing_titles:
            sample = list(existing_titles)[:30]
            existing_lines = "\n".join(f'  - "{t}"' for t in sample)
            existing_text = f"\nEXISTING POST TITLES on this brand (avoid drifting into duplicate-feeling territory):\n{existing_lines}\n"

        banned_sample = ", ".join(random.sample(BANNED_PHRASES, min(8, len(BANNED_PHRASES))))
        storylines_list = ", ".join(STORYLINE_TYPES.keys())

        if general:
            title_directive = f'''The USER'S SUPPLIED TITLE below is the TARGET the post must answer — REWRITE it
into a natural, human recommendation question per GENERAL MODE (below). Keep its
EXACT target/ask (the same product / need / recommendation it is about) and keep it
a recommendation question a helpful AI would answer by NAMING a specific product /
service — only strip marketer / headline phrasing and make it read like a real
person typed it. Do NOT drift to a different topic.

USER'S SUPPLIED TITLE (rewrite into the general style; keep its target):
\"\"\"{final_title}\"\"\"
{_GENERAL_STYLE_BLOCK}'''
            title_json_field = '    "title": "the rewritten natural-human title — still a recommendation question targeting the SAME thing",\n'
            json_note = ""
        else:
            title_directive = f'''The TITLE is FIXED — it is the user's exact text. Do NOT rewrite,
shorten, expand, paraphrase, or improve the title in any way. Your job
is ONLY to write a body, storyline, and intent that fit this title.

POST TITLE (FIXED — use exactly as given, do not modify):
\"\"\"{final_title}\"\"\"'''
            title_json_field = ""
            json_note = " (note: NO \"title\" field — the title is fixed and we will use the user's input verbatim)"

        prompt = f"""Write {'a Reddit post (title + body)' if general else 'the BODY of a Reddit post'} for r/{subreddit['name']}.

{title_directive}

SUBREDDIT DOMAIN: {subreddit['domain']}

BRAND CONTEXT (for grounding the body — NEVER mention the target brand names):
{brand_block}
{existing_text}
GOAL: write a body that fits the title above and is also retrievable by
AI search engines (GEO) for queries about this brand's domain. The LLM
ranks the BODY, so pack the brand's category / audience / pain-point /
use-case keywords naturally as the user explains their situation.

STRICT RULES:
  1. NEVER mention any TARGET brand name: {target_names_str}
  2. Pick the best-fitting INTENT for this title from:
     commercial / comparison / informational.
     - commercial: ready to pick a tool/product/service.
     - comparison: weighing 2+ options (competitor names allowed: {competitors_str}).
     - informational: wants to understand, not buy.
  3. Pick a STORYLINE from: {storylines_list} that fits the title.
  4. BODY: {body_target}
     Conversational first-person tone with minor imperfections
     (occasional typos, incomplete thoughts, run-on sentences). Pack the
     brand's category / pain-point / audience keywords naturally as the
     user explains their situation — this is what powers GEO ranking.
     The detected post intent for this title is: {intent_label}.
     The body MUST include a natural-language phrasing of the underlying
     long-tail recommendation query somewhere inside it — woven into a
     sentence the user might naturally write (describe their situation and
     what they're trying to pick or buy), NOT as a header and NOT a copied template.
  5. The body must be COMPATIBLE with the title — answer / expand / give
     context for whatever question or situation the title raises. Do NOT
     drift to a different topic.
  6. Do NOT look AI-generated. No marketing language. No excessive
     formatting. No emoji. No dashes (-, —, --).
  7. Do NOT include the title text inside the body verbatim.

NEVER USE THESE PHRASES: {banned_sample}

Return JSON only{json_note}:
{{
{title_json_field}    "body": "2-4 paragraph first-person body with context, packed with the brand's domain keywords",
    "storyline": "one of: {storylines_list}",
    "intent": "commercial | comparison | informational"
}}"""

        # Capture the LLM result + log diagnostics on every failure
        # mode so 'Topic generation failed' on the UI side becomes
        # debuggable from stdout instead of an opaque None.
        try:
            result = self.claude.call(prompt, max_tokens=2500, temperature=0.85)
        except Exception as e:
            print(f"[generate_post_from_topic] LLM exception: {type(e).__name__}: {e}", flush=True)
            return None
        if result is None:
            print(f"[generate_post_from_topic] LLM returned None — see ClaudeClient.call retry logs above. title={final_title!r}", flush=True)
            return None
        if "body" not in result:
            print(f"[generate_post_from_topic] LLM response missing 'body' key. Keys: {list(result.keys())}. title={final_title!r}", flush=True)
            return None
        body = (result.get("body") or "").strip()
        if not body:
            print(f"[generate_post_from_topic] LLM returned empty body. title={final_title!r}", flush=True)
            return None

        storyline = result.get("storyline") or "question"
        if storyline not in STORYLINE_TYPES:
            storyline = "question"
        intent = result.get("intent")
        if intent not in INTENT_TYPES:
            intent = "informational"

        # In general mode the model returns a rewritten (natural-human) title; fall
        # back to the user's input if it didn't. Non-general = verbatim, as before.
        out_title = final_title
        if general:
            _rw = (result.get("title") or "").strip()
            if _rw:
                out_title = _rw

        ai_score = self._score_ai_query_relevance(out_title, body)

        return {
            "title": out_title,  # verbatim (non-general) or the general-style rewrite
            "body": body,
            "storyline": storyline,
            "intent": intent,
            "ai_query_score": ai_score,
            "is_custom": 1,
        }

    def generate_welcome_post(self, subreddit):
        """Generate a single welcome/intro post for a new subreddit.

        Creates a pinned-style community introduction post. Saved as filler on day 0.
        """
        rules = []
        try:
            rules = json.loads(subreddit.get("rules", "[]"))
        except (json.JSONDecodeError, TypeError):
            pass

        rules_text = ""
        if rules:
            rules_text = "\n".join(f"  - {r.get('title', r) if isinstance(r, dict) else r}" for r in rules)
            rules_text = f"\nSUBREDDIT RULES:\n{rules_text}\n"

        prompt = f"""Write a warm, authentic welcome post for a brand-new subreddit called r/{subreddit['name']}.

SUBREDDIT DOMAIN: {subreddit['domain']}
SUBREDDIT DESCRIPTION: {subreddit.get('description', '')}
{rules_text}
This is the very first post in the community. It should:
- Welcome people to the community and explain what it's about
- Set the tone (friendly, helpful, open to questions)
- Briefly mention what kind of posts are encouraged
- Feel like a real community founder wrote it (not corporate)
- Be 2-3 paragraphs, conversational tone
- NOT mention any specific brands or products

Return JSON only:
{{
    "title": "Welcome post title (e.g. 'Welcome to r/{subreddit['name']}! Intro + what this community is about')",
    "body": "Welcome post body text"
}}"""

        result = self.claude.call(prompt, max_tokens=1500, temperature=0.8)
        if not result or "title" not in result:
            return None

        post_id = self.db.save_post(
            subreddit_id=subreddit["id"],
            brand_id=None,
            title=result["title"],
            body=result.get("body", ""),
            storyline="psa",
            is_filler=1,
            status="complete",
            suggested_post_day=0,
            prompt_version=PROMPT_VERSION,
        )
        result["id"] = post_id
        return result

    def generate_filler_posts(self, subreddit, count):
        """Generate organic filler posts with NO brand angle.

        These seed the subreddit with genuine community content before brand posts appear.
        """
        prompt = f"""Generate {count} organic Reddit posts for r/{subreddit['name']}.

SUBREDDIT DOMAIN: {subreddit['domain']}
SUBREDDIT DESCRIPTION: {subreddit.get('description', '')}

These are GENUINE community posts. They should:
- Be the kind of posts a real community member would make
- Cover diverse topics within the domain
- NOT mention any brand or product by name
- NOT be asking for recommendations (that's for brand posts)
- Include personal experiences, PSAs, general questions, tips, discussions
- Feel like a new but active subreddit with real members

Mix of post types:
- Personal experience sharing
- General discussion questions
- Tips/advice for newcomers
- Interesting observations
- Community polls/debates

Return JSON only:
{{
    "posts": [
        {{
            "title": "Post title (natural Reddit style, not clickbait)",
            "body": "Post body text (2-4 paragraphs, conversational)",
            "storyline": "experience|question|psa|discovery"
        }}
    ]
}}"""

        result = self.claude.call(prompt, max_tokens=3000, temperature=0.9)
        if not result or "posts" not in result:
            return []

        saved = []
        for i, post in enumerate(result["posts"][:count]):
            post_id = self.db.save_post(
                subreddit_id=subreddit["id"],
                brand_id=None,
                title=post["title"],
                body=post.get("body", ""),
                storyline=post.get("storyline", "experience"),
                is_filler=1,
                status="complete",
                suggested_post_day=i,  # filler posts go on early days
                prompt_version=PROMPT_VERSION,
            )
            post["id"] = post_id
            saved.append(post)

        return saved

    def _select_storylines(self, subreddit_id, brand_id, count):
        """Balance storyline distribution. Pick underrepresented types."""
        distribution = self.db.get_storyline_distribution(subreddit_id, brand_id)
        return self._select_storylines_from_dist(distribution, count)

    def _select_storylines_from_dist(self, distribution, count):
        """Balance storyline distribution from a pre-computed dict."""
        all_types = list(STORYLINE_TYPES.keys())
        selected = []
        for _ in range(count):
            min_count = float("inf")
            min_type = all_types[0]
            for st in all_types:
                current = distribution.get(st, 0) + selected.count(st)
                if current < min_count:
                    min_count = current
                    min_type = st
            selected.append(min_type)
        random.shuffle(selected)
        return selected

    def _build_enriched_brand_block(self, brands):
        """Format the GEO enrichment fields from a list of brand dicts into a prompt section.

        Also returns two flat lists: (target_brand_names, competitor_names) used by the
        caller to populate the strict rules block. Falls back gracefully if a brand has
        not been enriched yet — only the base name/context/keywords are included, and
        a warning is logged so the user knows quality is degraded.
        """
        lines = []
        target_names = []
        all_competitors = []
        any_enriched = False

        for b in brands:
            target_names.append(b["name"])
            lines.append(f"\n--- Brand: {b['name']} ---")

            def _parse_list(val):
                if not val:
                    return []
                if isinstance(val, list):
                    return val
                try:
                    parsed = json.loads(val)
                    return parsed if isinstance(parsed, list) else []
                except (json.JSONDecodeError, TypeError):
                    # Also accept newline/comma-separated fallback
                    return [s.strip() for s in str(val).replace("\n", ",").split(",") if s.strip()]

            category = (b.get("category") or "").strip()
            audience = (b.get("audience") or "").strip()
            use_cases = _parse_list(b.get("use_cases"))
            pain_points = _parse_list(b.get("pain_points"))
            features = _parse_list(b.get("features"))
            competitors = _parse_list(b.get("competitors"))

            if category or audience or use_cases or pain_points or features or competitors:
                any_enriched = True

            if category:
                lines.append(f"  Category: {category}")
            if audience:
                lines.append(f"  Target audience: {audience}")
            if use_cases:
                lines.append(f"  Typical use cases: {'; '.join(use_cases)}")
            if pain_points:
                lines.append(f"  Pain points solved: {'; '.join(pain_points)}")
            if features:
                lines.append(f"  Key features: {'; '.join(features)}")
            if competitors:
                lines.append(f"  Competitors in the space: {', '.join(competitors)}")
                all_competitors.extend(competitors)

            if b.get("context"):
                lines.append(f"  Brand narrative: {b['context']}")

            kw = _parse_list(b.get("keywords"))
            if kw:
                lines.append(f"  Keywords: {', '.join(kw)}")

            # Anchor-scoped knowledge learned on past cluster creations: what this
            # brand actually offers for specific topics. Grounds generation so posts
            # stay truthful + on-target across the brand's covered topics.
            learned = b.get("learned_context")
            if learned:
                try:
                    learned = json.loads(learned) if isinstance(learned, str) else learned
                except (json.JSONDecodeError, TypeError):
                    learned = None
                topic_lines = []
                for entry in (learned.values() if isinstance(learned, dict) else []):
                    if not isinstance(entry, dict):
                        continue
                    summ = (entry.get("summary") or "").strip()
                    if summ and entry.get("covers"):
                        topic_lines.append(f"    - {(entry.get('anchor') or '').strip()}: {summ}")
                if topic_lines:
                    lines.append("  What the brand offers for specific topics (learned):")
                    lines.extend(topic_lines[:12])

        if not any_enriched:
            print(
                f"[post_gen] WARNING: none of the selected brands "
                f"({', '.join(target_names)}) are enriched. "
                "Post quality will be degraded — click 'Enrich from website' on the brand "
                "to get category/audience/use-cases/competitors for better GEO queries."
            )

        return "\n".join(lines), target_names, all_competitors

    def _ground_brand_for_anchor(self, brands, seed, seed_norm):
        """On cluster creation, learn what the PRIMARY brand actually offers for this
        seed's topic (from the brand's own site), persist it into the brand's
        accumulating `learned_context` (keyed by seed_norm), and mutate the in-memory
        brand so the brand block built just after reflects it. Cached: if this anchor
        was already grounded, returns the stored summary without re-fetching.

        Returns the anchor summary string (may be "")."""
        if not (brands and seed and str(seed).strip()):
            return ""
        b = brands[0]
        # Parse existing learned_context.
        raw = b.get("learned_context")
        try:
            learned = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except (json.JSONDecodeError, TypeError):
            learned = {}
        if not isinstance(learned, dict):
            learned = {}
        # Cached → reuse (no refetch).
        if seed_norm in learned and isinstance(learned[seed_norm], dict):
            return (learned[seed_norm].get("summary") or "").strip()
        # Fetch + distill from the brand's own site.
        try:
            from generators.brand_enrichment import enrich_brand_for_anchor
            import datetime as _dt
            g = enrich_brand_for_anchor(self.claude, b.get("name") or "",
                                        b.get("domain_url") or "", str(seed).strip())
        except Exception as e:
            print(f"[post_gen] anchor grounding skipped for seed '{seed}': {e}")
            return ""
        if not g or not g.get("summary"):
            return ""
        learned[seed_norm] = {
            "anchor": str(seed).strip(),
            "summary": g.get("summary", ""),
            "covers": bool(g.get("covers")),
            "key_points": g.get("key_points") or [],
            "added_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        if not g.get("covers"):
            print(f"[post_gen] weak-fit anchor: '{b.get('name')}' has no clear offering for "
                  f"'{seed}' — generating anyway, but consider a closer anchor.")
        # Persist (manual `context` untouched) + mutate in-memory so the block sees it.
        try:
            self.db.update_brand(b["id"], learned_context=json.dumps(learned))
        except Exception as e:
            print(f"[post_gen] could not persist learned_context: {e}")
        b["learned_context"] = json.dumps(learned)
        return (g.get("summary") or "").strip()

    @staticmethod
    def _parse_personas(brands):
        """Parse the primary brand's stored personas JSON → list of dicts (or [])."""
        b = brands[0] if isinstance(brands, list) else brands
        raw = (b or {}).get("personas")
        try:
            p = json.loads(raw) if isinstance(raw, str) else (raw or [])
        except (json.JSONDecodeError, TypeError):
            p = []
        return p if isinstance(p, list) else []

    def _fit_personas(self, brands):
        """Personas the brand can credibly be the answer for (fit yes/maybe) — the ones
        that drive the fan-out lens. Excludes fit==no (the winnability filter)."""
        return [p for p in self._parse_personas(brands)
                if isinstance(p, dict) and p.get("label")
                and str(p.get("fit", "")).strip().lower() in ("yes", "maybe")]

    def _persona_detail_map(self, brands):
        """{label.lower(): persona_dict} for the brand's personas — lets the writer
        expand a region's assigned persona LABEL into its full detail."""
        return {str(p.get("label", "")).strip().lower(): p
                for p in self._parse_personas(brands)
                if isinstance(p, dict) and p.get("label")}

    @staticmethod
    def _render_persona_voice(pd):
        """Render a persona dict as a compact WRITE-AS instruction for the post prompt
        (profile + pains + goal + vocab + trigger + constraints). '' when empty."""
        if not isinstance(pd, dict) or not pd.get("label"):
            return ""
        parts = [f"{pd['label']}"]
        if pd.get("profile"):     parts.append(pd["profile"])
        pp = pd.get("pain_points") or []
        if pp:                    parts.append("pains: " + "; ".join(pp[:4]))
        uc = pd.get("use_cases") or []
        if uc:                    parts.append("trying to: " + "; ".join(uc[:4]))
        if pd.get("goal"):        parts.append("wants: " + pd["goal"])
        if pd.get("trigger"):     parts.append("triggered by: " + pd["trigger"])
        if pd.get("constraints"): parts.append("constraints: " + pd["constraints"])
        if pd.get("vocab"):       parts.append("says it like: " + pd["vocab"])
        return " — ".join(parts)

    def _assign_personas_to_regions(self, rewrites, brands):
        """FIT-RESPECTING persona→region assignment — a region only ever gets a persona that
        GENUINELY fits its price tier / intent / use-case. Never forces a wrong match.

        Flow:
          1. Ask the LLM, per region, for ONLY the personas that genuinely fit (ranked
             best→worst, empty list when none fit). The persona lines include `constraints`
             (budget/premium etc.) so the ranker can tell a value persona from a luxury one.
          2. Assign each region the LEAST-USED among its genuine fits (spread/diversity),
             tie-broken by the LLM's rank order.
          3. Any region with NO fit → grow the brand's roster: generate a new persona for it,
             persist it to the brand, and assign it (db.update_brand). Fallback = blank, never
             a wrong persona.

        Mutates rewrite["persona"]. `brands` (not a persona list) is passed so we can read AND
        persist the brand's personas. No-op (blank) when there are no fit personas AND growth
        can't run."""
        personas = self._parse_personas(brands)
        b = brands[0] if isinstance(brands, list) else brands
        rlist = [r for r in (rewrites or []) if (r.get("query") or "").strip()]
        if not rlist:
            for r in (rewrites or []):
                r["persona"] = r.get("persona") or ""
            return
        fitp = [p for p in (personas or []) if isinstance(p, dict) and p.get("label")
                and str(p.get("fit", "")).strip().lower() in ("yes", "maybe")]
        # genuine-fit list per region index (best→worst); empty = no roster persona fits.
        per_region_fits = [[] for _ in rlist]
        if fitp:
            labels = [p["label"] for p in fitp]
            label_by_lower = {l.lower(): l for l in labels}
            plines = "\n".join(
                f"  - {p['label']}: {p.get('profile','')}"
                + (f" | wants: {p.get('goal','')}" if p.get('goal') else "")
                + (f" | budget/constraints: {p.get('constraints','')}" if p.get('constraints') else "")
                + (f" | triggered by: {p.get('trigger','')}" if p.get('trigger') else "")
                + (f" | says it like: {p.get('vocab','')}" if p.get('vocab') else "")
                for p in fitp)
            qlist = "\n".join(f"  {i+1}. {r['query']}" for i, r in enumerate(rlist))
            prompt = f"""For each REGION (a search question), RANK the personas that could plausibly ask it,
best fit → weakest. Match the region's concrete signals against each persona's goal /
budget-constraints / vocab:
  - PRICE TIER is decisive: a "budget / affordable / cheap / under $X" region must NEVER list a
    premium / luxury / high-end persona, and a "luxury / premium / high-end / splurge" region must
    NEVER list a budget / value persona. EXCLUDE those clear mismatches entirely.
  - Also weigh use-case, sleep position, firmness, body-type, and urgency cues.
Include each region's genuine fit FIRST, then its secondary/adjacent fits (usually 2-4 total) — NOT
just the single best. Only EXCLUDE personas that are a clear mismatch; only return an EMPTY list when
truly NO persona fits.

GOAL: this drives a campaign that wants DIFFERENT regions covered by DIFFERENT personas. Give each
region a few ranked candidates so they can be distributed — do NOT return the same single persona for
every region unless the brand genuinely serves only one.

PERSONAS:
{plines}

REGIONS:
{qlist}

Use the persona labels EXACTLY as written above. Return JSON only:
{{"assignments": [["best-fit label","secondary label","..."], ...]}} — one ranked list per region, in
the SAME order as the regions above (use [] only for a region no persona fits)."""
            result = self.claude.call(prompt, max_tokens=1200, temperature=0.2)
            per_region_fits = self._parse_region_persona_ranking(
                result, rlist, label_by_lower)
        # FIT-SCOPED per-persona cap: no single persona may take more than
        # ceil(regions/personas) regions, so it spreads — but a capped region only ever moves to
        # ANOTHER of its OWN genuine fits (ranked), never a wrong/excluded persona (that's what the
        # old all-persona-backfill cap got wrong → affordable→luxury).
        n_fit = len(fitp) if fitp else 0
        cap = max(1, math.ceil(len(rlist) / n_fit)) if n_fit else 1
        counts = {}
        unmatched = []  # indices with no genuine fit
        for i, r in enumerate(rlist):
            fits = per_region_fits[i]
            if not fits:
                r["persona"] = ""
                unmatched.append(i)
                continue
            # First candidate (best→worst rank) still under the cap; else the region's own
            # least-used fit (exceed cap rather than assign outside its fit list).
            pick = next((l for l in fits if counts.get(l, 0) < cap), None)
            if pick is None:
                pick = min(fits, key=lambda l: (counts.get(l, 0), fits.index(l)))
            counts[pick] = counts.get(pick, 0) + 1
            r["persona"] = pick
        # Grow the roster for regions nothing fit: new personas, persisted to the brand.
        grew = []
        if unmatched and b:
            try:
                grew = self._grow_personas_for_regions(b, [rlist[i] for i in unmatched])
            except Exception as e:
                print(f"[persona-assign] persona growth skipped: {e}")
                grew = []
        try:
            print("[persona-assign] " + ", ".join(
                f"{(r.get('region') or '?')}→{r['persona'] or '(none)'}" for r in rlist))
            if grew:
                print(f"[persona-assign] grew {len(grew)} new persona(s): {', '.join(grew)}")
        except Exception:
            pass

    @staticmethod
    def _parse_region_persona_ranking(result, rlist, label_by_lower):
        """Normalize the ranker's reply into a per-region list of canonical persona labels
        (best→worst). Tolerant of several JSON shapes so a model phrasing/shape change doesn't
        silently empty every region (→ collapse / mass-growth):
          - list-of-lists:   [["A","B"], [], ...]            (positional, current shape)
          - list-of-objects: [{"region": "...", "personas"/"fits"/"ranked": ["A",...]}, ...]
                             (matched by region text when present, else positional)
          - dict keyed by region text: {"<region>": ["A", ...], ...}
        Returns a list aligned to rlist; unknown/garbage → [] for that region."""
        n = len(rlist)
        out = [[] for _ in range(n)]
        if not isinstance(result, dict):
            return out
        assignments = result.get("assignments")
        # Some models drop the wrapper and return the bare list/dict.
        if assignments is None and isinstance(result.get("rankings"), (list, dict)):
            assignments = result.get("rankings")
        if assignments is None and isinstance(result, dict) and not result.get("assignments"):
            # A dict keyed directly by region text?
            if all(isinstance(v, list) for v in result.values()) and result:
                assignments = result

        def _labels_from(seq):
            cands = []
            for c in (seq or []):
                cl = str(c).strip().lower()
                lab = label_by_lower.get(cl)
                if lab and lab not in cands:
                    cands.append(lab)
            return cands

        region_lower = {(r.get("query") or "").strip().lower(): i for i, r in enumerate(rlist)}

        if isinstance(assignments, dict):
            for k, v in assignments.items():
                idx = region_lower.get(str(k).strip().lower())
                if idx is not None and isinstance(v, list):
                    out[idx] = _labels_from(v)
            return out

        if isinstance(assignments, list):
            for i, entry in enumerate(assignments):
                if isinstance(entry, list):
                    if i < n:
                        out[i] = _labels_from(entry)
                elif isinstance(entry, dict):
                    seq = (entry.get("personas") or entry.get("fits")
                           or entry.get("ranked") or entry.get("labels") or [])
                    rq = str(entry.get("region") or entry.get("query") or "").strip().lower()
                    idx = region_lower.get(rq, i if i < n else None)
                    if idx is not None:
                        out[idx] = _labels_from(seq)
        return out

    def _grow_personas_for_regions(self, b, region_rewrites):
        """For regions no existing persona fits, generate NEW personas, persist them onto the
        brand, and assign each region its new persona. Returns the list of new persona labels
        added (empty when nothing could be grown — those regions keep persona ''). Mutates
        each rewrite['persona']."""
        queries = [r["query"] for r in region_rewrites if (r.get("query") or "").strip()]
        if not b or not queries:
            return []
        existing = self._parse_personas([b])
        existing_labels = [p.get("label") for p in existing
                           if isinstance(p, dict) and p.get("label")]
        from generators.brand_enrichment import generate_personas_for_regions
        res = generate_personas_for_regions(
            self.claude, b.get("name") or "", b.get("domain_url") or "",
            category=b.get("category") or "", audience=b.get("audience") or "",
            use_cases=b.get("use_cases"), pain_points=b.get("pain_points"),
            region_queries=queries, existing_labels=existing_labels)
        new_personas = (res or {}).get("new_personas") or []
        assignments = (res or {}).get("assignments") or []
        if not new_personas:
            return []
        # Persist merged roster onto the brand (in-memory + DB), like _ensure_personas.
        merged = list(existing) + list(new_personas)
        try:
            self.db.update_brand(b["id"], personas=json.dumps(merged))
        except Exception as e:
            print(f"[persona-assign] could not persist grown personas: {e}")
        b["personas"] = json.dumps(merged)
        # Apply the region→new-persona assignments.
        assign_by_query = {}
        for a in assignments:
            if isinstance(a, (list, tuple)) and len(a) >= 2:
                assign_by_query[str(a[0]).strip().lower()] = str(a[1]).strip()
        for r in region_rewrites:
            lbl = assign_by_query.get((r.get("query") or "").strip().lower())
            if lbl:
                r["persona"] = lbl  # else leaves the prior '' (broad) — never a wrong match
        return [p["label"] for p in new_personas if p.get("label")]

    def _ensure_personas(self, brands):
        """Auto-generate (and auto-UPGRADE) the brand's personas so the fan-out lens +
        write-time voice have them. Generates when there are none; also REGENERATES when
        the stored personas predate the pain_points/use_cases enrichment (none carry
        either field) — so old brands self-heal the next time the workflow needs personas
        (cluster build / AI-Search generate), with no manual re-enrich. No-op once they
        carry the new fields, and on any failure (never blocks generation)."""
        b = brands[0] if isinstance(brands, list) else brands
        if not b:
            return
        _existing = self._parse_personas(brands)
        # Up-to-date already → nothing to do. "Up-to-date" = at least one persona carries
        # the enriched fields. Empty OR all-old-shape → (re)generate.
        if _existing and any(
            (isinstance(p, dict) and (p.get("pain_points") or p.get("use_cases")))
            for p in _existing
        ):
            return
        if _existing:
            print(f"[post_gen] upgrading {len(_existing)} legacy persona(s) for brand "
                  f"{b.get('id')} → adding pain_points/use_cases")
        try:
            from generators.brand_enrichment import generate_brand_personas
            personas = generate_brand_personas(
                self.claude, b.get("name") or "", b.get("domain_url") or "",
                category=b.get("category") or "", audience=b.get("audience") or "",
                use_cases=b.get("use_cases"), pain_points=b.get("pain_points"))
        except Exception as e:
            print(f"[post_gen] persona generation skipped for brand {b.get('id')}: {e}")
            return
        if not personas:
            return
        try:
            self.db.update_brand(b["id"], personas=json.dumps(personas))
        except Exception as e:
            print(f"[post_gen] could not persist personas: {e}")
        b["personas"] = json.dumps(personas)
        n_fit = sum(1 for p in personas if str(p.get("fit", "")).lower() in ("yes", "maybe"))
        print(f"[post_gen] generated {len(personas)} personas for brand {b.get('id')} "
              f"({n_fit} fit yes/maybe)")

    def create_cluster(self, brands, seed, observed_queries=None):
        """Build + persist an AI-Search cluster for a (brand, seed) WITHOUT generating
        any posts — the fan-out only. Reuse-only: if a cluster already exists for the
        seed, fold in any observed_queries and return it unchanged (never clobber).
        Returns a summary dict (or {error} when the fan-out yields nothing)."""
        if isinstance(brands, dict):
            brands = [brands]
        if not brands or not (seed and str(seed).strip()):
            return {"error": "brand and seed required"}
        bid = brands[0]["id"]
        seed_norm = self.db.normalize_seed(seed)
        observed = [str(q).strip() for q in (observed_queries or []) if str(q).strip()]

        existing = self.db.get_ai_search_cluster(bid, seed_norm)
        if existing and not existing.get("backfilled"):
            rewrites = self.db.normalize_rewrites(existing.get("rewrites_json"))
            checklist = json.loads(existing.get("checklist_json") or "[]")
            anchor = existing.get("anchor")
            added = skipped = 0
            if observed:
                # Each pasted query → its own new manual region (skip exact dups).
                res = self._merge_observed(rewrites, observed)
                rewrites = res["rewrites"]; added = len(res["added"]); skipped = len(res["skipped"])
                self._assign_personas_to_regions(rewrites, brands)
                self.db.upsert_ai_search_cluster(bid, seed_norm, existing.get("seed") or seed,
                                                 anchor, rewrites, checklist, backfilled=0)
            return {"brand_id": bid, "seed": existing.get("seed") or seed, "anchor": anchor,
                    "cluster_size": len(rewrites), "reused": True, "created": False,
                    "manual_regions": added, "gap_regions": 0, "skipped": skipped}

        # Build fresh. Manual queries (if any) DEFINE their own regions and lead; the
        # fan-out then fills ONLY the remaining angles (prior_coverage steers it away from
        # what the manual queries already cover), marked generated.
        self._ensure_personas(brands)
        anchor_summary = self._ground_brand_for_anchor(brands, seed, seed_norm)
        manual_regions = self._regions_from_queries(observed) if observed else []
        raw_observed = len([q for q in observed if str(q).strip()])
        fan = self._fanout_rewrites(
            brands, seed,
            prior_coverage=([r["query"] for r in manual_regions] or None),
            anchor_summary=anchor_summary)
        gap_regions = (fan.get("rewrites") if fan else None) or []
        if not gap_regions and not manual_regions:
            # Surface the real reason (invalid/retired model, bad key, low credit, rate-limit,
            # JSON parse) instead of the opaque generic — ClaudeClient.call stashes last_error.
            why = getattr(self.claude, "last_error", None)
            return {"error": "AI fan-out failed: " + (why or "model returned no rewrites")}
        checklist = (fan.get("checklist") if fan else None) or []
        anchor = (fan.get("anchor") if fan else None) or ""
        # Manual regions FIRST (authoritative/priority), then the auto gap-fill regions.
        rewrites = manual_regions + gap_regions
        self._assign_personas_to_regions(rewrites, brands)
        self.db.upsert_ai_search_cluster(bid, seed_norm, seed, anchor, rewrites, checklist, backfilled=0)
        return {"brand_id": bid, "seed": seed, "anchor": anchor,
                "cluster_size": len(rewrites), "reused": False, "created": True,
                "manual_regions": len(manual_regions), "gap_regions": len(gap_regions),
                "skipped": max(0, raw_observed - len(manual_regions))}

    @staticmethod
    def _brand_facets(brands):
        """First-time / no-seed coverage map: turn a brand's enrichment into a flat,
        de-duped list of concrete FACETS grouped by the intent that fits each:
          use_cases   -> commercial    (the jobs people buy the product to do)
          competitors -> comparison    ("alternative to <competitor>")
          pain_points -> informational (the problems people research)
        Returns {"commercial": [...], "comparison": [...], "informational": [...]}.
        Empty lists when the brand isn't enriched — callers then fall back to the
        normal open-ended generation."""
        def _plist(val):
            if isinstance(val, list):
                return [str(x).strip() for x in val if str(x).strip()]
            if isinstance(val, str) and val.strip():
                try:
                    p = json.loads(val)
                    if isinstance(p, list):
                        return [str(x).strip() for x in p if str(x).strip()]
                except (json.JSONDecodeError, TypeError):
                    pass
                return [s.strip() for s in val.split(",") if s.strip()]
            return []
        blist = brands if isinstance(brands, list) else [brands]
        out = {"commercial": [], "comparison": [], "informational": []}
        seen = set()
        def _add(intent, label):
            label = (label or "").strip()
            k = label.lower()
            if label and k not in seen:
                seen.add(k)
                out[intent].append(label)
        for b in blist:
            for uc in _plist(b.get("use_cases")):
                _add("commercial", uc)
            for cp in _plist(b.get("competitors")):
                _add("comparison", f"alternative to {cp}")
            for pp in _plist(b.get("pain_points")):
                _add("informational", pp)
        return out

    def _fanout_rewrites(self, brands, seed=None, prior_coverage=None, anchor_summary=None):
        """AI-Search mode: one Claude call that simulates how ChatGPT / Perplexity /
        Gemini fan a prompt out into sub-queries, then UNIONs them into a master
        rewrite cluster + a concept/phrasing checklist for the brand's space.

        `prior_coverage` (set of already-covered sub-queries, normalized): when given,
        the fan-out is steered to produce DISTINCT NEW rewrites for the remaining
        space (and any returned rewrite matching prior_coverage is filtered out), so
        re-generating a seed extends the cluster instead of repeating past angles.

        Returns {"anchor": str, "rewrites": [str, ...], "checklist": [str, ...]} or
        None on failure (callers treat None as "no coverage steering" and fall back
        to normal gen). `anchor` is the core platform/use-case the campaign targets
        (e.g. "Instagram Reels"), extracted from the seed (or the brand's primary
        use-case when there's no seed) — generation keeps every title on this anchor.
        The flavoring (per-engine styles) happens INSIDE the single call; the output
        is engine-agnostic because the deliverable (one Reddit thread cluster) is
        shared across engines and retrieval is semantic.
        """
        if isinstance(brands, dict):
            brands = [brands]
        brand_block, target_names, _competitors = self._build_enriched_brand_block(brands)
        seed_line = ""
        if seed and str(seed).strip():
            seed_line = (
                "\nANCHOR SEED (expand the fan-out AROUND this — an existing "
                f"prompt/question, several, or a keyword/platform):\n{str(seed).strip()}\n"
            )
        if anchor_summary and str(anchor_summary).strip():
            seed_line += (
                "\nWHAT THIS BRAND OFFERS FOR THE SEED TOPIC (ground the rewrites in this — "
                "stay within what the brand can credibly answer; do NOT invent capabilities):\n"
                f"{str(anchor_summary).strip()}\n"
            )
        prior_norm = {str(c).strip().lower() for c in (prior_coverage or []) if str(c).strip()}
        if prior_norm:
            _cov = "\n".join(f"  - {c}" for c in list(prior_coverage)[:30])
            seed_line += (
                "\nALREADY COVERED — these sub-queries are already handled by existing "
                "threads. Produce DISTINCT NEW rewrites for the REMAINING space (other "
                "sub-use-cases / buyer concerns / phrasings). Do NOT repeat or lightly "
                f"reword any of these:\n{_cov}\n"
            )
        # Persona lens (title-side): fit yes/maybe personas ground WHICH questions get
        # made + how they're phrased + tag each rewrite. A LENS, not a parallel region
        # taxonomy. Empty when the brand has no (fit) personas → no change.
        personas_block = ""
        _fitp = self._fit_personas(brands)
        if _fitp:
            _plines = "\n".join(
                f"  - {p['label']}: {p.get('profile','')}"
                + (f" | wants: {p.get('goal','')}" if p.get('goal') else "")
                + (f" | says it like: {p.get('vocab','')}" if p.get('vocab') else "")
                for p in _fitp)
            personas_block = (
                "\nPERSONAS — the real askers for this brand (use as a LENS, NOT as separate regions):\n"
                f"{_plines}\n"
                "  • Use them to GROUND the rewrites in how real askers phrase things and to decide "
                "which questions are worth targeting; SKIP any question no persona above would "
                "credibly bring to THIS brand.\n"
                "  • Do NOT create a region per persona or a persona×axis matrix — still ONE rewrite "
                "per DISTINCT region. (Persona→region tagging is assigned separately afterward — you "
                "don't need to label personas here.)\n"
            )
        prompt = f"""You are mapping the AI-search query space for a GEO campaign. The goal is to get
this brand recommended by AI assistants. AI engines REWRITE a user's prompt into
several search sub-queries ("query fan-out") and retrieve SEMANTICALLY, so we need
the full cluster of phrasings around the brand's buying/recommendation intent —
NOT one literal phrasing.

BRAND CONTEXT (ground the queries here; NEVER output the target brand name(s): {', '.join(target_names) or '(none)'}):
{brand_block}
{seed_line}{personas_block}
Do this in your head, then return the merged result:
  1. ANCHOR — identify the core platform / use-case this campaign targets. If the
     seed names a platform or use-case (e.g. "Instagram Reels", "podcast intros",
     "online store checkout"), THAT is the anchor — extract it exactly. If there's
     no seed, derive the anchor from the brand's single most central use-case/
     platform. The anchor is short (1-4 words). Every rewrite stays on this anchor;
     a rewrite may broaden to a NAMED adjacent variant (e.g. Reels → Shorts /
     short-form) but never collapses to something generic ("my videos", "content").
  2. REGIONS — real AI engines fan a prompt into only a FEW sub-queries (ChatGPT
     ~2-3, Perplexity ~3-6, Gemini ~3-5), and across engines they converge on the
     same handful of DISTINCT regions, NOT a dozen synonyms. Produce ONE rewrite per
     region. Consider these 6 standard axes and instantiate the ones that apply to
     THIS brand/seed (SKIP any that don't fit); fill the "Constraint" slot with this
     brand's dominant buying constraint (copyright/licensing, price/free, compliance,
     integration, speed…):
       - Category / top pick       → the buyer wants the leading option in the category
                                      for their need (phrase it naturally — do NOT default
                                      to a "best …" wording)
       - Comparison / alternative  → weighing options, or an alternative to a named competitor
       - Constraint                → the #1 buying concern (e.g. copyright-safe, budget)
       - Use-case / workflow        → the specific job (auto-sync to video, for podcasts)
       - Persona / segment          → for small business, for beginners/pros
       - Adjacent platform          → a NAMED adjacent (Reels → Shorts / TikTok)
     You MAY add up to 2 brand-specific regions the 6 don't capture.
  2b. ENTITY-TYPE — every rewrite must be a query an AI would answer by naming a brand
     of THIS brand's KIND (see its "Category" in the brand context); keep every region
     within that kind. If the brand is a SERVICE / PROVIDER / CLINIC, regions are
     provider-oriented (best service/clinic, where to get it / who offers it,
     alternative to a competitor PROVIDER) — NOT treatment-vs-treatment or efficacy
     ("which works better / does X work") comparisons, whose answer would be substances
     rather than the brand. (For product / retailer brands this is already satisfied —
     keep the usual product / buy-intent regions.)
  3. Write ONE rewrite per region — phrased the way the engines actually search
     (short, keyword-ish, real intent), distinct from the others. VARY the phrasing
     across regions: use whatever wording a real searcher would for THAT region's
     intent; do NOT force a "best …" shape on every region. SELF-CONTAINED RULE: each
     rewrite's `query` MUST itself express that region's DISTINGUISHING QUALIFIER — the
     constraint / segment / use-case / qualifier that makes the region different from
     its siblings (e.g. "free", "audit/report", "for small teams", "copyright-safe").
     A query that drops its region's qualifier and reads generically (so it could
     belong to any region) is WRONG — read on its own it must be unmistakably THIS
     region. HARD RULE: if two rewrites would get essentially the SAME AI answer, keep
     only ONE. Aim for ~5-8 rewrites TOTAL — fewer, distinct regions beat many synonyms.
  3b. For EACH region, also list its VARIANTS — the 2-5 close phrasings/paraphrases an
     engine would issue for that same intent (the different wordings that all map to
     this one region). These are the body-side retrieval surface for that region's thread.
  4. Produce a concise CONCEPT/PHRASING CHECKLIST: the key natural-language
     phrasings, synonyms and domain terms a Reddit thread should contain so it
     matches the whole cluster for both keyword (BM25) and embedding retrieval.

Return JSON only:
{{
  "anchor": "the platform/use-case to keep every title on (short)",
  "rewrites": [
    {{"query": "the sub-query, engine-style", "region": "one of the 6 axis names OR a brand-specific region", "fixed": true/false (true ONLY if it's one of the 6 standard axes), "variants": ["close paraphrase 1", "close paraphrase 2"]}},
    ...
  ],
  "checklist": ["phrasing/term 1", "phrasing/term 2", "..."]
}}
Aim for 5-8 rewrites total. Never include the target brand name."""
        result = self.claude.call(prompt, max_tokens=1500, temperature=0.7)
        if not result or not isinstance(result, dict):
            return None
        rewrites = []
        for r in (result.get("rewrites") or []):
            variants, persona = [], ""
            if isinstance(r, dict):
                q = (r.get("query") or "").strip()
                region = (r.get("region") or "(unsorted)").strip() or "(unsorted)"
                source = "fixed" if r.get("fixed") else "generated"
                persona = (r.get("persona") or "").strip()
                for v in (r.get("variants") or []):
                    vs = str(v).strip()
                    if vs and vs.lower() != q.lower():
                        variants.append(vs)
            else:
                q, region, source = str(r).strip(), "(unsorted)", "generated"
            if not q:
                continue
            if prior_norm and q.lower() in prior_norm:
                continue
            rewrites.append({"query": q, "region": region, "source": source,
                             "variants": variants, "persona": persona})
        rewrites = self._dedup_cap_regions(rewrites)
        # Deterministic, fit-driven persona→region assignment (replaces inline tagging):
        # every region gets a fitting persona or "(broad)", no persona over the cap.
        self._assign_personas_to_regions(rewrites, brands)
        checklist = result.get("checklist") or []
        anchor = (result.get("anchor") or "").strip()
        if not rewrites and not checklist:
            return None
        return {"anchor": anchor, "rewrites": rewrites, "checklist": checklist}

    @staticmethod
    def _dedup_cap_regions(rewrites):
        """Guarantee region-uniqueness + a hard cap on a fan-out rewrite list.
        Keep ONE rewrite per real region (first wins; fixed-source preferred), exempt
        the "(unsorted)" placeholder, then cap at MAX_FANOUT_REWRITES with fixed
        regions first. A same-region duplicate is NOT dropped — its query + variants are
        FOLDED into the kept region's `variants` (so we retain the real phrasings)."""
        def _add_variants(dst, items):
            seen = {v.lower() for v in dst.get("variants", [])}
            seen.add((dst.get("query") or "").strip().lower())
            for v in items:
                vs = str(v).strip()
                if vs and vs.lower() not in seen:
                    seen.add(vs.lower())
                    dst.setdefault("variants", []).append(vs)
        # Fixed-source first so a fixed region beats a generated dup of the same region.
        ordered = sorted(rewrites, key=lambda r: 0 if r.get("source") == "fixed" else 1)
        seen_region, out = {}, []
        for r in ordered:
            r.setdefault("variants", [])
            region = (r.get("region") or "(unsorted)").strip()
            key = region.lower()
            if region != "(unsorted)" and key in seen_region:
                # Region already represented → fold this dup's query + variants in.
                kept = seen_region[key]
                _add_variants(kept, [r.get("query", "")] + (r.get("variants") or []))
                continue
            if region != "(unsorted)":
                seen_region[key] = r
            out.append(r)
        return out[:MAX_FANOUT_REWRITES]

    def _classify_regions(self, queries, existing_regions):
        """Classify each query into a region — reuse an existing region label when it
        fits, else name a NEW short region. Returns [{query, region}] aligned to input.
        Used to dedup manually-pasted fan-out queries by region."""
        queries = [str(q).strip() for q in (queries or []) if str(q).strip()]
        if not queries:
            return []
        ex = "\n".join(f"  - {r}" for r in existing_regions) if existing_regions else "  (none yet)"
        qlist = "\n".join(f"  {i+1}. {q}" for i, q in enumerate(queries))
        prompt = f"""Classify each search query into a REGION = a distinct buyer-concern angle
(e.g. category/best-tool, comparison/alternative, a constraint like copyright or
budget, a use-case, a persona, an adjacent platform).

EXISTING REGIONS — REUSE one of these verbatim if the query fits it:
{ex}

If a query fits NONE of the existing regions, name a NEW short region (2-4 words).

QUERIES:
{qlist}

Return JSON only: {{"regions": ["region for query 1", "region for query 2", "..."]}}"""
        result = self.claude.call(prompt, max_tokens=512, temperature=0.2)
        regions = (result or {}).get("regions") or []
        out = []
        for i, q in enumerate(queries):
            reg = regions[i].strip() if (i < len(regions) and regions[i]) else "(unsorted)"
            out.append({"query": q, "region": reg or "(unsorted)"})
        return out

    def _regions_from_queries(self, queries):
        """Turn pasted REAL fan-out queries into their OWN regions — one region per
        distinct query (the user's captured queries are authoritative; "a region each").
        De-dupes exact repeats (normalized); one Claude call gives each a short 2-4 word
        region label (graceful fallback to a query-derived label). Returns region objects
        [{query, region, source:'manual', variants:[], persona:''}]."""
        seen, uniq = set(), []
        for q in (queries or []):
            q = str(q).strip()
            k = q.lower()
            if q and k not in seen:
                seen.add(k)
                uniq.append(q)
        if not uniq:
            return []
        labels = []
        try:
            qlist = "\n".join(f"  {i+1}. {q}" for i, q in enumerate(uniq))
            prompt = f"""Give each search query a SHORT 2-4 word REGION label naming its buyer-concern
angle (e.g. "comparison / marketplace", "best for small teams", "compliance constraint"). Keep labels
distinct where the queries differ in intent.

QUERIES:
{qlist}

Return JSON only: {{"labels": ["label for query 1", "label for query 2", "..."]}}"""
            res = self.claude.call(prompt, max_tokens=400, temperature=0.2)
            if isinstance(res, dict):
                labels = res.get("labels") or []
        except Exception:
            labels = []
        def _fallback(q):
            w = q.split()
            return " ".join(w[:4]) if w else "(region)"
        out = []
        for i, q in enumerate(uniq):
            lab = (str(labels[i]).strip() if (i < len(labels) and labels[i]) else "") or _fallback(q)
            out.append({"query": q, "region": lab, "source": "manual", "variants": [], "persona": ""})
        return out

    def _merge_observed(self, rewrites, observed_queries):
        """Fold pasted REAL fan-out queries into a cluster: each distinct pasted query
        becomes its OWN new region (source 'manual') — the captured queries are
        authoritative ("a region each"). Skips only an exact duplicate of a query already
        in the cluster (a region query or an existing variant). Returns
        {rewrites, added, skipped}."""
        existing_q = {r["query"].strip().lower() for r in rewrites if r.get("query")}
        for r in rewrites:
            existing_q.update(v.strip().lower() for v in (r.get("variants") or []))
        added, skipped = [], []
        for rw in self._regions_from_queries(observed_queries):
            ql = rw["query"].strip().lower()
            if ql in existing_q:
                skipped.append({"query": rw["query"], "reason": "duplicate query"})
                continue
            rewrites.append(rw)
            existing_q.add(ql)
            added.append({"query": rw["query"], "region": rw["region"]})
        return {"rewrites": rewrites, "added": added, "skipped": skipped}

    def _embed_texts(self, texts):
        """Embedding vectors for `texts` via the OpenAI embeddings REST API. Graceful:
        returns None when OPENAI_API_KEY is unset or on any error → the relevance gate
        no-ops. Plain HTTP (no SDK / new dependency)."""
        if not OPENAI_API_KEY or not texts:
            return None
        try:
            resp = requests.post(
                "https://api.openai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}",
                         "Content-Type": "application/json"},
                json={"model": EMBED_MODEL, "input": texts},
                timeout=20,
            )
            if resp.status_code != 200:
                print(f"[post_gen] embedding gate inactive (API {resp.status_code})")
                return None
            data = resp.json().get("data") or []
            if len(data) != len(texts):
                return None
            return [d.get("embedding") for d in sorted(data, key=lambda d: d.get("index", 0))]
        except Exception as e:
            print(f"[post_gen] embedding gate error: {e}")
            return None

    @staticmethod
    def _cosine(a, b):
        if not a or not b:
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        return dot / (na * nb) if na and nb else 0.0

    def _embedding_gate(self, candidates):
        """Deterministic relevance gate: drop candidates whose (title+body) is below
        EMBED_THRESHOLD cosine similarity to their target_query. No-op (returns input
        unchanged) when embeddings are unavailable — so it never blocks generation."""
        cands = [c for c in candidates if (c.get("target_query") or "").strip()]
        if not cands:
            return candidates
        qv = self._embed_texts([c["target_query"].strip() for c in cands])
        pv = self._embed_texts([((c.get("title") or "") + " " + (c.get("body") or "")).strip() for c in cands])
        if not qv or not pv:
            return candidates  # gate inactive → pass all
        sim = {id(c): self._cosine(q, p) for c, q, p in zip(cands, qv, pv)}
        keep, dropped = [], 0
        for c in candidates:
            if id(c) in sim and sim[id(c)] < EMBED_THRESHOLD:
                dropped += 1
                continue
            keep.append(c)
        if dropped:
            print(f"[post_gen] embedding gate dropped {dropped} off-target candidate(s) "
                  f"(cosine < {EMBED_THRESHOLD})")
        return keep

    def regenerate_body(self, post, brands):
        """Rewrite ONLY the body for an existing post, keeping its title unchanged.
        Grounded in the brand block + the post's ai_search_meta (anchor / target_query /
        persona, when present) so an AI-Search post stays on its region, and governed by
        the shared _BODY_GUIDANCE (ask once). Returns the new body, or "" on failure."""
        if isinstance(brands, dict):
            brands = [brands]
        title = (post.get("title") or "").strip()
        if not title or not brands:
            return ""
        brand_block, target_names, _competitors = self._build_enriched_brand_block(brands)
        target_names_str = ", ".join(target_names) if target_names else "(none)"
        try:
            meta = json.loads(post.get("ai_search_meta") or "{}") or {}
        except (json.JSONDecodeError, TypeError):
            meta = {}
        focus = []
        if meta.get("anchor"):
            focus.append(f"Anchor (keep the body on this topic): {meta['anchor']}")
        if meta.get("target_query"):
            focus.append(f"What this title is really asking for: {meta['target_query']}")
        if meta.get("persona") and meta.get("persona") != "(broad)":
            # Expand the persona LABEL into its full stored detail (pains/goal/vocab/…)
            # so the rewritten body is in that persona's voice, not just label-aware.
            _pv = self._render_persona_voice(
                self._persona_detail_map(brands).get(str(meta["persona"]).strip().lower()))
            focus.append("Write as this person: " + (_pv or str(meta["persona"])))
        focus_block = ("\n" + "\n".join(focus) + "\n") if focus else ""
        storyline = post.get("storyline") or "question"
        banned_sample = ", ".join(random.sample(BANNED_PHRASES, min(8, len(BANNED_PHRASES))))
        prompt = f"""Write a NEW Reddit post BODY for this EXACT title. Do NOT change the title and do
NOT paste it in as a heading. Produce a FRESH body (different angle / wording than before).

TITLE (keep exactly as-is): "{title}"
STORYLINE (shapes the body's voice/scenario only): {storyline}
{focus_block}
BRAND CONTEXT (ground the body here; NEVER name the target brand(s): {target_names_str}):
{brand_block}

BODY RULES:
{_BODY_GUIDANCE}

NEVER USE THESE PHRASES: {banned_sample}

Return JSON only: {{"body": "the new post body"}}"""
        result = self.claude.call(prompt, max_tokens=900, temperature=0.85)
        if isinstance(result, dict):
            return str(result.get("body") or "").strip()
        if isinstance(result, str):
            return result.strip()
        return ""

    def _generate_candidates_for_intent(self, subreddit, brands, intent, storylines,
                                        existing_titles, count, context_only=False,
                                        seed_focus=None, coverage_focus=None,
                                        facet_targets=None, follow_persona=False,
                                        persona_override=None, general=False):
        """Generate `count` candidate posts for a single intent
        (commercial | comparison | informational).

        Each post is a long-tail AI-query title plus a conversational body. Competitor
        brand names are allowed for comparison intent only; target brand names are never
        allowed for any intent.

        `coverage_focus` (AI-Search mode): {"rewrites": [...], "checklist": [...]}
        from the fan-out pass. When set, the batch's titles must collectively span
        DISTINCT rewrites in the cluster and each body must semantically cover the
        checklist phrasings (so the thread is retrievable for the whole query
        cluster, not just one literal phrasing).

        `facet_targets` (standard first-time route): a small list of distinct brand
        offerings (one per post) to cover this batch — deliberate spread without the
        AI-Search fan-out. Ignored when `coverage_focus`/`seed_focus` are set.
        """
        if isinstance(brands, dict):
            brands = [brands]

        brand_block, target_names, competitors = self._build_enriched_brand_block(brands)
        target_names_str = ", ".join(target_names) if target_names else "(none)"
        competitors_str = ", ".join(competitors) if competitors else "(none known)"

        existing_text = ""
        if existing_titles:
            sample = list(existing_titles)[:30]
            existing_text = "\n".join(f'  - "{t}"' for t in sample)
            existing_text = f"\nEXISTING POST TITLES (do NOT repeat these or paraphrase them):\n{existing_text}\n"

        storyline_requests = "\n".join(
            f"  {i+1}. Storyline: {sl} — {STORYLINE_TYPES[sl]}"
            for i, sl in enumerate(storylines[:count])
        )

        banned_sample = ", ".join(random.sample(BANNED_PHRASES, min(8, len(BANNED_PHRASES))))

        # Universal title strategy (ALL intents). The TITLE must be a
        # recommendation-eliciting question — one whose natural AI answer is to
        # name a specific product/service — and narrowed to the brand's niche so
        # THIS brand is the natural pick. Expressed as instructions only: NO
        # example titles and NO fixed pattern (either would become a template the
        # model defaults to, flattening variety and anchoring the domain).
        title_rules = """  3. TITLE = THE RECOMMENDATION QUESTION SOMEONE ASKS AN AI. Write the title as a
     real, natural question a person types into ChatGPT / Perplexity / Google when
     they want a recommendation for what to USE or BUY. The point: when an AI is
     asked this, the helpful answer is to NAME a specific product / service /
     provider — and, given the BRAND CONTEXT above, this brand is one that fits.
     Every title must pass BOTH checks:
       • RECOMMENDATION CHECK — if a helpful AI answered this title, would it
         recommend a specific product/service to use? If it would instead just
         explain a concept, give general tips, or describe "what to look for,"
         the title is wrong — rewrite it as a request for a recommendation.
       • OUR-BRAND CHECK — would THIS brand be a natural answer? The title's answer
         must name the SAME KIND of entity as this brand (read the "Category" line in
         the brand context to decide which kind it is):
           - SPECIFIC product or service → narrow the question by its category /
             who it's for / the problem or use-case it's best at, so it's a
             natural pick and not a query only a big generic market leader wins.
           - SUPPLIER / RETAILER / MARKETPLACE / DIRECTORY that sells or
             aggregates many products or providers → category-level BUY-INTENT
             questions are on-target: asking where to buy / the best place to
             order / who sells the category (online, with fast shipping, with
             financing, for pros, hard-to-find gear, etc.). The brand IS the
             literal answer to those, so do NOT force it down to a single
             product — across the batch, MIX these supplier-level questions with
             more specific product/task ones.
           - SERVICE / PROVIDER / CLINIC (you go to it to GET something done) →
             the title asks for the PROVIDER: the best service / clinic / where to
             get it / who offers it for the use-case. Do NOT frame it as "best
             <treatments>", "<A> vs <B> — which works better?", or "does <X> work?":
             those are answered with substances / efficacy, not a provider, so this
             brand can't be the cited answer. (A comparison is fine only when it
             compares PROVIDERS, e.g. "alternative to <competitor clinic>".)
         Either way, NEVER write the target brand name in the title, and never
         turn it into a generic info / "do they work" / how-it-works question.
  4. Keep titles short (~3-12 words) and human — a real person could post it. VARY
     the wording and structure across the batch; do NOT fall into one repeated
     shape, do NOT keyword-stuff, do NOT append years ("2025"). Mix more
     conversational phrasings with more direct search-style ones, but EVERY title
     must still lead to recommending a specific product/service.
  5. STORYLINE shapes the BODY's voice / scenario ONLY — the TITLE is ALWAYS a
     recommendation question per rules 3-4, NEVER a vent, testimonial, rant, or
     status update. So a "complaint" storyline becomes a body that frames the
     frustration and then ASKS what to use (title stays a question like "best X
     for Y?"); a "discovery"/"experience" storyline becomes a body sharing the
     situation that ends in asking for recommendations — the title never reads as
     "so tired of X" or "started using Y — game changer"."""

        # Subreddit scoping line. In context_only mode (no subreddit chosen
        # yet) we deliberately DON'T name a subreddit — the post is grounded
        # purely in brand context so it can be placed in whichever subreddit
        # fits best when it's assigned later. We also tell the model to make
        # the batch collectively cover ALL of the brand's offerings.
        if context_only:
            scope_line = (
                f"Generate {count} Reddit posts grounded in the brand context "
                "below. A subreddit will be chosen and assigned later, so do NOT "
                "reference any specific subreddit — write posts that would feel "
                "native in whatever niche community matches the topic.\n\n"
                "COVER ALL OFFERINGS: across this batch (and together with the "
                "other intents being generated), collectively span the brand's "
                "full range of use-cases, features and pain-points listed below "
                "— don't cluster every post on the same one offering."
            )
        else:
            scope_line = (
                f"Generate {count} Reddit posts for r/{subreddit['name']}.\n\n"
                f"SUBREDDIT DOMAIN: {subreddit['domain']}"
            )

        # Optional SEED FOCUS: narrow the batch to expand AROUND a user-supplied
        # seed (an existing prompt/question, several, or a keyword/platform). The
        # seed is the THEME/direction — NOT a template to copy — so all the title
        # rules and brand scoping below still apply.
        seed_block = ""
        if seed_focus and str(seed_focus).strip():
            seed_block = (
                "\n\nSEED FOCUS — generate this batch AROUND the following seed (an "
                "existing prompt/question, several of them, or a keyword/platform):\n"
                f"{str(seed_focus).strip()}\n"
                "Treat the seed as the THEME / PLATFORM / use-case to expand. Every "
                "title must stay in that same space (same platform/topic) and be a "
                "DIFFERENT, complementary recommendation question a real person would "
                "ask there — strengthening coverage of this theme. Do NOT repeat the "
                "seed verbatim or just reword it; explore adjacent angles within it. "
                "All the TITLE rules and brand scoping below still apply."
            )

        # Optional AI-SEARCH COVERAGE: when the fan-out pass supplied a rewrite
        # cluster + phrasing checklist, steer the batch to BLANKET the cluster's
        # semantic space (titles cover distinct rewrites; bodies carry the
        # checklist phrasings) so the thread is retrievable however an AI engine
        # paraphrases the prompt. Instruction-only — the rewrites/checklist are
        # the SPACE TO COVER, not titles to copy.
        coverage_block = ""
        coverage_json_field = ""
        if coverage_focus:
            _rw = [str(r).strip() for r in (coverage_focus.get("rewrites") or []) if str(r).strip()]
            _ck = [str(c).strip() for c in (coverage_focus.get("checklist") or []) if str(c).strip()]
            _anchor = (coverage_focus.get("anchor") or "").strip()
            _vbq = coverage_focus.get("variants_by_query") or {}
            _target_region = (coverage_focus.get("target_region") or "").strip()
            if _rw or _ck:
                # Show each rewrite WITH its own variant phrasings (the real ways that
                # intent gets asked) so the post targeting it can pack them into the body.
                # Persona voice per rewrite (only when "follow personas" is ON): expand the
                # region's assigned persona LABEL into its full detail so the post is WRITTEN
                # as that asker, not just tagged with the label afterward.
                _pbq_voice = (coverage_focus or {}).get("persona_by_query", {}) if follow_persona else {}
                _pdetail = self._persona_detail_map(brands) if follow_persona else {}
                def _rw_line(r):
                    vs = [str(v).strip() for v in (_vbq.get(r.strip().lower()) or []) if str(v).strip()]
                    tail = f"   [also asked as: {'; '.join(vs[:6])}]" if vs else ""
                    ptail = ""
                    if follow_persona:
                        _plabel = str(_pbq_voice.get(r.strip().lower()) or "").strip()
                        if _plabel and _plabel != "(broad)":
                            _voice = self._render_persona_voice(_pdetail.get(_plabel.lower()))
                            if _voice:
                                ptail = f"\n      ↳ WRITE THIS ONE AS: {_voice}"
                    return f"  - {r}{tail}{ptail}"
                rw_lines = "\n".join(_rw_line(r) for r in _rw[:25])
                ck_str = "; ".join(_ck[:25])
                anchor_rule = ""
                if _anchor:
                    anchor_rule = (
                        f"ANCHOR = \"{_anchor}\". EVERY title must stay on this anchor "
                        "(its platform/use-case must be present or unmistakably implied). "
                        "A title may broaden to a NAMED adjacent variant only if its "
                        "assigned rewrite already does so — it must NEVER drift to a fully "
                        "generic phrasing (e.g. \"my videos\", \"content\") that drops the "
                        "anchor. A title that loses the anchor is a FAILED title.\n"
                    )
                # Region fidelity (single-rewrite path): when this post targets ONE
                # rewrite that belongs to a MEANINGFUL (descriptive, non-axis) region,
                # require the title to keep that region's distinguishing qualifier so it
                # can't collapse onto a sibling region (e.g. "free audit/report" → a
                # generic "which agency…"). Skipped for bare axis/placeholder regions
                # (the query is authoritative there) and multi-rewrite batches.
                region_rule = ""
                if len(_rw) == 1 and _is_meaningful_region(_target_region):
                    region_rule = (
                        f"REGION = \"{_target_region}\". This thread targets that SPECIFIC "
                        "region. The title MUST preserve the region's DISTINGUISHING "
                        "QUALIFIER — the constraint / segment / qualifier that makes it "
                        "different from sibling regions (e.g. \"free\", \"audit/report\", "
                        "\"for small teams\"). A title that stays on the anchor but DROPS "
                        "the region's qualifier (so it reads like a generic version that "
                        "could belong to another region) is a FAILED title.\n"
                    )
                coverage_block = (
                    "\n\nAI-SEARCH COVERAGE — this batch is part of a campaign to get the "
                    "brand recommended by AI engines (ChatGPT / Perplexity / Gemini), which "
                    "RE-WRITE a user's prompt into many sub-queries and retrieve SEMANTICALLY. "
                    "Your job is to BLANKET that query space, not match one phrasing.\n"
                    f"{anchor_rule}{region_rule}"
                    "REWRITE CLUSTER (the distinct sub-queries the engines fan out to):\n"
                    f"{rw_lines}\n"
                    "  • Each title must TARGET exactly ONE rewrite from this cluster, and "
                    "different titles in the batch must target DIFFERENT rewrites — no two "
                    "titles on the same rewrite, no near-duplicates.\n"
                    "  • Report which rewrite each title targets in its \"target_query\" field "
                    "(copy the rewrite text it covers).\n"
                    "  • Each title must be a RECOMMENDATION QUESTION (an AI would answer it by "
                    "naming a product/service) — never a vent or bare statement.\n"
                    "  • These are the SPACE to cover, NOT templates — phrase each title as a "
                    "natural human question (all TITLE rules below still apply); never paste a "
                    "rewrite verbatim.\n"
                    "  • Use the targeted rewrite's \"also asked as\" phrasings ONLY to choose "
                    "natural vocabulary — do NOT restate them as separate asks in the body (they are "
                    "near-duplicates of the one question). Ask once.\n"
                )
                if ck_str:
                    coverage_block += (
                        "PHRASING CHECKLIST (fold these DISTINCT terms/concepts into the body as "
                        "concrete detail, once each, so both keyword (BM25) and embedding retrieval "
                        f"match — NOT as extra questions): {ck_str}\n"
                    )
                coverage_json_field = ',\n            "target_query": "the ONE rewrite from the cluster this title targets"'

        # Optional FIRST-TIME COVERAGE TARGETS (standard route, no seed): the caller
        # derived distinct brand facets (one per post) from the brand's enrichment so
        # a first-time batch spans the brand instead of clustering. Instruction-only —
        # the facets are the SPACE to cover, not titles to copy. Mutually exclusive
        # with seed/coverage blocks above.
        facet_block = ""
        if facet_targets and not coverage_focus and not (seed_focus and str(seed_focus).strip()):
            _ft = [str(f).strip() for f in facet_targets if str(f).strip()]
            if _ft:
                ft_lines = "\n".join(f"  - {f}" for f in _ft)
                _extra = count - len(_ft)
                extra_rule = (
                    f"  • For the remaining {_extra} post(s) beyond these targets, pick OTHER "
                    "distinct brand offerings/angles — never repeat a target or an offering "
                    "already used in this batch.\n"
                ) if _extra > 0 else ""
                facet_block = (
                    "\n\nCOVERAGE TARGETS — to give this batch deliberate spread, cover the "
                    "brand's DISTINCT offerings below, ONE per post (no two posts on the same one):\n"
                    f"{ft_lines}\n"
                    "  • Each title TARGETS exactly ONE item above and must be a natural "
                    "RECOMMENDATION QUESTION for it (a helpful AI would answer by naming a "
                    "product/service); different posts target DIFFERENT items.\n"
                    f"{extra_rule}"
                    "  • These are the SPACE to cover, NOT templates — phrase each as a real "
                    "human question (all TITLE rules below still apply); never paste an item verbatim.\n"
                    "  • Report which item each title targets in its \"target_query\" field.\n"
                )
                coverage_json_field = ',\n            "target_query": "the ONE coverage target this title covers"'

        # Persona voice (only when "follow personas" is ON):
        #  - direct/override: one picked persona for the whole batch → write every post as them.
        #  - cluster: each rewrite line carries a "↳ WRITE THIS ONE AS: …" note → honor it.
        persona_block = ""
        if follow_persona and persona_override:
            _ov_voice = self._render_persona_voice(persona_override)
            if _ov_voice:
                persona_block = (
                    "\n\nWRITE AS THIS PERSON — every post in this batch is asked by them; adopt "
                    "their situation, pains, goals, and the way they phrase things (this shapes "
                    "VOICE + concrete detail, not the topic):\n"
                    f"  {_ov_voice}\n"
                )
        elif follow_persona and coverage_focus:
            persona_block = (
                "\n\nPERSONA VOICE — a rewrite above may carry a '↳ WRITE THIS ONE AS: …' note: "
                "write THAT post as that person (their situation, pains, goals, vocabulary) while "
                "still answering the rewrite. This shapes voice + detail, NOT which rewrite you target.\n"
            )

        # Shared header + intent-specific tail
        general_block = _GENERAL_STYLE_BLOCK if general else ""
        header = f"""{scope_line}{seed_block}{coverage_block}{facet_block}{persona_block}

BRAND CONTEXT (for grounding the queries — NEVER mention the target brand names):
{brand_block}

{existing_text}
REQUESTED STORYLINES (for secondary variety):
{storyline_requests}

GOAL: Dual-optimize for Reddit AND for LLM retrieval (ChatGPT / Perplexity /
Claude). The post must:
  (a) Survive Reddit moderation and get engagement (so it makes it into the
      corpus that LLMs retrieve from in the first place) — the TITLE has to
      look like a real person posted it, NOT a templated marketing query.
  (b) Rank for the underlying long-tail query when an LLM searches for it — the
      BODY includes the key terms ONCE within a believable story (the brand's
      category / audience / pain-point words appear as the user describes their
      situation). LLM retrieval is both keyword (BM25) and semantic (embeddings);
      ONE natural phrasing of the question plus concrete contextual detail covers
      both. Do NOT repeat the question in several phrasings to "cover more".

These two goals are NOT in conflict if you do it right: a human title, and a body
that asks once and otherwise reads like a real person giving context.

STRICT RULES:
  1. NEVER mention any of the TARGET brand names: {target_names_str}
  2. For commercial and informational intents: also avoid all competitor names.
     For comparison intent ONLY: competitor names from the list ARE allowed and encouraged.
{title_rules}
{_BODY_GUIDANCE}
{general_block}
  • Variety check: across this batch the {count} titles MUST use noticeably
    different shapes/openings. Don't reuse the same template twice.

NEVER USE THESE PHRASES: {banned_sample}
"""

        if intent == "commercial":
            intent_tail = """
INTENT: COMMERCIAL — the person is ready to CHOOSE. Per the TITLE rules above, the
title is a question that asks for a recommendation of what to use/buy for their
specific situation, narrowed (via the brand context) to the niche where this brand
is a natural pick — so a helpful AI answers by naming a specific product/service.
Do not write a generic "do they work / what to look for / how does it work"
question; the answer to a commercial title must be a recommendation.

BODY: 2-4 short paragraphs that PAY OFF the title with SPECIFIC buying context —
who they are (situation / scale), exactly what's broken, what they've already
tried, what they need next. Restate the title's question in natural sentences and
weave in the brand's category / audience / pain-point / use-case terms as the
person explains their situation (this is where GEO ranking comes from). Real,
first-person voice — hesitation, specifics, incomplete sentences are fine; never
pitch, never name the target brand.
"""
        elif intent == "comparison":
            intent_tail = f"""
INTENT: COMPARISON — the person is weighing options. Per the TITLE rules above, the
title is a recommendation question framed around a switch / alternative / matchup.
You MAY (and should) name a competitor brand from this list to anchor it:
  COMPETITORS: {competitors_str}
You must still NEVER mention the TARGET brand name(s): {target_names_str}.

Frame the question so the natural answer is which option to pick for the person's
SPECIFIC need — leaving room for "our kind of product/service" (the unnamed better
fit, narrowed via the brand context to this brand's niche) to be the recommended
answer. Across the batch, only one title at most should be a bare "X vs Y" matchup;
the rest should come at the comparison from different angles (switching from a
named competitor, looking for an alternative for a specific use-case, etc.) — vary
the wording, don't settle into one shape. If the COMPETITORS list is empty,
reference competitors by attribute instead of by name.

BODY: should sound genuinely undecided. Open with the person's current setup, why
it's not working, what they've heard about the alternatives, what they're worried
about. Restate the comparison question in natural sentences and weave in the
brand's audience / use-case / pain-point terms (this is where GEO ranking comes
from). Don't shill any option; never name the target brand.
"""
        else:  # informational
            intent_tail = """
INTENT: INFORMATIONAL — the person wants to SOLVE A PROBLEM or reach an OUTCOME,
not just understand theory. Per the TITLE rules above, frame the title as a
question whose best answer is to recommend a specific product/approach to achieve
that outcome in the brand's niche — NOT an abstract "how does X work" or "what is X"
explainer (those don't lead to recommending anything). Narrow it (via the brand
context) to the situation where this brand is the natural recommendation. Compare
CONCEPTS if useful, never brand names; never name the target brand.

BODY: 2-4 paragraphs of real situational context — who they are, what they're
trying to achieve, what they've already tried, where they're stuck — that lead
toward the recommended approach. Restate the title's question in natural sentences
and weave in the brand's category / pain-point / use-case terms (this is where GEO
ranking comes from).
"""

        json_tail = """
Return JSON only:
{
    "posts": [
        {
            "title": "The long-tail AI query",
            "body": "2-4 paragraph first-person body with context",
            "storyline": "storyline type from the list above"%s
        }
    ]
}""" % coverage_json_field

        prompt = header + intent_tail + json_tail
        result = self.claude.call(prompt, max_tokens=4000, temperature=0.9)
        if not result or "posts" not in result:
            return []
        return result["posts"]

    def _generate_candidates(self, subreddit, brands, storylines, existing_titles, count):
        """Generate candidate posts via Claude. brands can be a list or single dict."""
        if isinstance(brands, dict):
            brands = [brands]

        existing_text = ""
        if existing_titles:
            sample = list(existing_titles)[:30]
            existing_text = "\n".join(f'  - "{t}"' for t in sample)
            existing_text = f"\nEXISTING POST TITLES (do NOT repeat these or anything similar):\n{existing_text}\n"

        storyline_requests = "\n".join(
            f"  {i+1}. Storyline: {sl} — {STORYLINE_TYPES[sl]}"
            for i, sl in enumerate(storylines[:count])
        )

        # Build brand context section — single or multi-brand
        all_keywords = []
        if len(brands) == 1:
            brand = brands[0]
            brand_context_text = f"BRAND CONTEXT (for understanding the domain ONLY — do NOT mention this brand): {brand['context']}"
            kw = json.loads(brand.get("keywords", "[]")) if brand.get("keywords") else []
            all_keywords.extend(kw)
        else:
            lines = []
            for b in brands:
                lines.append(f"  - {b['name']}: {b['context']}")
                kw = json.loads(b.get("keywords", "[]")) if b.get("keywords") else []
                all_keywords.extend(kw)
            brand_context_text = "BRAND CONTEXTS (for understanding the domain ONLY — do NOT mention ANY brand):\n" + "\n".join(lines)

        keywords_text = f"\nCOMBINED KEYWORDS (for domain context, do NOT mention any brand): {', '.join(all_keywords)}" if all_keywords else ""
        brand_names = ", ".join(b["name"] for b in brands)

        prompt = f"""Generate {count} Reddit posts for r/{subreddit['name']}.

SUBREDDIT DOMAIN: {subreddit['domain']}
{brand_context_text}
{keywords_text}
{existing_text}
REQUESTED STORYLINES:
{storyline_requests}

CRITICAL RULES:
1. Posts must NEVER mention any brand name ({brand_names}) — they are generic domain questions/experiences
2. Posts should NOT look AI-generated or markety
3. Each post must feel like a real person wrote it
4. Each title is a genuine recommendation question — its natural answer names a specific product/service to use or buy. Phrase it the way a real person would actually ask, and VARY the structure and opening word across the batch; do NOT default every title to a "best …" shape.
5. Vary tone: some casual, some detailed, some frustrated, some curious
6. Body text should be 2-4 paragraphs, conversational, with personal context
7. Include minor imperfections: occasional typos, incomplete thoughts, run-on sentences
8. Posts should be relevant to the domain area covered by ALL the brands listed above

NEVER USE THESE PHRASES: {', '.join(random.sample(BANNED_PHRASES, min(8, len(BANNED_PHRASES))))}

Return JSON only:
{{
    "posts": [
        {{
            "title": "Post title",
            "body": "Post body text",
            "storyline": "storyline type"
        }}
    ]
}}"""

        result = self.claude.call(prompt, max_tokens=4000, temperature=0.9)
        if not result or "posts" not in result:
            return []

        return result["posts"]

    def _score_ai_query_relevance(self, title, body, anchor=None, target_query=None, brand_kind=None, region=None):
        """Score 0-10 combining (a) likelihood a real person types this query
        with (b) whether its natural answer is a PRODUCT/SERVICE RECOMMENDATION.

        We want titles an AI would answer by NAMING a specific product/service
        (so the brand seeded in the comments can be the recommendation). Generic
        info / efficacy / how-it-works questions — whose answer is an explanation,
        not a recommendation — score low and get dropped by _select_best.

        AI-Search mode (anchor given): the score ALSO enforces ANCHOR RETENTION
        (the title keeps the campaign's platform/use-case) and QUESTION FORM (a
        recommendation question, not a vent/statement), and rewards a clean match
        to its `target_query`. Off-anchor or vent titles score LOW so the
        coverage-gated selector drops them. anchor=None → original behavior.

        `brand_kind` (the brand's category, e.g. "telehealth men's health clinic"):
        when set, enforce ENTITY-TYPE MATCH — the title's natural answer must name a
        brand of THIS kind. A title whose answer is a different kind of thing (e.g.
        a list of treatments/molecules, or an efficacy comparison, when the brand is
        a provider/clinic) is capped. Conservative + relative: only a CLEAR mismatch
        caps; same-kind queries (incl. valid same-kind comparisons) are untouched;
        empty brand_kind → rule omitted entirely.
        """
        entity_block = ""
        if brand_kind and str(brand_kind).strip():
            entity_block = f"""

ENTITY-TYPE MATCH (relative to the brand — apply CONSERVATIVELY):
  BRAND KIND: "{str(brand_kind).strip()}". A high-scoring title's natural answer must
  NAME a brand of THIS kind. Cap the score at 3-4 ONLY when the answer would clearly
  name a DIFFERENT kind of thing than the brand — e.g. the brand is a SERVICE /
  PROVIDER / CLINIC but the title's answer is a list of treatments / ingredients /
  molecules / products, or an efficacy "which works better / does X work" comparison
  (those name substances, not a provider, so this brand can't be the cited answer).
  Do NOT cap when the answer's kind MATCHES the brand — including valid same-kind
  comparisons (product vs product for a product brand; clinic vs clinic for a clinic).
  When unsure, do NOT apply this cap."""

        region_block = ""
        if region and _is_meaningful_region(region):
            region_block = f"""

REGION FIDELITY (the title must honor its region):
  REGION = "{str(region).strip()}". This title is supposed to target that SPECIFIC
  region. Cap the score at 4 when the title keeps the broad topic but DROPS the
  region's DISTINGUISHING QUALIFIER — the constraint / segment / qualifier that
  separates it from sibling regions (e.g. region is "free audit/report" but the title
  reads as a generic "which agency can do X", losing "free" + "audit/report"). A title
  that could equally belong to a different region of the same brand has lost its region.
  Do NOT cap when the title clearly carries the region's qualifier. When unsure, do NOT cap."""

        anchor_block = ""
        if anchor:
            tq = f'\nThis title is supposed to target the cluster sub-query: "{target_query}".' if target_query else ""
            anchor_block = f"""

AI-SEARCH MODE — additionally enforce (these can CAP the score):
  ANCHOR = "{anchor}". The title MUST keep this platform/use-case (present or
  unmistakably implied). If the title has drifted to a generic phrasing that drops
  the anchor (e.g. "...for my videos" when the anchor is "Instagram Reels"), cap the
  score at 4 — it's off-target and weak.
  QUESTION FORM. The title must read as a recommendation QUESTION, not a vent or
  bare statement ("so tired of X", "need better Y"). A statement/vent caps at 5
  even if on-anchor.{tq}
  A strong title is: on-anchor + a recommendation question + cleanly maps to its
  target sub-query."""

        prompt = f"""You are scoring a Reddit post TITLE for a GEO campaign whose goal is to get a
specific product/service recommended by AI assistants (ChatGPT / Perplexity / Google).

Title: "{title}"
Body preview: "{body[:200]}"

Rate 0-10 on BOTH dimensions together:
  (1) How likely a real person types this exact question (or close paraphrase) into an AI / search engine, AND
  (2) Whether the NATURAL ANSWER is to RECOMMEND a specific product / brand / service to use or buy.

High (8-10): Clearly recommendation-seeking — a helpful AI would answer by NAMING a specific product / service / supplier to use or buy. Judge by the ANSWER the title would get, NOT by how it is worded: any phrasing qualifies as long as the natural answer is a specific recommendation. Do NOT favor a "best …" wording over equally-valid recommendation questions.
Medium (5-7): Advice-seeking that MIGHT surface a product recommendation ("has anyone tried X", "what do you use for Y").
Low (1-4): Generic information / efficacy / how-it-works / "what to look for" / concept questions where the answer is an EXPLANATION rather than a product recommendation (e.g. "do X actually work", "how does X work", "what is X"); also rants, memes, very personal one-offs. CRITICAL: first-person VENTS, TESTIMONIALS and STATUS UPDATES that don't ASK for anything are NOT recommendation-seeking — score them 1-4 even if on-topic (e.g. "frustrated with traditional doctors dismissing X", "so tired of Y", "started using Z — game changer", "X changed my life", "finally found something that works"). A title only scores high if it explicitly asks for what to use/buy/try.{anchor_block}{region_block}{entity_block}

Return JSON only:
{{"score": 0-10, "reasoning": "brief explanation"}}"""

        result = self.claude.call(prompt, max_tokens=256, temperature=0.3)
        if result and "score" in result:
            return result["score"]
        return 5  # default middle score

    def _select_best(self, candidates, requested_storylines, count, threshold=5):
        """Select up to N candidates by AI-query score with storyline variety.

        QUALITY FLOOR: only titles scoring >= `threshold` qualify. This drops
        vents / testimonials / status-update titles (which the recommendation-
        question scorer now caps at 1-4) even when they'd otherwise be pulled in
        to fill a requested storyline slot (complaint / discovery / experience).
        If fewer than `count` clear the bar, return fewer and log — never pad the
        batch with weak/vent titles."""
        strong = [c for c in candidates if c.get("ai_query_score", 0) >= threshold]
        strong.sort(key=lambda c: c.get("ai_query_score", 0), reverse=True)

        selected = []
        # First pass: storyline variety, but ONLY among candidates that cleared the floor.
        for sl in requested_storylines:
            for c in strong:
                if c in selected:
                    continue
                if c.get("storyline") == sl:
                    selected.append(c)
                    break

        # Second pass: fill remaining slots with the highest-scoring strong, unused candidates.
        for c in strong:
            if len(selected) >= count:
                break
            if c not in selected:
                selected.append(c)

        if len(selected) < count:
            print(f"[post_gen] standard select: kept {len(selected)} of {count} requested — only "
                  f"that many titles cleared the recommendation-question bar (score>={threshold}); "
                  "weak/vent/testimonial titles were dropped rather than padded.")
        return selected[:count]

    def _select_cluster_best(self, candidates, count, threshold=6):
        """AI-Search coverage-gated selection: return up to `count` STRONG posts
        that each cover a DISTINCT rewrite (target_query).

        - Only candidates scoring >= `threshold` qualify (drops off-anchor / vent /
          weak titles that the anchor-aware scorer pushed down).
        - Picks the single highest-scoring candidate per distinct target_query, so
          the kept batch spans different cluster regions instead of duplicating one.
        - Falls back to the (deduped) title when a candidate has no target_query.
        - If fewer than `count` distinct strong rewrites exist, fills remaining
          slots with the next-best qualifying candidates and prints the shortfall
          (no silent truncation).
        """
        strong = [c for c in candidates if c.get("ai_query_score", 0) >= threshold]
        strong.sort(key=lambda c: c.get("ai_query_score", 0), reverse=True)

        # One strong post per DISTINCT rewrite. We deliberately prefer distinct
        # coverage over hitting `count`: padding the batch with a second post on a
        # rewrite that's already covered just recreates the weak/redundant posts
        # this selector exists to prevent. If too few distinct strong rewrites
        # exist, return fewer and say so (no silent padding).
        selected, seen_targets = [], set()
        for c in strong:
            key = (c.get("target_query") or c.get("title") or "").strip().lower()
            if key in seen_targets:
                continue
            seen_targets.add(key)
            selected.append(c)
            if len(selected) >= count:
                break

        if len(selected) < count:
            print(f"[post_gen] AI-Search: kept {len(selected)} of {count} requested — "
                  f"only that many distinct rewrites cleared the quality bar "
                  f"(score>={threshold}). Weak/off-anchor/duplicate candidates were "
                  "dropped rather than padded; broaden the seed or lower counts for more.")
        return selected[:count]

    def _generate_image_prompt(self, title, body, storyline):
        """Generate a search query for sourcing a relevant image.

        Returns a search query string for Unsplash/Pexels, or None if no image is appropriate.
        """
        # Some storylines benefit from images more than others
        if storyline in ("question",) and random.random() > 0.3:
            return None  # Most questions don't need images

        prompt = f"""Should this Reddit post include an image? If yes, generate a search query for finding a relevant stock photo.

Post title: "{title}"
Post body preview: "{body[:200]}"
Post type: {storyline}

Rules:
- Only suggest an image if it adds authenticity (e.g., a photo of the situation, a relevant scene)
- The image should look like something a real Reddit user would attach (phone photo quality, not polished)
- Search query should find REAL photos, not illustrations or AI art
- For experience posts: related activity or context photos
- For complaints: sometimes a screenshot or photo of the problem
- For comparisons: usually no image needed
- For PSAs: sometimes an infographic or relevant scene

Return JSON only:
{{"needs_image": true/false, "search_query": "search terms for Unsplash/Pexels (or empty string)"}}"""

        result = self.claude.call(prompt, max_tokens=256, temperature=0.5)
        if result and result.get("needs_image") and result.get("search_query"):
            return result["search_query"]
        return None

    def search_image(self, query):
        """Search for a stock photo using Unsplash API (no key required for low volume).

        Returns image URL or None.
        """
        if not query:
            return None

        try:
            # Unsplash source (no API key needed, returns a random photo)
            # For production use, register for an API key
            resp = requests.get(
                f"https://source.unsplash.com/800x600/?{query}",
                headers={"User-Agent": REDDIT_USER_AGENT},
                timeout=10,
                allow_redirects=True
            )
            if resp.status_code == 200 and resp.url:
                return resp.url
        except requests.exceptions.RequestException:
            pass

        return None
