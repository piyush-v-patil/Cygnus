"""Text normalisation shared by the answer bank, ATS detection and slugs."""
from __future__ import annotations

import re
import unicodedata

_REQUIRED_MARK = re.compile(r"(\(\s*required\s*\)|\*|✱)+\s*$", re.I)
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")


def normalize_question(text: str) -> str:
    """§8.6: lowercase, NFKC, strip trailing required markers and punctuation."""
    t = unicodedata.normalize("NFKC", text or "").strip()
    prev = None
    while prev != t:
        prev = t
        t = _REQUIRED_MARK.sub("", t).strip()
    t = t.lower()
    t = _PUNCT.sub(" ", t)
    return _WS.sub(" ", t).strip()


def slug(text: str, max_len: int = 40) -> str:
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    t = re.sub(r"[^a-zA-Z0-9]+", "-", t).strip("-").lower()
    return (t[:max_len].rstrip("-")) or "x"


def html_to_text(html: str) -> str:
    """Strip HTML (Greenhouse double-escapes its `content`) to readable text."""
    import html as _html
    t = _html.unescape(html or "")
    if "&lt;" in t or "&amp;" in t:
        t = _html.unescape(t)
    t = re.sub(r"(?is)<(script|style).*?</\1>", " ", t)
    t = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>|</div>", "\n", t)
    t = re.sub(r"(?i)<li[^>]*>", "\n- ", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = _html.unescape(t)
    t = re.sub(r"[ \t ]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n\n", t)
    return t.strip()
