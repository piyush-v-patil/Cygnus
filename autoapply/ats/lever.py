"""Lever postings API. VERIFIED 2026-10-20:
GET api.lever.co/v0/postings/{company}/{id}?mode=json returns descriptionPlain,
lists[], additionalPlain, categories.location; a closed posting answers 404
{"ok":false,"error":"Document not found"}. Lever does not publish the form's
questions; they are read from the page.
"""
from __future__ import annotations

from ..normalize import html_to_text
from .base import AtsClient, FetchError, JobDetails
from .detect import AtsRef


class LeverClient(AtsClient):
    name = "lever"
    form_hints = {
        "container": "li.application-question, .application-additional, .application-question",
        "submit": "button#btn-submit, button[type=submit]:has-text('Submit')",
        "resume_input": "input[type=file]#resume-upload-input, input[type=file][name=resume]",
        "cover_input": "",
        "error": ".error-message, .application-error",
    }

    def fetch(self, ref: AtsRef) -> JobDetails:
        url = f"https://api.lever.co/v0/postings/{ref.board_token}/{ref.job_id}"
        r = self._get(url, params={"mode": "json"})
        if r.status_code == 404:
            return JobDetails(closed=True)
        if r.status_code != 200:
            raise FetchError(f"lever HTTP {r.status_code}")
        j = r.json()
        parts = [j.get("descriptionPlain") or html_to_text(j.get("description", ""))]
        for lst in j.get("lists") or []:
            parts.append(f"{lst.get('text', '')}\n{html_to_text(lst.get('content', ''))}")
        parts.append(j.get("additionalPlain") or "")
        cats = j.get("categories") or {}
        return JobDetails(description_text="\n\n".join(p for p in parts if p).strip(),
                          title=j.get("text"), location=cats.get("location"))
