#!/usr/bin/env python3
"""FU274 — the three defect probes that found the agency-article damage, as one command.

`blog_eval.py --rewrites` runs the differential detectors that already existed. These three are the
ones that caught what those missed, and they are the A/B yardstick for a model swap: run them before
the swap and after, on the same articles.

    PYTHONPATH=. DB_PATH=<db> python3 tools/rewrite_probe.py [--ids 251,252,253]

Each probe compares a stored `rewritten_body` against its own `body_markdown`. Nothing here gates or
mutates anything — it reports.
"""
import argparse
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Polarity openers only — the word IS the answer an engine lifts. Nothing topical.
DIRECT = ("Yes", "No", "Not reliably", "Not necessarily", "Not always", "Rarely",
          "Usually", "Often", "Only", "Generally", "It depends")
# Attribution verbs that cast doubt. Vertical-neutral by construction. Matched only with a SUBJECT
# in front, because "which claims are valid" is the noun and carries no doubt at all — and the
# subject may be a PRONOUN: the reported defect was "It claims to have scaled 650+ brands", in a
# paragraph whose topic is the publisher. A brand-name-only window missed it.
DOUBT = r"(?:claims?|claimed|purports?|alleges?|boasts?|touts?|brags?|insists?)\b"
SUBJ = r"(?:It|They|The\s+(?:company|agency|firm|team)|{b})\s+(?:also\s+|further\s+)?"
# LLM register markers. Counted only as a DIFFERENCE — a word the input never used.
MARKERS = ("comprehensive", "crucial", "essential", "notably", "in the realm of", "aforementioned",
           "boasts", "hinges on", "it is worth noting", "delve", "tapestry", "landscape of")


def _faq_answers(body):
    """(question, answer) for each FAQ entry — an H3 question followed by its first paragraph."""
    tail = re.split(r"(?im)^#{1,3}\s*FAQ\s*$", body or "", maxsplit=1)
    if len(tail) < 2:
        return []
    out, q = [], None
    for line in re.split(r"(?im)^#{1,4}\s*sources\s*$", tail[1])[0].split("\n"):
        st = line.strip()
        if re.match(r"^#{2,4}\s+", st):
            q = re.sub(r"^#+\s*", "", st)
        elif st and q and not st.startswith(("#", "|", "-")):
            out.append((q, st))
            q = None
    return out


def _direct(ans):
    # Strip markdown emphasis first: the operator bolds a direct opener precisely BECAUSE it is
    # load-bearing, and "**Yes.**" must not read as an opener that was lost. The first version of
    # this probe reported 0 of 0 direct openers on a bolded article for exactly that reason.
    a = re.sub(r"[*_`]", "", ans or "").strip()
    return any(a.split(".")[0].strip() == d or a.startswith(d + ",") for d in DIRECT)


def probe(orig, rew, brand=""):
    o_faq, r_faq = _faq_answers(orig), _faq_answers(rew)
    by_q = {q: a for q, a in r_faq}
    lost = [q for q, a in o_faq if _direct(a) and not _direct(by_q.get(q, ""))]

    def _doubt_on_brand(text):
        """Doubt verbs whose subject is the publisher, counted per PARAGRAPH that names it — so a
        pronoun subject inside the brand's own profile counts, and the same verb inside a competitor's
        profile does not."""
        if not brand:
            return 0
        rx = re.compile(SUBJ.format(b=re.escape(brand)) + DOUBT)
        n = 0
        for para in re.split(r"\n\s*\n", text or ""):
            if brand.lower() not in para.lower():
                continue
            n += len(rx.findall(para))
        return n

    added = {m: (rew or "").lower().count(m) - (orig or "").lower().count(m) for m in MARKERS}
    return {
        "faq_direct": (sum(1 for _, a in o_faq if _direct(a)),
                       sum(1 for q, a in o_faq if _direct(a) and _direct(by_q.get(q, "")))),
        "faq_lost": lost,
        "doubt": (_doubt_on_brand(orig), _doubt_on_brand(rew)),
        "markers": {m: n for m, n in added.items() if n > 0 and (orig or "").lower().count(m) == 0},
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ids", default="")
    ap.add_argument("--limit", type=int, default=40)
    a = ap.parse_args()
    from config import DB_PATH
    c = sqlite3.connect(f"file:{os.environ.get('DB_PATH') or DB_PATH}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    q = ("SELECT b.id, b.title, b.body_markdown o, b.rewritten_body r, "
         "coalesce(br.name,'') brand FROM blogs b LEFT JOIN brands br ON br.id = b.brand_id "
         "WHERE coalesce(b.rewritten_body,'') <> ''")
    ids = [x.strip() for x in a.ids.split(",") if x.strip().isdigit()]
    if ids:
        q += f" AND b.id IN ({','.join(ids)})"
    q += f" ORDER BY b.id DESC LIMIT {int(a.limit)}"
    rows = list(c.execute(q))
    tot_o = tot_k = tot_d = 0
    for r in rows:
        p = probe(r["o"], r["r"], r["brand"])
        had, kept = p["faq_direct"]
        tot_o += had
        tot_k += kept
        tot_d += max(0, p["doubt"][1] - p["doubt"][0])
        print(f"\n{r['id']:>4}  {(r['title'] or '')[:58]}")
        print(f"      direct FAQ openers  {had} -> {kept}"
              + (f"   LOST: {'; '.join(x[:44] for x in p['faq_lost'][:2])}" if p["faq_lost"] else ""))
        print(f"      doubt-verbs on {r['brand'] or '(no brand)'}: "
              f"{p['doubt'][0]} -> {p['doubt'][1]}")
        if p["markers"]:
            print("      register markers the original never used: "
                  + ", ".join(f"{m} x{n}" for m, n in sorted(p["markers"].items(),
                                                             key=lambda kv: -kv[1])[:6]))
    print(f"\n=== {len(rows)} article(s): direct FAQ openers {tot_o} -> {tot_k}; "
          f"doubt-verbs added to the publisher {tot_d}")


if __name__ == "__main__":
    main()
