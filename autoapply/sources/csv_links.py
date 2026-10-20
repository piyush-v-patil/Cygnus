"""CSV / plain-text link list adapter (§6.1.3).

Columns: url (required), company, title, location (optional). A file with one
URL per line and no header is accepted too. Missing fields are filled in from
the ATS during enrichment.
"""
from __future__ import annotations

import csv
import hashlib
from pathlib import Path
from typing import Iterator

from ..config import SourceCfg
from .base import Posting, SourceError


class CsvSource:
    writeback = False

    def __init__(self, cfg: SourceCfg):
        self.cfg = cfg
        self.name = cfg.label
        self.path = Path(cfg.path or "").expanduser()

    def _rows(self) -> list[dict]:
        if not self.path.exists():
            raise SourceError(f"{self.name}: file not found: {self.path}")
        text = self.path.read_text(encoding="utf-8-sig")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if not lines:
            return []
        first = lines[0].lower()
        if "url" in [c.strip() for c in first.split(",")]:
            rows = list(csv.DictReader(lines))
            return [{(k or "").strip().lower(): (v or "").strip() for k, v in r.items()} for r in rows]
        return [{"url": ln.strip().split(",")[0].strip()} for ln in lines if ln.strip().startswith("http")]

    def postings(self) -> Iterator[Posting]:
        for r in self._rows():
            url = r.get("url", "")
            if not url.startswith("http"):
                continue
            yield Posting(company=r.get("company", ""), title=r.get("title", ""), apply_url=url,
                          location=r.get("location", ""), source_name=self.name,
                          source_ref=hashlib.sha1(url.encode()).hexdigest()[:12])

    def h1b(self):
        return None

    def gh_tokens(self) -> dict[str, str]:
        return {}

    def check(self) -> str:
        return f"{self.name}: {len(self._rows())} links"
