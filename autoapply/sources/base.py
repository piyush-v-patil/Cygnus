"""Common posting record every job source yields (§6.1)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Iterator, Protocol


@dataclass
class Posting:
    company: str
    title: str
    apply_url: str
    location: str = ""
    tier: str | None = None
    role: str | None = None
    posted_at: str | None = None
    first_seen: str | None = None
    source_name: str = ""
    source_ref: str | None = None
    # job-monitor only: status / closed_at gate the prefilter (§6.4.2)
    monitor_status: str | None = None
    closed_at: str | None = None
    extra: dict = field(default_factory=dict)

    def as_fields(self) -> dict:
        d = asdict(self)
        d.pop("extra")
        return d


class SourceError(Exception):
    pass


class Source(Protocol):
    name: str
    writeback: bool

    def postings(self) -> Iterator[Posting]: ...

    def h1b(self) -> dict | None: ...

    def gh_tokens(self) -> dict[str, str]: ...

    def check(self) -> str: ...
