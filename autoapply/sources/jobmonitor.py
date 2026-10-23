"""job-monitor tracker adapter (§6.1.1, §6.1.2).

Reads docs/data/jobs.json and docs/data/h1b.json from the user's fork (or a
public repo, read-only), and config/companies.yaml to resolve `gh_jid` links to
their Greenhouse board token.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import yaml

from ..config import SourceCfg
from ..github import GitHub, GitHubError
from .base import Posting, SourceError

JOBS_PATH = "docs/data/jobs.json"
H1B_PATH = "docs/data/h1b.json"
COMPANIES_PATH = "config/companies.yaml"


class JobMonitorSource:
    def __init__(self, cfg: SourceCfg, token: str | None = None, github: GitHub | None = None):
        self.cfg = cfg
        self.name = cfg.label
        self.writeback = cfg.type == "jobmonitor" and cfg.writeback
        # jobmonitor_public never uses the token: it is read-only by definition
        self.gh = github or GitHub(token if cfg.type == "jobmonitor" else None)
        self._jobs: dict | None = None
        self._h1b: dict | None = None
        self._tokens: dict[str, str] | None = None

    # ------------------------------------------------------------- reading --
    def _read(self, path: str, local: str | None) -> bytes:
        if local:
            return Path(local).expanduser().read_bytes()
        if not self.cfg.repo:
            raise SourceError(f"{self.name}: no repo configured for {path}")
        try:
            return self.gh.read_raw(self.cfg.repo, path, self.cfg.branch)
        except GitHubError as e:
            raise SourceError(f"{self.name}: {e}") from e

    def data(self) -> dict:
        if self._jobs is None:
            raw = self._read(JOBS_PATH, self.cfg.path)
            try:
                self._jobs = json.loads(raw)
            except ValueError as e:
                raise SourceError(f"{self.name}: jobs.json is not valid JSON") from e
            if not isinstance(self._jobs.get("jobs"), dict):
                raise SourceError(f"{self.name}: jobs.json has no `jobs` object")
        return self._jobs

    def h1b(self) -> dict | None:
        if self._h1b is None:
            local = self.cfg.h1b_path
            if self.cfg.path and not local:
                cand = Path(self.cfg.path).expanduser().with_name("h1b.json")
                local = str(cand) if cand.exists() else None
            if not local and not self.cfg.repo:
                return None
            try:
                self._h1b = json.loads(self._read(H1B_PATH, local))
            except (SourceError, ValueError):
                self._h1b = {}
        return self._h1b or None

    def gh_tokens(self) -> dict[str, str]:
        if self._tokens is None:
            self._tokens = {}
            local = self.cfg.companies_path
            if self.cfg.path and not local:
                # a local checkout: <repo>/docs/data/jobs.json -> <repo>/config/companies.yaml
                parents = Path(self.cfg.path).expanduser().resolve().parents
                cand = parents[2] / COMPANIES_PATH if len(parents) > 2 else None
                local = str(cand) if cand is not None and cand.exists() else None
            try:
                raw = self._read(COMPANIES_PATH, local) if (local or self.cfg.repo) else b""
                reg = yaml.safe_load(raw) or {}
            except (SourceError, yaml.YAMLError):
                reg = {}
            for group in reg.values() if isinstance(reg, dict) else []:
                for c in group or []:
                    if isinstance(c, dict) and c.get("fetcher") == "greenhouse" and c.get("token"):
                        self._tokens[str(c["name"]).strip().lower()] = str(c["token"])
        return self._tokens

    def postings(self) -> Iterator[Posting]:
        for job_id, j in self.data()["jobs"].items():
            url = j.get("employer_url") or j.get("url") or ""
            if not url:
                continue
            yield Posting(
                company=j.get("company", ""), title=j.get("title", ""), apply_url=url,
                location=j.get("location", ""), tier=j.get("tier"), role=j.get("role"),
                posted_at=j.get("posted_at"), first_seen=j.get("first_seen"),
                source_name=self.name, source_ref=job_id,
                monitor_status=j.get("status", "new"), closed_at=j.get("closed_at"),
                extra={"skipped_by": j.get("skipped_by"), "applied_via": j.get("applied_via")},
            )

    def check(self) -> str:
        n = len(self.data()["jobs"])
        return f"{self.name}: {n} jobs (updated {self.data().get('updated', '?')})"
