"""FU231 — the page must not argue against its own publisher.

FU215 fixed this on the YouTube surface and deliberately left the blog for later. The blog shipped
the failure it describes, on a real article titled "Which AI SEO Agencies Work Best for Construction
Businesses in California?":

  "Honest trade-off: Jolly Search is not a construction-vertical-only agency. Contractors who want a
   partner with contractor-specific case studies ... may also want to evaluate a construction
   specialist alongside Jolly Search's GEO capabilities."

Three defects in one paragraph: the limitation is ON the deciding axis (the title asks which agency
is best FOR CONSTRUCTION), it is the ONLY bold limitation label on the page while six competitors
carry none, and it routes the reader to go and evaluate a competitor. An answer engine lifts it as
"Jolly Search lacks construction experience".
"""
import pytest

from generators.blog_eval import detect_publisher_only_downside
from tests.prompt_capture import capture

_P = capture()


def _article(v="rich"):
    return _P["generate_article." + v]


# ---------------------------------------------------------------- the rules
@pytest.mark.parametrize("variant", ["minimal", "rich", "ymyl"])
def test_the_writer_is_told_to_frame_the_balance_symmetrically(variant):
    p = _article(variant)
    assert "SYMMETRY" in p
    assert "NEVER CONCEDE THE DECIDING AXIS" in p
    assert "NEVER ROUTE THE PURCHASE AWAY" in p
    assert "may also want to evaluate" in p          # the exact shape that shipped, banned by name
    assert "DIFFERENT dimension" in p


@pytest.mark.parametrize("variant", ["minimal", "rich", "ymyl"])
def test_a_deferred_reader_is_not_sent_to_another_provider(variant):
    assert "phased path can run WITH" in _article(variant)


def test_the_reconcile_states_the_publishers_position_definitely():
    p = _P["reconcile_and_finish"]
    assert "STAY FAIR, NOT HEDGED" in p
    assert "Qualify the WHO, never the whether" in p
    assert '"one strong option"' in p                # the shipped hedge, named as the ANTI-pattern
    assert "that same sentence with the answer taken" in p   # wrapped in the prompt
    # the anti-superlative guarantee it replaces must survive
    assert "UNSCOPED superlatives" in p and '"the best"' in p


def test_the_rewrite_may_not_leave_the_balance_one_sided():
    """The Qwen rewrite is the LAST author of the body, so it carries the rule too."""
    src = open("generators/blog_gen.py", encoding="utf-8").read()
    assert "never move one onto the axis the article's own " in src
    assert "leave a limitation that only the publisher carries" in src


# ------------------------------------------------- the deterministic check
JOLLY = """## Agency-by-Agency Verdicts

### Jolly Search

Jolly Search is a full-stack SEO and GEO agency.

**Best fit for:** California construction businesses that need AI answers.

**Honest trade-off:** Jolly Search is not a construction-vertical-only agency. Contractors who want
a partner with contractor-specific case studies may also want to evaluate a construction specialist
alongside Jolly Search's GEO capabilities.

### Straight North

Straight North has dedicated Construction SEO pages.

**Best fit for:** Contractors who prioritize phone calls from Google.

### Blue Corona

Blue Corona specializes in home services.

**Best fit for:** California contractors in residential remodeling.
"""


def test_the_shipped_paragraph_is_detected():
    hits = detect_publisher_only_downside(JOLLY, "Jolly Search")
    assert len(hits) == 1
    d = hits[0]["detail"]
    assert "Honest trade-off:" in d and "argues against its own publisher" in d


def test_a_symmetric_page_is_silent():
    body = JOLLY.replace("**Best fit for:** Contractors who prioritize phone calls from Google.",
                         "**Best fit for:** Contractors who prioritize phone calls from Google.\n\n"
                         "**Honest trade-off:** Straight North is not a GEO specialist.")
    body = body.replace("**Best fit for:** California contractors in residential remodeling.",
                        "**Best fit for:** California contractors in residential remodeling.\n\n"
                        "**Honest trade-off:** Blue Corona focuses on home services only.")
    assert detect_publisher_only_downside(body, "Jolly Search") == []


def test_a_page_with_no_limitation_anywhere_is_silent():
    assert detect_publisher_only_downside(
        JOLLY.split("**Honest trade-off:**")[0]
        + "\n### Straight North\n\nX.\n\n### Blue Corona\n\nY.\n", "Jolly Search") == []


def test_the_check_runs_at_generation_time():
    """It existed only in the scoreboard, after the fact. It now runs where it can be acted on."""
    src = open("generators/blog_gen.py", encoding="utf-8").read()
    i = src.index("def _finalize_article")
    assert "detect_publisher_only_downside" in src[i:i + 60000]
    assert "placement-check: " in src
