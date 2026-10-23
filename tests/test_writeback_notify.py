"""job-monitor write-back (mock GitHub incl. 409 retry) and Discord grouping (§12.2, §14)."""
import copy
import json

from autoapply.github import GitHubError
from autoapply.ledger import Ledger
from autoapply.notify import Discord, Notifier, chunk_lines, group_needs_input
from autoapply.writeback import Writeback, applied_change, apply_changes, skip_change

DOC = {"version": 1, "updated": "x", "sources": {}, "jobs": {
    "a:1": {"company": "A", "status": "new", "title": "T", "first_seen": "2026-10-01"},
    "b:2": {"company": "B", "status": "new", "title": "U", "first_seen": "2026-10-02"},
}}


class FakeGitHub:
    def __init__(self, conflicts=0):
        self.doc = copy.deepcopy(DOC)
        self.conflicts = conflicts
        self.puts = []
        self.sha = "sha1"

    def blob_sha(self, repo, path, branch):
        return self.sha

    def read_raw(self, repo, path, branch):
        return json.dumps(self.doc).encode()

    def put_file(self, repo, path, branch, content, sha, message):
        self.puts.append((sha, message))
        if self.conflicts:
            self.conflicts -= 1
            # someone else committed in between: their change must survive our retry
            self.doc["jobs"]["b:2"]["status"] = "interview"
            self.sha = "sha2"
            return 409
        self.doc = json.loads(content)
        return 200


def _ledger(tmp_path):
    led = Ledger(tmp_path / "l.sqlite")
    led.upsert_discovered("greenhouse:a:1", {"source_name": "jm", "source_ref": "a:1"})
    led.upsert_discovered("greenhouse:b:2", {"source_name": "jm", "source_ref": "b:2"})
    led.upsert_discovered("url:x", {"source_name": "csv", "source_ref": "zz"})
    return led


def test_apply_changes_touches_only_targets_and_allowed_fields():
    doc = copy.deepcopy(DOC)
    apply_changes(doc, {"a:1": {**applied_change(), "notes": "secret fit reason"}, "missing": skip_change("x")})
    assert doc["jobs"]["a:1"]["status"] == "applied" and doc["jobs"]["a:1"]["applied_via"] == "autoapply"
    assert "notes" not in doc["jobs"]["a:1"]
    assert doc["jobs"]["b:2"] == DOC["jobs"]["b:2"]
    assert doc["jobs"]["a:1"]["title"] == "T"


def test_flush_one_commit_and_409_retry(tmp_path):
    led = _ledger(tmp_path)
    gh = FakeGitHub(conflicts=1)
    wb = Writeback(led, "jm", "me/jm", "main", gh)
    wb.queue("greenhouse:a:1", skip_change("low_fit"))
    wb.queue("url:x", skip_change("low_fit"))            # not from this source: ignored
    assert wb.flush() == 1
    assert [p[1] for p in gh.puts] == ["autoapply: update statuses"] * 2
    assert gh.puts[1][0] == "sha2"                        # re-read after the conflict
    j = gh.doc["jobs"]
    assert j["a:1"] == {**DOC["jobs"]["a:1"], "status": "skip", "skip_reason": "low_fit", "skipped_by": "autoapply"}
    assert j["b:2"]["status"] == "interview"              # the other writer's change survived
    assert wb.flush() == 0                                # nothing pending any more


def test_failed_write_is_retried_next_time(tmp_path):
    led = _ledger(tmp_path)

    class Down(FakeGitHub):
        def blob_sha(self, *a):
            raise GitHubError("HTTP 503", 503)

    wb = Writeback(led, "jm", "me/jm", "main", Down())
    wb.queue("greenhouse:a:1", applied_change())
    assert wb.flush() == 0
    assert len(led.pending_writebacks("jm")) == 1
    wb.gh = FakeGitHub()
    assert wb.flush() == 1 and wb.gh.doc["jobs"]["a:1"]["status"] == "applied"



def test_a_failed_flush_stops_flushing_on_every_skip(tmp_path, monkeypatch):
    import autoapply.writeback as wbm
    monkeypatch.setattr(wbm, "FLUSH_EVERY", 1)
    led = Ledger(tmp_path / "l.sqlite")
    calls = []

    class Down(FakeGitHub):
        def blob_sha(self, *a):
            calls.append(1)
            raise GitHubError("HTTP 403", 403)

    wb = Writeback(led, "jm", "me/jm", "main", Down())
    for i in range(20):
        led.upsert_discovered(f"greenhouse:c:{i}", {"source_name": "jm", "source_ref": f"c:{i}"})
        wb.queue(f"greenhouse:c:{i}", skip_change("low_fit"))
    assert len(calls) == 1                                   # not one jobs.json download per skip
    assert wb.flush() == 0 and len(calls) == 2              # the end of the run still tries


class Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body or {}

    def json(self):
        return self._body


def test_discord_retries_429_and_chunks():
    calls, sleeps = [], []
    answers = [Resp(429, {"retry_after": 0.5}), Resp(204)]

    def post(url, json):
        calls.append(json["content"])
        return answers.pop(0) if answers else Resp(204)

    d = Discord("https://discord/webhook", post=post, sleep=sleeps.append)
    assert d.send("hello")
    assert sleeps == [0.5] and len(calls) == 2
    long = "\n".join(f"line {i} " + "x" * 90 for i in range(60))
    parts = chunk_lines(long.splitlines())
    assert len(parts) > 1 and all(len(p) <= 2000 for p in parts)


def test_needs_input_immediate_then_grouped():
    sent = []
    d = Discord("u", post=lambda url, json: (sent.append(json["content"]), Resp(204))[1], sleep=lambda s: None)
    n = Notifier(d, "immediate", group_threshold=2)
    for i in range(14):
        n.needs_input_item(f"Co{i % 3}", f"Role {i}", ["Q?"])
    assert len(sent) == 2                                  # first two pinged immediately
    n.flush_needs_input()
    grouped = sent[2:]
    assert len(grouped) == 2                               # 12 left, at most 10 per message
    assert all("Needs input (" in m for m in grouped)


def test_group_needs_input_keeps_companies_together():
    items = [{"company": c, "title": "t", "questions": []} for c in "BAAB"]
    groups = group_needs_input(items)
    assert [i["company"] for i in groups[0]] == ["A", "A", "B", "B"]


def test_background_discord_never_blocks_the_caller():
    import threading
    import time
    gate, sent = threading.Event(), []

    def post(url, json):
        gate.wait(5)                                       # a slow webhook
        sent.append(json["content"])
        return Resp(204)

    d = Discord("u", post=post, sleep=lambda s: None, background=True)
    t0 = time.monotonic()
    assert d.send("one") and d.send("two")
    assert time.monotonic() - t0 < 0.5 and not sent
    assert d.drain(0.2) is False
    gate.set()
    assert d.drain(5) and sent == ["one", "two"]


def test_disabled_discord_sends_nothing():
    d = Discord(None)
    assert d.send("x") is False
