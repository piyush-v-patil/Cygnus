"""H-1B sponsor lookup against job-monitor's h1b.json (§6.5).

`companies[name]` has three states and they must stay distinguishable:
  key missing -> never looked up (unknown)          -> qualifies
  None        -> looked up, no filings found
  {...}       -> filings record with `filed` count
"""
from __future__ import annotations

import re

_MISSING = object()
_SUFFIX = re.compile(r"[,.]?\s*\b(inc|llc|l\.l\.c|ltd|corp|corporation|co|company|plc|gmbh)\b\.?$", re.I)


def _norm(name: str) -> str:
    n = (name or "").strip().lower()
    prev = None
    while prev != n:
        prev = n
        n = _SUFFIX.sub("", n).strip(" ,.")
    return re.sub(r"\s+", " ", n)


class H1BIndex:
    def __init__(self, data: dict | None):
        companies = (data or {}).get("companies") or {}
        self.exact = companies
        self.loose: dict[str, object] = {}
        for k, v in companies.items():
            self.loose.setdefault(_norm(k), v)

    def record(self, company: str):
        """The raw record: _MISSING, None or the filings dict."""
        if company in self.exact:
            return self.exact[company]
        return self.loose.get(_norm(company), _MISSING)

    def state(self, company: str) -> str:
        r = self.record(company)
        if r is _MISSING:
            return "unknown"
        if r is None:
            return "no_record"
        return "filer" if int(r.get("filed") or 0) > 0 else "zero_filed"

    def qualifies(self, company: str, mode: str) -> bool:
        if mode == "off":
            return True
        st = self.state(company)
        if mode == "known_filers_only":
            return st == "filer"
        # skip_no_record
        return st in ("unknown", "filer")
