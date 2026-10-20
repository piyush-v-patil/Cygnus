"""Greenhouse job board API. VERIFIED 2026-10-20:
GET boards-api.greenhouse.io/v1/boards/{token}/jobs/{id}?questions=true returns
`content` (HTML, entity-escaped), `questions`, `location.name`, `company_name`;
a removed posting answers 404.
"""
from __future__ import annotations

from ..normalize import html_to_text
from .base import AtsClient, FetchError, JobDetails
from .detect import AtsRef


class GreenhouseClient(AtsClient):
    name = "greenhouse"
    form_hints = {
        # job-boards.greenhouse.io (2025+ React board): react-select comboboxes
        # are <input role=combobox class=select__input> labelled by label[for=id]
        "container": ".field-wrapper, .input-wrapper, .select__container, fieldset, .file-upload, .text-input-wrapper",
        "submit": "button[type=submit], button:has-text('Submit application')",
        "resume_input": "input[type=file]#resume, input[type=file][id*=resume i]",
        "cover_input": "input[type=file]#cover_letter, input[type=file][id*=cover i]",
        "error": ".helper-text--error, .field-error, [role=alert]",
    }

    def fetch(self, ref: AtsRef) -> JobDetails:
        url = f"https://boards-api.greenhouse.io/v1/boards/{ref.board_token}/jobs/{ref.job_id}"
        r = self._get(url, params={"questions": "true"})
        if r.status_code == 404:
            return JobDetails(closed=True)
        if r.status_code != 200:
            raise FetchError(f"greenhouse HTTP {r.status_code}")
        j = r.json()
        questions = list(j.get("questions") or [])
        for c in j.get("compliance") or []:
            questions.extend(c.get("questions") or [])
        return JobDetails(
            description_text=html_to_text(j.get("content", "")),
            questions=questions,
            title=j.get("title"),
            company=j.get("company_name"),
            location=(j.get("location") or {}).get("name"),
        )
