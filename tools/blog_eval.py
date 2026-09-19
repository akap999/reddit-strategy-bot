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
    args = ap.parse_args()
    if args.compare:
        return compare(*args.compare)
    run_local(args)


if __name__ == "__main__":
    main()
