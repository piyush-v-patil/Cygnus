"""Write application outcomes back to the user's job-monitor fork (§12.2).

Mirrors job-monitor's docs/app.js persist(): sha from the directory listing, raw
read, modify only the target ids' own fields, PUT with the sha, re-read and
retry on 409. Only short codes and dates are written - the repo is public.
"""
from __future__ import annotations

import json
import logging
from datetime import date

from .github import GitHub, GitHubError
from .ledger import Ledger
from .sources.jobmonitor import JOBS_PATH

log = logging.getLogger(__name__)
ALLOWED_FIELDS = {"status", "applied_on", "applied_via", "skip_reason", "skipped_by"}
FLUSH_EVERY = 25
COMMIT_MESSAGE = "autoapply: update statuses"


def applied_change(day: date | None = None) -> dict:
    return {"status": "applied", "applied_on": (day or date.today()).isoformat(), "applied_via": "autoapply"}


def skip_change(reason: str) -> dict:
    return {"status": "skip", "skip_reason": reason, "skipped_by": "autoapply"}


def unskip_change(reason: str) -> dict:
    """Undo a skip this tool wrote - and only that: a status the user set stays."""
    return {"status": "new", "skip_reason": None, "skipped_by": None,
            "_only_if": {"status": "skip", "skipped_by": "autoapply", "skip_reason": reason}}


def apply_changes(doc: dict, changes: dict[str, dict]) -> list[str]:
    """Set the given fields on the target ids only. Returns ids that were changed.

    A None value removes the field; `_only_if` applies the change only when the
    job currently has those exact values.
    """
    jobs = doc.get("jobs") or {}
    found = []
    for job_id, change in changes.items():
        job = jobs.get(job_id)
        if job is None:
            continue
        cond = change.get("_only_if") or {}
        if any(job.get(k) != v for k, v in cond.items()):
            continue
        for k, v in change.items():
            if k not in ALLOWED_FIELDS:
                continue
            if v is None:
                job.pop(k, None)
            else:
                job[k] = v
        found.append(job_id)
    return found


class Writeback:
    def __init__(self, ledger: Ledger, source_name: str, repo: str, branch: str, github: GitHub):
        self.ledger = ledger
        self.source_name = source_name
        self.repo = repo
        self.branch = branch
        self.gh = github

    def queue(self, posting_key: str, change: dict):
        if not self.ledger.queue_writeback(posting_key, change, self.source_name):
            return
        if len(self.ledger.pending_writebacks(self.source_name)) >= FLUSH_EVERY:
            self.flush()

    def flush(self) -> int:
        """One commit for everything pending. Returns how many entries were written."""
        pending = self.ledger.pending_writebacks(self.source_name)
        if not pending:
            return 0
        # last change per job wins (e.g. skip then a later manual submit)
        changes: dict[str, dict] = {}
        for p in pending:
            changes[p["source_ref"]] = p["change"]
        ids = [p["id"] for p in pending]
        for attempt in range(3):
            try:
                sha = self.gh.blob_sha(self.repo, JOBS_PATH, self.branch)
                doc = json.loads(self.gh.read_raw(self.repo, JOBS_PATH, self.branch))
                apply_changes(doc, changes)
                # same layout as job-monitor monitor/state.py, so the commit diff is just our fields
                body = json.dumps(doc, indent=1, ensure_ascii=False, sort_keys=True).encode("utf-8")
                status = self.gh.put_file(self.repo, JOBS_PATH, self.branch, body, sha, COMMIT_MESSAGE)
            except (GitHubError, ValueError) as e:
                log.warning("write-back failed (will retry next time): %s", e)
                self.ledger.mark_writebacks(ids, error=str(e)[:300])
                return 0
            if status == 409:
                log.info("write-back conflict, re-reading (attempt %d)", attempt + 1)
                continue
            self.ledger.mark_writebacks(ids)
            return len(changes)
        self.ledger.mark_writebacks(ids, error="409 conflict after 3 attempts")
        return 0
