"""Minimal GitHub contents-API client used for reading job-monitor and writing back.

jobs.json is several MB, so it is always read with the raw media type (the
base64 `content` field stops at 1 MB) and the blob sha comes from the parent
directory listing - the same approach as job-monitor's docs/app.js persist().
"""
from __future__ import annotations

import base64
import time

import requests

API = "https://api.github.com"


class GitHubError(Exception):
    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


class GitHub:
    def __init__(self, token: str | None = None, session: requests.Session | None = None,
                 timeout: float = 60):
        self.token = token
        self.http = session or requests.Session()
        self.timeout = timeout

    def _headers(self, accept: str = "application/vnd.github+json") -> dict:
        h = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "autoapply"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _req(self, method: str, url: str, accept: str | None = None, **kw) -> requests.Response:
        for attempt in range(4):
            try:
                r = self.http.request(method, url, headers=self._headers(accept or "application/vnd.github+json"),
                                      timeout=self.timeout, **kw)
            except requests.RequestException as e:
                if attempt == 3:
                    raise GitHubError(f"GitHub unreachable: {e}") from e
                time.sleep(2 ** attempt)
                continue
            if r.status_code >= 500 and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            return r
        raise GitHubError("GitHub unreachable")

    def read_raw(self, repo: str, path: str, branch: str) -> bytes:
        if self.token:
            r = self._req("GET", f"{API}/repos/{repo}/contents/{path}",
                          accept="application/vnd.github.raw", params={"ref": branch, "t": int(time.time())})
        else:
            r = self._req("GET", f"https://raw.githubusercontent.com/{repo}/{branch}/{path}")
        if r.status_code != 200:
            raise GitHubError(f"read {repo}/{path}@{branch}: HTTP {r.status_code}", r.status_code)
        return r.content

    def blob_sha(self, repo: str, path: str, branch: str) -> str:
        d, _, name = path.rpartition("/")
        r = self._req("GET", f"{API}/repos/{repo}/contents/{d}", params={"ref": branch, "t": int(time.time())})
        if r.status_code != 200:
            raise GitHubError(f"list {repo}/{d}: HTTP {r.status_code}", r.status_code)
        for f in r.json():
            if f.get("name") == name:
                return f["sha"]
        raise GitHubError(f"{name} not found on {branch}")

    def put_file(self, repo: str, path: str, branch: str, content: bytes, sha: str, message: str) -> int:
        body = {"message": message, "content": base64.b64encode(content).decode(),
                "sha": sha, "branch": branch}
        r = self._req("PUT", f"{API}/repos/{repo}/contents/{path}", json=body)
        if r.status_code not in (200, 201, 409):
            raise GitHubError(f"write {repo}/{path}: HTTP {r.status_code} {r.text[:200]}", r.status_code)
        return r.status_code

    def repo_permissions(self, repo: str) -> dict:
        r = self._req("GET", f"{API}/repos/{repo}")
        if r.status_code != 200:
            raise GitHubError(f"repo {repo}: HTTP {r.status_code}", r.status_code)
        return r.json().get("permissions") or {}
