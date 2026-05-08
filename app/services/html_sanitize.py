"""
Tiny HTML sanitizer for ad-hoc email composer output.

The composer's contenteditable produces innerHTML the BDR can edit + paste
into. Word, Google Docs, etc. paste in tons of junk attributes (mso-*,
inline color: rgb(31, 31, 31), etc.) that bloat the email and sometimes
trigger spam filters. We allow a minimal tag + attribute set and drop
the rest.

Risk model: we are the sender, the BDR is trusted, but we still want a
clean output to maximize deliverability. This is NOT defending against
a hostile user-submitted HTML payload (those would warrant a more
restrictive sanitizer like bleach with a strict CSS scrubber).
"""
from __future__ import annotations
from bs4 import BeautifulSoup, NavigableString, Comment


_ALLOWED_TAGS = {
    "p", "br", "div", "span",
    "strong", "b", "em", "i", "u", "s",
    "a",
    "ul", "ol", "li",
    "blockquote",
    "h1", "h2", "h3", "h4",
    "code", "pre",
    "hr",
}

_ALLOWED_ATTRS = {
    "a": {"href", "title"},
    # No attributes anywhere else — strips classes, ids, inline styles,
    # mso-* artifacts, etc.
}


def sanitize_email_html(raw_html: str) -> str:
    """Strip everything that isn't in the allow-list. Returns clean HTML.
    On parse failure, returns text-escaped fallback so we never bubble
    bad markup to Resend."""
    if not raw_html or not raw_html.strip():
        return ""
    try:
        soup = BeautifulSoup(raw_html, "lxml")
    except Exception:
        return _text_escape(raw_html)

    # Strip HTML comments first — Word emits a lot of <!--[if mso]-->
    for c in soup.find_all(string=lambda t: isinstance(t, Comment)):
        c.extract()

    for tag in list(soup.find_all(True)):
        if tag.name not in _ALLOWED_TAGS:
            tag.unwrap()  # keep text content, drop the tag itself
            continue
        # Drop disallowed attrs
        allowed_attrs = _ALLOWED_ATTRS.get(tag.name, set())
        for attr in list(tag.attrs):
            if attr not in allowed_attrs:
                del tag[attr]
        # Sanitize <a href> — only allow http/https/mailto/tel
        if tag.name == "a":
            href = (tag.get("href") or "").strip()
            if href and not href.lower().startswith(("http://", "https://", "mailto:", "tel:")):
                del tag["href"]
            # Force target=_blank rel=noopener for safety
            tag["target"] = "_blank"
            tag["rel"] = "noopener noreferrer"

    # bs4 always wraps in <html><body> for full docs; lxml may or may not.
    body = soup.body
    if body:
        return body.decode_contents().strip()
    # If no body tag (fragment input), return everything
    return str(soup).strip()


def _text_escape(s: str) -> str:
    """Last-resort fallback: escape and wrap in <p> tags."""
    return (
        "<p>"
        + s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n\n", "</p><p>").replace("\n", "<br>")
        + "</p>"
    )
