"""MDX escaping of crawled and model-written text (B13, audit 2.2).

The body escaper must leave no raw ``{``, ``}`` or ``<`` and no ESM line,
keep markdown (``>`` quotes, ``[^N]`` footnotes) intact, be idempotent, and
be the identity on clean prose (the golden emit test pins the latter for
whole pages).
"""

from __future__ import annotations

import json
import re

import pytest

from cce.models.content import ContentLineage, ContentScores, ContentUnit
from cce.output.mdx.escape import escape_mdx_body, escape_mdx_inline
from cce.output.mdx.formatter import format_mdx_page
from cce.output.mdx.thnklabs import format_thnklabs_page
from tests.conftest import make_evidence
from tests.test_evidence.test_untrusted_prompts import HOSTILE_TITLE

pytestmark = pytest.mark.unit

_ESM_LINE_RE = re.compile(r"^\s*(import|export)\b", re.MULTILINE)

HOSTILE_BODY = (
    "## Sleep\n\n"
    "Adults need sleep {fetch('//evil')} [ev:ev_1].\n\n"
    "<Foo/> and <script>alert(1)</script> and <!-- x -->\n\n"
    'import fs from "fs"\n'
    "export const x = 1\n"
    "  export{a}\n\n"
    "> A quoted line [ev:ev_1].\n\n"
    "```js\nconst y = {z: 1} < 2;\n```\n"
)


def _no_mdx_syntax(text: str) -> None:
    assert not {"{", "}", "<"} & set(text)
    assert not _ESM_LINE_RE.search(text)


def test_body_escaper_removes_expressions_tags_and_esm():
    out = escape_mdx_body(HOSTILE_BODY)

    _no_mdx_syntax(out)
    assert "&#123;fetch('//evil')&#125;" in out
    assert "&lt;script>alert(1)&lt;/script>" in out
    assert '&#105;mport fs from "fs"' in out
    assert "&#101;xport const x = 1" in out
    assert "  &#101;xport&#123;a&#125;" in out
    assert "> A quoted line [ev:ev_1]." in out  # blockquote kept
    assert "const y = &#123;z: 1&#125; &lt; 2;" in out  # code escaped too


@pytest.mark.parametrize(
    "text",
    [
        "## How Much Sleep Adults Need\n\nMost adults need seven hours [^1].",
        "> Quote & more, 5 > 3, a | b, (c), snake_case, \\ backslash.",
        "Exporting goods and important imports [^2].",
    ],
)
def test_body_escaper_is_the_identity_on_clean_prose(text):
    assert escape_mdx_body(text) == text


def test_escapers_compose_idempotently():
    once = escape_mdx_body(HOSTILE_BODY)
    assert escape_mdx_body(once) == once
    inline = escape_mdx_inline(HOSTILE_TITLE)
    assert escape_mdx_body(inline) == inline


def test_inline_escaper_keeps_a_hostile_title_on_one_inert_line():
    out = escape_mdx_inline(HOSTILE_TITLE)

    assert "\n" not in out
    _no_mdx_syntax(out)
    assert r"\[win\](https://evil.example)" in out
    assert r"\`x\`" in out
    assert "&lt;script&gt;" in out


def _unit(content: str) -> ContentUnit:
    return ContentUnit(
        id="cu_1",
        path="explore",
        content=content,
        citations=[],
        evidence_map=[],
        scores=ContentScores(confidence=1.0, coverage=1.0, source_diversity=0.5),
        lineage=ContentLineage(policy_id="p", run_id="r", engine_version="0.4.0"),
    )


def _split(mdx: str) -> tuple[dict, str]:
    head, body = mdx.split("\n\n", 1)
    return json.loads(head[head.index("{") : head.rindex("}") + 1]), body


def test_client_resources_bullet_escapes_a_hostile_title():
    """Audit 2.2's named sink: the rebuilt resources list."""
    body = "## Sleep\n\nA claim [ev:ev_1].\n\n## Curated Resources\n\n- x\n"
    lookup = {
        "ev_1": make_evidence(id="ev_1", url="https://h.example", title=HOSTILE_TITLE)
    }
    meta, rendered = _split(format_thnklabs_page(_unit(body), lookup, topic_slug="t"))

    _no_mdx_syntax(rendered)
    [bullet] = [ln for ln in rendered.splitlines() if ln.startswith("- ")]
    assert bullet.startswith(r"- **Sleep &#123;globalThis.pwned=1&#125;")
    assert bullet.endswith("** [^1]")
    # The metadata keeps the raw title (JSON-escaped, not MDX-escaped).
    assert meta["citations"][0]["title"] == " ".join(HOSTILE_TITLE.split())


@pytest.mark.parametrize("fmt", ["generic", "client"])
def test_page_body_is_escaped_and_metadata_derived_from_raw_text(fmt):
    body = "## Sleep {x} <b>now</b>\n\n" + HOSTILE_BODY
    lookup = {"ev_1": make_evidence(id="ev_1")}
    if fmt == "generic":
        mdx = format_mdx_page(_unit(body), lookup, "job_1", curated_at="t")
    else:
        mdx = format_thnklabs_page(_unit(body), lookup, topic_slug="t")
    meta, rendered = _split(mdx)

    _no_mdx_syntax(rendered)
    assert meta["title"] == "Sleep {x} <b>now</b>"
