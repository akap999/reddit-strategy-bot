#!/usr/bin/env python3
"""FU220 — the blog scoreboard runner.

Scores stored blogs with generators/blog_eval.py (grounding · defects · rubric), prints a table and
writes one JSON report per run. Run it where the database and the API key live — on Railway — with
`--remote`, which uploads this file + generators/blog_eval.py to the volume and starts the run
detached (a railway ssh session can drop; the run keeps going):

  python3 tools/blog_eval.py --remote --list [--brand-ids 23,34] [--limit 40]
  python3 tools/blog_eval.py --remote --ids 142,137 --label baseline
  python3 tools/blog_eval.py --remote --latest-per-seed --brand-ids 23,34 --limit 12 --label baseline
  python3 tools/blog_eval.py --remote --status baseline          # progress, then fetch the report
  python3 tools/blog_eval.py --compare baseline after-factcheck   # reads ./evals/ (fetched reports)

Flags: --no-grounding, --no-rubric (defects only, $0); --factcheck scores what the automatic
fact-check WOULD produce (applied in memory, never saved). Nothing here writes to a blog.

FU251 adds `--replay`, which runs every DETERMINISTIC check over every stored body and reports what
they would change — no model, no network, no write, so it costs nothing and takes about a minute:

  python3 tools/blog_eval.py --replay [--detail] [--brand-ids 34] [--limit 500]

FU252 adds `--rewrites`, the same idea for the REWORDING pass — it compares every stored body with
its own rewrite and reports what changed about what the article ASSERTS (a lost figure bound, a
dropped link, a strengthened claim, a changed acronym expansion, a bold subject that lost its verb):

  python3 tools/blog_eval.py --rewrites [--detail]

Run both before AND after any generator change. They are the gate the project kept skipping.
"""
import argparse
import base64
import glob
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REMOTE_APP = "/app"
REMOTE_DIR = "/data/evals"
REPORT_MARK = "===FU220-REPORT==="


def _eval_dir():
    d = os.environ.get("BLOG_EVAL_DIR") or (REMOTE_DIR if os.path.isdir("/data") else str(HERE.parent / "evals"))
    os.makedirs(d, exist_ok=True)
    return d


def _load_eval_module():
    """The eval module next to this file first (so an uploaded copy wins over an older deployed one),
    then the app's package."""
    sys.path.insert(0, os.getcwd())
    sys.path.insert(0, str(HERE.parent))
    local = HERE.parent / "generators" / "blog_eval.py"
    if local.exists():
        spec = importlib.util.spec_from_file_location("blog_eval_fu220", str(local))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    from generators import blog_eval as mod
    return mod


# ── remote (Railway) ─────────────────────────────────────────────────────────────────────────────────
def _railway(cmd, timeout=180):
    return subprocess.run(["railway", "ssh", "--", cmd], capture_output=True, text=True, timeout=timeout)


def _upload_cmd():
    parts = [f"mkdir -p {REMOTE_DIR}/code/tools {REMOTE_DIR}/code/generators"]
    for rel in ("tools/blog_eval.py", "generators/blog_eval.py"):
        b64 = base64.b64encode((HERE.parent / rel).read_bytes()).decode()
        parts.append(f"echo {b64} | base64 -d > {REMOTE_DIR}/code/{rel}")
    return " && ".join(parts)


def run_remote(argv):
    args = [a for a in argv if a != "--remote"]
    if "--status" in args:
        label = args[args.index("--status") + 1]
        r = _railway(f"tail -n 25 {REMOTE_DIR}/{label}.log 2>/dev/null; "
                     f"f=$(ls -t {REMOTE_DIR}/{label}-*.json 2>/dev/null | head -1); "
                     f"if [ -n \"$f\" ]; then echo {REPORT_MARK} $f; cat $f; fi")
        out = r.stdout
        if REPORT_MARK in out:
            log, rest = out.split(REPORT_MARK, 1)
            print(log)
            name, _, js = rest.strip().partition("\n")
            os.makedirs(HERE.parent / "evals", exist_ok=True)
            dest = HERE.parent / "evals" / os.path.basename(name.strip())
            dest.write_text(js)
            print(f"report saved → {dest}")
        else:
            print(out or r.stderr)
        return
    quick = "--list" in args or "--compare" in args
    run = f"cd {REMOTE_APP} && python3 {REMOTE_DIR}/code/tools/blog_eval.py " + " ".join(shlex.quote(a) for a in args)
    if quick:
        r = _railway(_upload_cmd() + " && " + run, timeout=300)
        print(r.stdout or r.stderr)
        return
    label = args[args.index("--label") + 1] if "--label" in args else "eval"
    detached = (f"setsid nohup sh -c {shlex.quote(run)} > {REMOTE_DIR}/{label}.log 2>&1 < /dev/null & "
                f"sleep 2; echo started; tail -n 3 {REMOTE_DIR}/{label}.log")
    r = _railway(_upload_cmd() + " && " + detached)
    print(r.stdout or r.stderr)
    print(f"poll with: python3 tools/blog_eval.py --remote --status {label}")


# ── selection ────────────────────────────────────────────────────────────────────────────────────────
def _db():
    sys.path.insert(0, os.getcwd())
    from config import DB_PATH
    from db import Database
    d = Database(DB_PATH)
    d.connect()
    return d


def _candidates(db, brand_ids=None, limit=40):
    """The latest finished version of each (brand, seed) — a regenerate overwrites its row, and a seed
    generated twice as separate blogs keeps only the newest row."""
    q = ("SELECT b.id, b.brand_id, br.name AS brand, b.seed, b.status, b.updated_at, "
         "length(b.body_markdown) AS chars FROM blogs b LEFT JOIN brands br ON br.id = b.brand_id "
         "WHERE b.status != 'awaiting_sources' AND length(coalesce(b.body_markdown,'')) > 2000 "
         "AND b.id IN (SELECT max(id) FROM blogs GROUP BY brand_id, lower(trim(seed)))")
    params = []
    if brand_ids:
        q += " AND b.brand_id IN (%s)" % ",".join("?" * len(brand_ids))
        params += brand_ids
    q += " ORDER BY b.updated_at DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in db.conn.execute(q, params).fetchall()]


# ── FU251: replay ────────────────────────────────────────────────────────────────────────────────────
# Every check added in FU251 is DETERMINISTIC — no model, no network, no DB write. So they can be
# replayed over every article already stored, which is a far better test of a generator change than
# one fresh generation: 214 real bodies across every vertical, for nothing, in about a minute.
#
# This exists because the discipline it enforces was not being followed. FU242 → FU249 shipped on unit
# tests alone; unit tests prove a check FIRES, and nothing proved the article got better. The first
# replay found 53 of 214 published articles carrying damage from our own removal passes, and two ways
# the new price check called a correctly-cited source bad. Neither was findable by reading the code.
def _replay_rows(db, brand_ids=None, ids=None, limit=1000):
    q = ("SELECT b.id, b.title, b.seed, b.body_markdown, br.name AS brand, br.domain_url AS dom "
         "FROM blogs b LEFT JOIN brands br ON br.id = b.brand_id "
         "WHERE length(coalesce(b.body_markdown,'')) > 500")
    params = []
    if ids:
        q += " AND b.id IN (%s)" % ",".join("?" * len(ids))
        params += ids
    if brand_ids:
        q += " AND b.brand_id IN (%s)" % ",".join("?" * len(brand_ids))
        params += brand_ids
    q += " ORDER BY b.id LIMIT ?"
    params.append(limit)
    return [dict(r) for r in db.conn.execute(q, params).fetchall()]


def _blocks_from_sources(body):
    """The article's own ## Sources list back into evidence blocks. The price check reads only each
    block's URL, so this is faithful for it — and it means a replay needs no stored evidence."""
    import re as _re
    tail = body.split("## Sources", 1)[-1] if "## Sources" in body else ""
    out = []
    for ln in tail.split("\n"):
        m = _re.search(r"\[S(\d+)\]", ln)
        if not m:
            continue
        u = _re.search(r"\((https?://[^)\s]+)\)", ln) or _re.search(r"(https?://\S+)", ln)
        n = int(m.group(1))
        while len(out) < n:
            out.append({"url": "", "text": ""})
        out[n - 1] = {"url": (u.group(1) if u else "").rstrip(">),."), "text": "x" * 800}
    return out


def run_replay_rewrites(args):
    """FU252 — what the REWORDING changed about what each stored article ASSERTS. $0.

    A different question from `--replay`: that one asks whether a body is damaged, this one compares
    a body with its own rewrite. Walks both pairs the schema carries — (body_markdown,
    rewritten_body) and the FU250 (imported_body, imported_rewritten)."""
    import collections
    E = _load_eval_module()
    db = _db()
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(blogs)")}
    total = collections.Counter()
    blocking = budgeted = affected = 0
    seen = 0
    for bcol, rcol in (("body_markdown", "rewritten_body"), ("imported_body", "imported_rewritten")):
        if bcol not in cols or rcol not in cols:
            continue
        q = (f"SELECT b.id, b.title, b.{bcol} o, b.{rcol} r FROM blogs b "
             f"WHERE length(coalesce(b.{rcol},''))>500 AND length(coalesce(b.{bcol},''))>500")
        params = []
        if _ids(args.brand_ids):
            q += " AND b.brand_id IN (%s)" % ",".join("?" * len(_ids(args.brand_ids)))
            params += _ids(args.brand_ids)
        rows = [dict(r) for r in db.conn.execute(q + " ORDER BY b.id", params).fetchall()]
        print(f"\n=== {bcol} → {rcol}: {len(rows)} pair(s) ===")
        seen += len(rows)
        for x in rows:
            hits = E.rewrite_findings(x["o"] or "", x["r"] or "")
            if not hits:
                continue
            affected += 1
            for h in hits:
                total[h["check"]] += 1
                if h.get("severity") == "blocking":
                    blocking += 1
                else:
                    budgeted += 1
            if args.detail:
                print(f"  #{x['id']:<5} {(x['title'] or '')[:52]}")
                for h in hits[:6]:
                    print(f"        [{h.get('severity','?'):<9}] {h['check']:<28} {h['detail'][:74]}")
    print(f"\n=== {seen} pair(s) replayed — no model, no network, $0 ===")
    print(f"\n{affected} pair(s) carry at least one finding "
          f"({blocking} blocking, {budgeted} budgeted)\n")
    for k, v in total.most_common():
        print(f"    {v:>4}  {k}")
    return 0


def run_replay(args):
    """What the current deterministic passes WOULD do to every stored article. $0."""
    import collections
    E = _load_eval_module()
    db = _db()
    from generators.blog_gen import BlogGenerator
    gen = BlogGenerator.__new__(BlogGenerator)
    gen._claim_pages = {}
    rows = _replay_rows(db, _ids(args.brand_ids), _ids(args.ids), args.limit or 1000)
    dmg = collections.Counter()
    edi = collections.Counter()            # FU253: what the article SAYS, kept apart from damage
    n_dmg = n_edi = dedup_n = dedup_blogs = 0
    price_removed = price_capped = price_blogs = regressions = 0
    # FU254 — the two passes added this round that need no network: the table-orientation rule and
    # the repeated-claim reduction. Both change the BODY, so both are replayable and both are
    # measured against the same damage floor as every other removal.
    flip_blogs = claim_blogs = claim_removed = 0
    detail = []
    for r in rows:
        body = r["body_markdown"] or ""
        hits = E.body_damage(body)
        if hits:
            n_dmg += 1
            for h in hits:
                dmg[h["check"]] += 1
        ehits = E.editorial_findings(body, r.get("title") or "")
        if ehits:
            n_edi += 1
            for h in ehits:
                edi[h["check"]] += 1
        _, nd = BlogGenerator._dedupe_repeated_clauses(body)
        if nd:
            dedup_n += nd
            dedup_blogs += 1
        # FU254 (1) — does the table rule now read this article's axis differently?
        try:
            gen._table_punt_note = ""
            gen._article_tools = []
            _flipped = gen._resolve_table_punts(body)
            _plain = BlogGenerator.__new__(BlogGenerator)
            _plain._table_punt_note = ""
            _plain._article_tools = []
            _plain._table_is_transposed = lambda hdr, data: False
            if _flipped != _plain._resolve_table_punts(body):
                flip_blogs += 1
        except Exception as e:
            print(f"  !! #{r['id']} table: {type(e).__name__}: {e}", flush=True)
        # FU254 (2) — the same uncited comparative claim, said once instead of five times
        try:
            _cout, _cnote = gen._repeated_claim_check(body)
            if _cnote:
                claim_blogs += 1
                m2 = __import__("re").search(r"removed (\d+) restatement", _cnote)
                claim_removed += int(m2.group(1)) if m2 else 0
                if len(E.body_damage(_cout)) > len(hits):
                    regressions += 1
                    print(f"  !! #{r['id']} REGRESSION: the claim reduction raised damage",
                          flush=True)
        except Exception as e:
            print(f"  !! #{r['id']} claim: {type(e).__name__}: {e}", flush=True)
        blocks = _blocks_from_sources(body)
        note = ""
        if blocks:
            try:
                out, note = gen._price_source_check(
                    body, blocks, {"name": r["brand"] or "", "domain_url": r["dom"] or ""})
            except Exception as e:
                print(f"  !! #{r['id']}: {type(e).__name__}: {e}", flush=True)
                continue
            if note:
                price_blogs += 1
                m = __import__("re").search(r"removed (\d+) price", note)
                if m:
                    price_removed += int(m.group(1))
                else:
                    price_capped += 1
                if len(E.body_damage(out)) > len(hits):
                    regressions += 1
                    print(f"  !! #{r['id']} REGRESSION: a removal raised the damage count", flush=True)
        if args.detail and (hits or note):
            detail.append((r["id"], (r["title"] or r["seed"] or "")[:54], len(hits), note[:110]))

    print(f"\n=== replayed {len(rows)} stored article(s) — no model, no network, $0 ===")
    print(f"\nDAMAGE already in stored bodies: {n_dmg} article(s)")
    for k, v in dmg.most_common():
        print(f"    {v:>4}  {k}")
    print(f"\nFU254 table orientation: {flip_blogs} article(s) whose table rule reads a "
          f"different axis now (an option kept instead of deleted)")
    print(f"FU254 repeated claim: {claim_blogs} article(s), {claim_removed} restatement(s) removed")
    print(f"\nEDITORIAL findings — what the article says, NOT damage: {n_edi} article(s)")
    for k, v in edi.most_common():
        print(f"    {v:>4}  {k}")
    print(f"\nDE-DUPLICATOR would remove {dedup_n} repeat(s) across {dedup_blogs} article(s)")
    print(f"\nPRICE SOURCE: {price_blogs} article(s) — {price_removed} price(s) removed, "
          f"{price_capped} reported-not-touched (over the fabrication cap)")
    print(f"\nINVARIANT — removals that RAISED the damage count: {regressions}   "
          f"{'OK' if regressions == 0 else '*** BROKEN ***'}")
    if detail:
        print("\nper article:")
        for bid, t, nh, note in detail:
            print(f"  #{bid:<5} dmg={nh:<3} {t}")
            if note:
                print(f"         {note}")
    return 1 if regressions else 0


# ── run ──────────────────────────────────────────────────────────────────────────────────────────────
def _table(rows):
    cols = ["id", "brand", "words", "checked", "confirmed", "contradicted", "not_on_page",
            "unreadable", "uncited", "defects", "rubric"]
    head = " ".join(f"{c[:12]:>12}" if c != "brand" else f"{c:<18}" for c in cols)
    print(head)
    for r in rows:
        print(" ".join(f"{str(r.get(c) if r.get(c) is not None else '-')[:12]:>12}" if c != "brand"
                       else f"{str(r.get(c) or '')[:18]:<18}" for c in cols))


def run_local(args):
    E = _load_eval_module()
    db = _db()
    if args.list:
        for c in _candidates(db, _ids(args.brand_ids), args.limit):
            print(f"{c['id']:>5}  {str(c['brand'] or '')[:18]:<18} {c['status']:<10} {c['updated_at'] or '':<20} "
                  f"{c['chars']:>6}  {c['seed'][:80]}")
        return
    ids = _ids(args.ids)
    if args.latest_per_seed:
        ids += [c["id"] for c in _candidates(db, _ids(args.brand_ids), args.limit) if c["id"] not in ids]
    if not ids:
        print("nothing to score — pass --ids or --latest-per-seed")
        return
    from config import ANTHROPIC_API_KEY
    from generators.base import ClaudeClient
    from generators.blog_gen import BlogGenerator
    gen = BlogGenerator(ClaudeClient(ANTHROPIC_API_KEY), db) if not args.no_grounding else BlogGenerator(None, db)
    rub = None
    if not args.no_rubric:
        rub = ClaudeClient(ANTHROPIC_API_KEY)
        rub.model = E.EVAL_RUBRIC_MODEL
    reps, t0 = [], time.time()
    for n, bid in enumerate(ids, 1):
        blog = db.get_blog(bid)
        if not blog:
            print(f"[{n}/{len(ids)}] #{bid} not found", flush=True)
            continue
        brand = db.get_brand(blog.get("brand_id")) if blog.get("brand_id") else {}
        print(f"[{n}/{len(ids)}] #{bid} {brand.get('name', '')}: {blog.get('seed', '')[:70]}", flush=True)
        fc = None
        try:
            if args.factcheck:
                # Round 2 offline: score what the automatic fact-check WOULD produce. Nothing is saved.
                fgen = BlogGenerator(ClaudeClient(ANTHROPIC_API_KEY), db)
                nb, nm, fc = E.factcheck_body(fgen, brand or {}, blog.get("body_markdown") or "",
                                              blog.get("meta_description") or "")
                fc["cost"] = round(fgen.claude.usage_cost(), 4)
                blog = dict(blog, body_markdown=nb, meta_description=nm)
                print(f"    fact-check: {fc['applied']} applied, {fc['refused']} refused "
                      f"(of {fc['items']} item(s)) ${fc['cost']:.2f}", flush=True)
            rep = E.evaluate_blog(gen, brand or {}, blog, rubric_client=rub,
                                  grounding=not args.no_grounding, rubric=not args.no_rubric)
            if fc is not None:
                rep["factcheck"] = fc
                rep["cost"]["factcheck"] = fc["cost"]
        except Exception as e:
            rep = {"blog_id": bid, "error": str(e)[:300]}
        reps.append(rep)
        row = E.summary_row(rep)
        print("    " + json.dumps(row) + f"  ({rep.get('secs', '?')}s, ${sum((rep.get('cost') or {}).values()):.2f})",
              flush=True)
    out = {"label": args.label, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "rubric_model": getattr(rub, "model", ""), "secs": round(time.time() - t0, 1),
           "cost": round(sum(sum((r.get("cost") or {}).values()) for r in reps), 4),
           "blogs": reps}
    path = os.path.join(_eval_dir(), f"{args.label}-{time.strftime('%Y%m%d-%H%M%S')}.json")
    with open(path, "w") as f:
        json.dump(out, f, indent=1)
    print()
    _table([E.summary_row(r) for r in reps])
    print(f"\n{len(reps)} blog(s) · ${out['cost']:.2f} · {out['secs']}s → {path}")


def _ids(s):
    return [int(x) for x in str(s or "").replace(" ", "").split(",") if x.strip().isdigit()]


def _latest(label):
    files = sorted(glob.glob(os.path.join(str(HERE.parent / "evals"), f"{label}*.json"))
                   + glob.glob(os.path.join(_eval_dir(), f"{label}*.json")), key=os.path.getmtime)
    if not files:
        raise SystemExit(f"no report for '{label}' — fetch it with --remote --status {label}")
    return json.load(open(files[-1]))


def compare(a, b):
    E = _load_eval_module()
    A, B = _latest(a), _latest(b)
    ra = {r.get("blog_id"): E.summary_row(r) for r in A["blogs"]}
    rb = {r.get("blog_id"): E.summary_row(r) for r in B["blogs"]}
    keys = ["contradicted", "not_on_page", "uncited", "defects", "rubric"]
    print(f"{'id':>5}  {'brand':<18} " + " ".join(f"{k[:12]:>16}" for k in keys))
    tot = {k: [0, 0] for k in keys}
    for bid in [i for i in ra if i in rb]:
        cells = []
        for k in keys:
            x, y = ra[bid].get(k), rb[bid].get(k)
            if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                tot[k][0] += x
                tot[k][1] += y
            cells.append(f"{str(x)}→{str(y)}")
        print(f"{bid:>5}  {str(ra[bid].get('brand') or '')[:18]:<18} " + " ".join(f"{c:>16}" for c in cells))
    print("total  " + " " * 18 + " ".join(f"{str(round(v[0], 1)) + '→' + str(round(v[1], 1)):>16}"
                                          for v in tot.values()))


def main():
    if "--remote" in sys.argv[1:]:
        return run_remote(sys.argv[1:])
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ids", default="")
    ap.add_argument("--latest-per-seed", action="store_true")
    ap.add_argument("--brand-ids", default="")
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--label", default="eval")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"))
    ap.add_argument("--no-grounding", action="store_true")
    ap.add_argument("--no-rubric", action="store_true")
    ap.add_argument("--factcheck", action="store_true",
                    help="Round 2 offline: apply the automatic fact-check in memory, then score the result")
    ap.add_argument("--replay", action="store_true",
                    help="FU251: run every DETERMINISTIC check over the stored bodies and report what "
                         "they would change. No model, no network, no write — $0.")
    ap.add_argument("--detail", action="store_true", help="with --replay: one line per affected article")
    ap.add_argument("--rewrites", action="store_true",
                    help="FU252: compare every stored body with its own REWRITE and report what the "
                         "rewording changed about what the article asserts. Also $0.")
    args = ap.parse_args()
    if args.compare:
        return compare(*args.compare)
    if args.rewrites:
        return run_replay_rewrites(args)
    if args.replay:
        return run_replay(args)
    run_local(args)


if __name__ == "__main__":
    main()
