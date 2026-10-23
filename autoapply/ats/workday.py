"""Workday job data through the public "CXS" JSON that its career sites use.

VERIFIED 2026-10-23 against live tenants (nvidia.wd5):
GET https://<host>/wday/cxs/<tenant>/<site>/job/<slug> returns jobPostingInfo
{title, jobDescription (HTML), location, canApply, posted, ...} without a login;
a removed posting answers 403 {"errorCode":"S22","message":"permission denied"}
(404/410 are treated the same). The form itself is a multi-page wizard behind a
per-company account; see browser/workday.py.
"""
from __future__ import annotations

from ..normalize import html_to_text
from .base import AtsClient, FetchError, JobDetails
from .detect import AtsRef, workday_parts


class WorkdayClient(AtsClient):
    name = "workday"
    form_hints = {
        "container": "[data-automation-id^='formField-']",
        "submit": "button[data-automation-id='bottom-navigation-next-button'], "
                  "button[data-automation-id='pageFooterNextButton']",
        "resume_input": "input[type=file][data-automation-id='file-upload-input-ref']",
        "cover_input": "",
        "error": "[data-automation-id='errorMessage'], [data-automation-id='errorBanner']",
        "workday": True,
    }

    def fetch(self, ref: AtsRef) -> JobDetails:
        tenant, host, site, slug = workday_parts(ref)
        url = f"https://{host}/wday/cxs/{tenant}/{site}/job/{slug}"
        try:
            r = self.http.get(url, timeout=self.timeout,
                              headers={"User-Agent": "autoapply", "Accept": "application/json"})
        except Exception as e:  # noqa: BLE001 - requests errors
            raise FetchError(str(e)) from e
        if r.status_code in (404, 410):
            return JobDetails(closed=True)
        if r.status_code == 403:
            # S22 is Workday's "this posting is gone"; any other 403 (bot protection, rate
            # limits) is a failed fetch to retry, never a permanent "closed"
            try:
                code = (r.json() or {}).get("errorCode")
            except ValueError:
                code = None
            if code == "S22":
                return JobDetails(closed=True)
            raise FetchError(f"workday HTTP 403{f' ({code})' if code else ''}")
        if r.status_code != 200:
            raise FetchError(f"workday HTTP {r.status_code}")
        try:
            info = (r.json() or {}).get("jobPostingInfo") or {}
        except ValueError as e:
            raise FetchError("workday answered something that is not JSON") from e
        if not info or info.get("canApply") is False or info.get("posted") is False:
            return JobDetails(closed=True)
        return JobDetails(description_text=html_to_text(info.get("jobDescription") or "").strip(),
                          title=info.get("title"), location=info.get("location"))
