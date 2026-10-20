"""Per-ATS knowledge: where the description lives and how its form is laid out."""
from __future__ import annotations

from dataclasses import dataclass, field

import requests

from .detect import AtsRef


@dataclass
class JobDetails:
    closed: bool = False
    description_text: str = ""
    questions: list = field(default_factory=list)   # ATS-declared questions, when published
    title: str | None = None
    company: str | None = None
    location: str | None = None


class FetchError(Exception):
    """Transient: try again next run."""


class AtsClient:
    name = "base"
    # CSS for one question block, its label, and how required is marked. Used by
    # browser/fields.py as hints; the generic enumerator works without them.
    form_hints: dict = {}

    def __init__(self, http: requests.Session | None = None, timeout: float = 30):
        self.http = http or requests.Session()
        self.timeout = timeout

    def _get(self, url: str, **kw) -> requests.Response:
        try:
            return self.http.get(url, timeout=self.timeout, headers={"User-Agent": "autoapply"}, **kw)
        except requests.RequestException as e:
            raise FetchError(str(e)) from e

    def fetch(self, ref: AtsRef) -> JobDetails:
        raise NotImplementedError
