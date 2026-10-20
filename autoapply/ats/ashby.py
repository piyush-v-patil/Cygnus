"""Ashby public job-board API. VERIFIED 2026-10-20:
GET api.ashbyhq.com/posting-api/job-board/{org} returns {"jobs": [...]} with
id, title, location, descriptionHtml/descriptionPlain, isListed. There is no
per-job endpoint, so the board is cached once per org per run; a job missing
from its board is closed.
"""
from __future__ import annotations

from ..normalize import html_to_text
from .base import AtsClient, FetchError, JobDetails
from .detect import AtsRef


class AshbyClient(AtsClient):
    name = "ashby"
    form_hints = {
        "container": ".ashby-application-form-field-entry, fieldset",
        "submit": "button.ashby-application-form-submit-button, button:has-text('Submit Application')",
        "resume_input": "input[type=file]#_systemfield_resume",
        "cover_input": "",
        "error": ".ashby-application-form-field-entry [class*=error], [role=alert]",
    }

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._boards: dict[str, dict[str, dict]] = {}

    def _board(self, org: str) -> dict[str, dict]:
        if org not in self._boards:
            r = self._get(f"https://api.ashbyhq.com/posting-api/job-board/{org}")
            if r.status_code == 404:
                self._boards[org] = {}
            elif r.status_code != 200:
                raise FetchError(f"ashby HTTP {r.status_code}")
            else:
                self._boards[org] = {j["id"].lower(): j for j in r.json().get("jobs") or []}
        return self._boards[org]

    def fetch(self, ref: AtsRef) -> JobDetails:
        j = self._board(ref.board_token or "").get((ref.job_id or "").lower())
        if j is None:
            return JobDetails(closed=True)
        text = j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml", ""))
        return JobDetails(description_text=text, title=j.get("title"), location=j.get("location"))
