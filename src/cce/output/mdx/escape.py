"""MDX escaping for crawled and model-written text (B13, see SECURITY.md).

MDX reads ``{...}`` as a JavaScript expression, ``<`` as the start of JSX
or HTML, and a line starting with ``import`` / ``export`` as an ES module
statement. None of that may come from a crawled page or a model's reply, so
emit writes those characters as character references, which MDX and
CommonMark render as the literal character. Both escapers are the identity
on text without them.
"""

from __future__ import annotations

import re

_BODY_TABLE = str.maketrans({"{": "&#123;", "}": "&#125;", "<": "&lt;"})
# The keyword, not a longer word ("exporting"): import"x", export{a}, import(
_ESM_RE = re.compile(r"^([ \t]*)(import|export)(?![\w$])", re.MULTILINE)
_INLINE_PUNCT_RE = re.compile(r"([\\`*\[\]])")
_CR_RE = re.compile(r"\r\n?")


def escape_mdx_body(markdown: str) -> str:
    """Escape a markdown body for MDX. Idempotent.

    ``{``, ``}`` and ``<`` become character references everywhere, code
    included (writer output is prose; skipping code spans would open a gap
    wherever this module and the MDX parser disagree on where code starts).
    Inside code spans and fences references are not decoded, so such code
    shows ``&#123;``, ``&#125;`` or ``&lt;`` verbatim. A line that starts
    with ``import`` or ``export`` gets its first letter as a character
    reference. ``>`` (blockquotes), ``&``, ``[^N]`` footnotes and backslashes
    are left alone.
    """
    # A lone CR ends a line for MDX but not for the ESM regex's "^", so line
    # endings are normalised first (review of B13: "\r\rexport ..." was live).
    markdown = _CR_RE.sub("\n", markdown)
    # ESM first, on the raw text, so "export{" is seen before "{" is replaced.
    neutralised = _ESM_RE.sub(
        lambda m: f"{m.group(1)}&#{ord(m.group(2)[0])};{m.group(2)[1:]}", markdown
    )
    return neutralised.translate(_BODY_TABLE)


def escape_mdx_inline(text: str) -> str:
    """Escape untrusted text (a crawled title) for use inside one markdown
    line: whitespace collapsed, so it can neither end the line nor start an
    ESM block; ``\\``, backticks, ``*``, ``[`` and ``]`` backslash-escaped,
    so it can't close the surrounding emphasis or make a link or code span;
    ``>`` as ``&gt;``; then ``escape_mdx_body``. Apply once, to raw text.
    """
    text = _INLINE_PUNCT_RE.sub(r"\\\1", " ".join(text.split()))
    return escape_mdx_body(text.replace(">", "&gt;"))
