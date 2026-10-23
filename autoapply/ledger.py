"""SQLite ledger: every posting the tool has touched, and its state machine (§11).

`posting_key` is the primary key, which is what makes "never apply twice"
(§6.3) a database guarantee rather than a convention.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1

# ---------------------------------------------------------- state machine ----
DISCOVERED = "discovered"
FILTERED_OUT = "filtered_out"
UNSUPPORTED = "unsupported"
ENRICHED = "enriched"
CLOSED = "closed"
SKIPPED = "skipped"
SCORED = "scored"
FILLING = "filling"
READY = "ready"
NEEDS_INPUT = "needs_input"
STUCK = "stuck"
ERROR = "error"
SUBMITTED = "submitted"
SUBMITTED_MANUAL = "submitted_manual"
WOULD_SUBMIT = "would_submit"
REJECTED_BY_USER = "rejected_by_user"
HELD = "held"            # scored below the cutoff and parked (fit.low_fit: hold); re-decided every run

LANES = (READY, NEEDS_INPUT, STUCK)
TERMINAL = {FILTERED_OUT, UNSUPPORTED, CLOSED, SKIPPED, SUBMITTED, SUBMITTED_MANUAL,
            WOULD_SUBMIT, REJECTED_BY_USER}
IN_FLIGHT = {ENRICHED, SCORED, HELD, FILLING, READY, NEEDS_INPUT, STUCK, ERROR}
APPLIED = {SUBMITTED, SUBMITTED_MANUAL}

# Rule-stage outcomes cost nothing to recompute, so they are re-checked every
# run: a profile edit (new tier, sponsor_filter: off) brings postings back.
RECHECKABLE = {(FILTERED_OUT, None), (SKIPPED, "no_h1b_record"), (SKIPPED, "us_person_company")}

SKIP_REASONS = ("low_fit", "us_person_required", "us_person_company", "no_sponsorship", "no_h1b_record",
                "yoe", "blocker_other")
MAX_ERROR_RETRIES = 3

TRANSITIONS: dict[str, set[str]] = {
    DISCOVERED: {FILTERED_OUT, UNSUPPORTED, ENRICHED, CLOSED, SKIPPED, ERROR},
    FILTERED_OUT: {DISCOVERED},
    SKIPPED: {DISCOVERED, HELD},    # DISCOVERED: RECHECKABLE skips; HELD: low_fit released (hold policy)
    ENRICHED: {CLOSED, SKIPPED, SCORED, HELD, ERROR},
    SCORED: {FILLING, SKIPPED, HELD, CLOSED, ERROR},
    HELD: {SCORED, SKIPPED, CLOSED, ERROR},
    FILLING: {READY, NEEDS_INPUT, STUCK, ERROR, CLOSED, SCORED},   # SCORED: requeued (budget)
    READY: {SUBMITTED, SUBMITTED_MANUAL, WOULD_SUBMIT, REJECTED_BY_USER, CLOSED, STUCK, NEEDS_INPUT},
    NEEDS_INPUT: {READY, SUBMITTED, SUBMITTED_MANUAL, WOULD_SUBMIT, REJECTED_BY_USER, CLOSED, STUCK},
    STUCK: {SUBMITTED, SUBMITTED_MANUAL, WOULD_SUBMIT, REJECTED_BY_USER, CLOSED, READY, NEEDS_INPUT},
    ERROR: {ENRICHED, SCORED, HELD, FILLING, READY, NEEDS_INPUT, STUCK, CLOSED, SKIPPED},
    UNSUPPORTED: set(), CLOSED: set(), SUBMITTED: set(), SUBMITTED_MANUAL: set(),
    WOULD_SUBMIT: set(), REJECTED_BY_USER: set(),
}


class IllegalTransition(Exception):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS postings (
  posting_key TEXT PRIMARY KEY,
  source_name TEXT, source_ref TEXT, company TEXT, title TEXT, location TEXT,
  tier TEXT, role TEXT, ats TEXT, board_token TEXT, ats_job_id TEXT, apply_url TEXT,
  posted_at TEXT, first_seen TEXT, state TEXT NOT NULL, reason TEXT,
  error_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS postings_state ON postings(state);
CREATE TABLE IF NOT EXISTS posting_refs (
  source_name TEXT, source_ref TEXT, posting_key TEXT,
  PRIMARY KEY (source_name, source_ref)
);
CREATE TABLE IF NOT EXISTS details (
  posting_key TEXT PRIMARY KEY, description_text TEXT, questions_json TEXT, fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS scores (
  posting_key TEXT, score INTEGER, role_family TEXT, reasons_json TEXT, blockers_json TEXT,
  yoe_required INTEGER, model TEXT, prompt_version TEXT, resume_hash TEXT, created_at TEXT,
  PRIMARY KEY (posting_key, prompt_version, resume_hash)
);
CREATE TABLE IF NOT EXISTS applications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  posting_key TEXT UNIQUE, lane TEXT, resume_file TEXT, fill_plan_json TEXT,
  missing_json TEXT, stuck_reason TEXT, screenshot_path TEXT, submission_dir TEXT,
  submitted_at TEXT, writeback_done INTEGER NOT NULL DEFAULT 0,
  created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS llm_usage (
  ts TEXT, purpose TEXT, model TEXT, input_tokens INTEGER, output_tokens INTEGER,
  cache_read_tokens INTEGER, cache_write_tokens INTEGER, cost_usd REAL, posting_key TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, ended_at TEXT, kind TEXT,
  counts_json TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS events (ts TEXT, posting_key TEXT, event TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS writeback_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_name TEXT, source_ref TEXT, posting_key TEXT, change_json TEXT,
  created_at TEXT, done_at TEXT, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT
);
"""


@dataclass
class PostingRow:
    posting_key: str
    state: str
    reason: str | None
    data: dict[str, Any]

    def __getitem__(self, k):
        return self.data[k]

    def get(self, k, default=None):
        return self.data.get(k, default)


class Ledger:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self.db = sqlite3.connect(self.path, timeout=30, isolation_level=None,
                                  check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._depth = 0
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.execute("INSERT OR IGNORE INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION),))

    def close(self):
        self.db.close()

    @contextmanager
    def tx(self):
        """A write transaction; nested use joins the outer one."""
        if self._depth:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        self.db.execute("BEGIN IMMEDIATE")
        self._depth = 1
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        finally:
            self._depth = 0

    # ------------------------------------------------------------ postings --
    def get(self, key: str) -> PostingRow | None:
        r = self.db.execute("SELECT * FROM postings WHERE posting_key=?", (key,)).fetchone()
        return PostingRow(r["posting_key"], r["state"], r["reason"], dict(r)) if r else None

    def key_for_ref(self, source_name: str, source_ref: str) -> str | None:
        r = self.db.execute("SELECT posting_key FROM posting_refs WHERE source_name=? AND source_ref=?",
                            (source_name, source_ref)).fetchone()
        return r[0] if r else None

    def refs_for(self, key: str) -> list[tuple[str, str]]:
        return [(r[0], r[1]) for r in self.db.execute(
            "SELECT source_name, source_ref FROM posting_refs WHERE posting_key=?", (key,))]

    def upsert_discovered(self, key: str, fields: dict[str, Any]) -> bool:
        """Insert a newly seen posting. Returns True if it is new to the ledger.

        An existing row keeps its state; only empty descriptive fields are
        filled in, so a second source seeing the same posting never resets it.
        """
        ts = now_iso()
        cols = ("source_name", "source_ref", "company", "title", "location", "tier", "role",
                "ats", "board_token", "ats_job_id", "apply_url", "posted_at", "first_seen")
        with self.tx():
            if fields.get("source_ref"):
                # a ref recorded under a "url:" key (site not supported then) moves to the
                # posting's real key once its site is supported (Workday, added later)
                self.db.execute("INSERT INTO posting_refs VALUES (?,?,?) "
                                "ON CONFLICT(source_name, source_ref) DO UPDATE SET posting_key=excluded.posting_key "
                                "WHERE posting_refs.posting_key LIKE 'url:%' AND excluded.posting_key NOT LIKE 'url:%'",
                                (fields["source_name"], fields["source_ref"], key))
            existing = self.get(key)
            if existing is None:
                self.db.execute(
                    f"INSERT INTO postings (posting_key,{','.join(cols)},state,created_at,updated_at)"
                    f" VALUES (?,{','.join('?' * len(cols))},?,?,?)",
                    (key, *[fields.get(c) for c in cols], DISCOVERED, ts, ts))
                self._event(key, "discovered", fields.get("source_name"))
                return True
            fill = {c: fields[c] for c in cols if fields.get(c) and not existing.get(c)}
            if fill:
                self.db.execute(
                    f"UPDATE postings SET {', '.join(f'{c}=?' for c in fill)}, updated_at=? WHERE posting_key=?",
                    (*fill.values(), ts, key))
            return False

    def update_fields(self, key: str, **fields):
        if not fields:
            return
        self.db.execute(
            f"UPDATE postings SET {', '.join(f'{c}=?' for c in fields)}, updated_at=? WHERE posting_key=?",
            (*fields.values(), now_iso(), key))

    def set_state(self, key: str, new: str, reason: str | None = None, detail: str | None = None):
        row = self.get(key)
        if row is None:
            raise KeyError(key)
        if new != row.state:
            if new not in TRANSITIONS.get(row.state, set()):
                raise IllegalTransition(f"{key}: {row.state} -> {new}")
            released = new == HELD and row.reason == "low_fit"
            if row.state == SKIPPED and (SKIPPED, row.reason) not in RECHECKABLE and not released:
                raise IllegalTransition(f"{key}: skipped({row.reason}) is final")
        extra = ""
        args: list[Any] = [new, reason, now_iso()]
        if new == ERROR:
            extra = ", error_count = error_count + 1"
        self.db.execute(f"UPDATE postings SET state=?, reason=?, updated_at=?{extra} WHERE posting_key=?",
                        (*args, key))
        self._event(key, f"state:{new}", reason if detail is None else f"{reason or ''} {detail}".strip())

    def postings_in(self, states: Iterable[str]) -> list[PostingRow]:
        states = list(states)
        q = (f"SELECT * FROM postings WHERE state IN ({','.join('?' * len(states))}) "
             "ORDER BY COALESCE(posted_at, first_seen, created_at) DESC, posting_key")
        return [PostingRow(r["posting_key"], r["state"], r["reason"], dict(r))
                for r in self.db.execute(q, states)]

    def counts(self) -> dict[str, int]:
        return {r[0]: r[1] for r in self.db.execute("SELECT state, COUNT(*) FROM postings GROUP BY state")}

    def reason_counts(self) -> dict[str, int]:
        return {f"{r[0]}:{r[1]}": r[2] for r in self.db.execute(
            "SELECT state, reason, COUNT(*) FROM postings WHERE reason IS NOT NULL "
            "GROUP BY state, reason ORDER BY 3 DESC")}

    # ------------------------------------------------------------- details --
    def save_details(self, key: str, description: str, questions: Any):
        self.db.execute("INSERT OR REPLACE INTO details VALUES (?,?,?,?)",
                        (key, description, json.dumps(questions), now_iso()))

    def details(self, key: str) -> dict | None:
        r = self.db.execute("SELECT * FROM details WHERE posting_key=?", (key,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["questions"] = json.loads(d.pop("questions_json") or "null")
        return d

    # -------------------------------------------------------------- scores --
    def save_score(self, key: str, s: dict, model: str, prompt_version: str, resume_hash: str):
        self.db.execute("INSERT OR REPLACE INTO scores VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (key, s["score"], s.get("role_family"), json.dumps(s.get("reasons", [])),
                         json.dumps(s.get("blockers", [])), s.get("yoe_required"), model,
                         prompt_version, resume_hash, now_iso()))

    def score(self, key: str, prompt_version: str | None = None, resume_hash: str | None = None) -> dict | None:
        q, args = "SELECT * FROM scores WHERE posting_key=?", [key]
        if prompt_version:
            q += " AND prompt_version=?"
            args.append(prompt_version)
        if resume_hash:
            q += " AND resume_hash=?"
            args.append(resume_hash)
        r = self.db.execute(q + " ORDER BY created_at DESC LIMIT 1", args).fetchone()
        if not r:
            return None
        d = dict(r)
        d["reasons"] = json.loads(d.pop("reasons_json") or "[]")
        d["blockers"] = json.loads(d.pop("blockers_json") or "[]")
        return d

    # -------------------------------------------------------- applications --
    def upsert_application(self, key: str, **fields) -> int:
        for k in ("fill_plan", "missing"):
            if k in fields:
                fields[f"{k}_json"] = json.dumps(fields.pop(k))
        ts = now_iso()
        row = self.db.execute("SELECT id FROM applications WHERE posting_key=?", (key,)).fetchone()
        if row is None:
            fields.setdefault("created_at", ts)
            fields["updated_at"] = ts
            cols = ["posting_key", *fields]
            cur = self.db.execute(
                f"INSERT INTO applications ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                (key, *fields.values()))
            return cur.lastrowid
        fields["updated_at"] = ts
        self.db.execute(f"UPDATE applications SET {', '.join(f'{c}=?' for c in fields)} WHERE id=?",
                        (*fields.values(), row[0]))
        return row[0]

    def application(self, key: str | None = None, app_id: int | None = None) -> dict | None:
        if app_id is not None:
            r = self.db.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
        else:
            r = self.db.execute("SELECT * FROM applications WHERE posting_key=?", (key,)).fetchone()
        return _app(r)

    def applications_in_lane(self, lane: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT a.* FROM applications a JOIN postings p USING(posting_key) "
            "WHERE p.state=? ORDER BY COALESCE(p.posted_at, p.first_seen) DESC, a.id DESC", (lane,))
        return [_app(r) for r in rows]

    def submitted_applications(self, search: str = "") -> list[dict]:
        rows = self.db.execute(
            "SELECT a.* FROM applications a JOIN postings p USING(posting_key) "
            "WHERE p.state IN (?,?,?) AND (?='' OR p.company LIKE ?) ORDER BY a.submitted_at DESC",
            (SUBMITTED, SUBMITTED_MANUAL, WOULD_SUBMIT, search, f"%{search}%"))
        return [_app(r) for r in rows]

    # ------------------------------------------------------------------ llm --
    def record_usage(self, purpose: str, model: str, usage: dict, cost: float, key: str | None):
        self.db.execute("INSERT INTO llm_usage VALUES (?,?,?,?,?,?,?,?,?)",
                        (now_iso(), purpose, model, usage.get("input_tokens", 0),
                         usage.get("output_tokens", 0), usage.get("cache_read_tokens", 0),
                         usage.get("cache_write_tokens", 0), cost, key))

    def month_spend(self, month: str | None = None) -> float:
        month = month or datetime.now(timezone.utc).strftime("%Y-%m")
        r = self.db.execute("SELECT COALESCE(SUM(cost_usd),0) FROM llm_usage WHERE substr(ts,1,7)=?",
                            (month,)).fetchone()
        return float(r[0])

    # ----------------------------------------------------------------- runs --
    def start_run(self, kind: str) -> int:
        return self.db.execute("INSERT INTO runs (started_at, kind) VALUES (?,?)",
                               (now_iso(), kind)).lastrowid

    def end_run(self, run_id: int, counts: dict, error: str | None = None):
        self.db.execute("UPDATE runs SET ended_at=?, counts_json=?, error=? WHERE id=?",
                        (now_iso(), json.dumps(counts), error, run_id))

    def last_run(self, kind: str = "run") -> dict | None:
        r = self.db.execute("SELECT * FROM runs WHERE kind=? ORDER BY id DESC LIMIT 1", (kind,)).fetchone()
        return dict(r) if r else None

    def requeue(self, lanes: Iterable[str]) -> list[str]:
        """Send unsubmitted applications back to `scored` so the next run fills them afresh.

        Their fill plans are dropped; scores, descriptions and the answer bank stay.
        `would_submit` (approved during a test run, never sent) can be requeued too,
        so those jobs are applied to for real.
        """
        lanes = [l for l in lanes if l in LANES or l == WOULD_SUBMIT]
        if not lanes:
            return []
        keys = [r.posting_key for r in self.postings_in(lanes)]
        with self.tx():
            for k in keys:
                self.db.execute("UPDATE postings SET state=?, reason=NULL, error_count=0, updated_at=? "
                                "WHERE posting_key=?", (SCORED, now_iso(), k))
                self.db.execute("DELETE FROM applications WHERE posting_key=?", (k,))
                self._event(k, "requeued", None)
        return keys

    # ------------------------------------------------------------ writeback --
    def queue_writeback(self, key: str, change: dict, only_source: str) -> int:
        n = 0
        for source_name, ref in self.refs_for(key):
            if source_name != only_source:
                continue
            n += 1
            self.db.execute(
                "INSERT INTO writeback_queue (source_name, source_ref, posting_key, change_json, created_at)"
                " VALUES (?,?,?,?,?)", (source_name, ref, key, json.dumps(change), now_iso()))
        return n

    def pending_writebacks(self, source_name: str) -> list[dict]:
        rows = self.db.execute("SELECT * FROM writeback_queue WHERE done_at IS NULL AND source_name=? "
                               "ORDER BY id", (source_name,))
        return [dict(r, change=json.loads(r["change_json"])) for r in rows]

    def mark_writebacks(self, ids: list[int], error: str | None = None):
        if not ids:
            return
        marks = ",".join("?" * len(ids))
        if error is None:
            self.db.execute(f"UPDATE writeback_queue SET done_at=? WHERE id IN ({marks})", (now_iso(), *ids))
            self.db.execute(
                f"UPDATE applications SET writeback_done=1 WHERE posting_key IN "
                f"(SELECT posting_key FROM writeback_queue WHERE id IN ({marks}))", ids)
        else:
            self.db.execute(f"UPDATE writeback_queue SET attempts=attempts+1, last_error=? "
                            f"WHERE id IN ({marks})", (error, *ids))

    # --------------------------------------------------------------- events --
    def _event(self, key: str | None, event: str, detail: str | None = None):
        self.db.execute("INSERT INTO events VALUES (?,?,?,?)", (now_iso(), key, event, detail))

    def event(self, key: str | None, event: str, detail: str | None = None):
        self._event(key, event, detail)

    def events(self, key: str) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM events WHERE posting_key=? ORDER BY ts", (key,))]


def _app(r) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    d["fill_plan"] = json.loads(d.pop("fill_plan_json") or "null")
    d["missing"] = json.loads(d.pop("missing_json") or "[]")
    return d
