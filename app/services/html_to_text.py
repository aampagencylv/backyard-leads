"""HTML → plain text conversion for the `text` alternative on outbound
email sends.

Why we care: HTML-only emails are a known soft spam signal at Gmail
and Outlook. Including a well-formatted plain-text alternative is one
of the lowest-effort deliverability lifts available, and it also
improves the reply UX (the prospect's quoted-text on Reply is the
plain version, not a styled mess).

Strategy: walk the parsed tree with BeautifulSoup (already a dep),
preserve link URLs in `text (url)` form so the prospect can still
follow them in clients that downgrade to text, normalize whitespace
at the end.
"""
from __future__ import annotations
import re
from typing import Optional

from bs4 import BeautifulSoup, NavigableString, Tag


_BLOCK_TAGS = {
    "p", "div", "section", "article", "header", "footer", "aside",
    "blockquote", "pre", "hr", "table", "tr", "ul", "ol",
}
_LINE_BREAK_TAGS = {"br"}
_LIST_ITEM_TAGS = {"li"}
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_DROP_TAGS = {"style", "script", "head", "title", "meta", "link", "noscript"}


def _walk(node, out: list[str]) -> None:
    if isinstance(node, NavigableString):
        text = str(node)
        # Strip the awful "whitespace between tags" runs but preserve
        # intentional spacing inside paragraphs.
        if text and not text.isspace():
            out.append(re.sub(r"\s+", " ", text))
        elif text and " " in text and not out[-1].endswith(" ") if out else False:
            out.append(" ")
        return

    if not isinstance(node, Tag):
        return

    name = (node.name or "").lower()

    if name in _DROP_TAGS:
        return

    if name == "a":
        # Render anchor text + its URL so the link survives in text mode.
        href = (node.get("href") or "").strip()
        link_text_parts: list[str] = []
        for c in node.children:
            _walk(c, link_text_parts)
        link_text = " ".join(link_text_parts).strip()
        if href and link_text and href != link_text:
            out.append(f"{link_text} ({href})")
        elif href:
            out.append(href)
        elif link_text:
            out.append(link_text)
        return

    if name in _LINE_BREAK_TAGS:
        out.append("\n")
        return

    if name == "hr":
        out.append("\n\n---\n\n")
        return

    if name == "img":
        alt = (node.get("alt") or "").strip()
        if alt:
            out.append(f"[{alt}]")
        return

    if name in _HEADING_TAGS:
        out.append("\n\n")
        for c in node.children:
            _walk(c, out)
        out.append("\n\n")
        return

    if name in _LIST_ITEM_TAGS:
        out.append("\n- ")
        for c in node.children:
            _walk(c, out)
        return

    if name in _BLOCK_TAGS:
        # Surround block with double-newlines so paragraphs separate cleanly.
        out.append("\n\n")
        for c in node.children:
            _walk(c, out)
        out.append("\n\n")
        return

    # Default: just descend
    for c in node.children:
        _walk(c, out)


def html_to_plain_text(html: Optional[str]) -> str:
    """Convert HTML to a readable plain-text body suitable for the
    `text` alternative on a multipart email. Idempotent: passing the
    output back in yields the same string."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    # Pull <body> if present, otherwise treat the whole soup as body.
    root = soup.body or soup
    chunks: list[str] = []
    for c in root.children:
        _walk(c, chunks)
    text = "".join(chunks)
    # Collapse 3+ newlines down to 2, strip trailing whitespace per line,
    # then strip the whole string.
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return text.strip()
