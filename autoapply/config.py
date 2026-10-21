"""Profile folder: layout, profile.yaml schema, secrets and the answer bank.

Everything personal lives in the profile folder (default ~/.autoapply, or
--profile-dir / AUTOAPPLY_HOME). The code never holds personal data (spec §2.1).
"""
from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .normalize import normalize_question

ENV_HOME = "AUTOAPPLY_HOME"
SECRET_KEYS = ("ANTHROPIC_API_KEY", "GITHUB_TOKEN", "DISCORD_WEBHOOK_URL")


class ConfigError(Exception):
    """The profile folder is missing or invalid. Message is user-facing."""


# ---------------------------------------------------------------- layout ----
class Paths:
    """Every file the tool reads or writes inside the profile folder (§5)."""

    def __init__(self, root: Path | str):
        self.root = Path(root).expanduser().resolve()

    @classmethod
    def resolve(cls, profile_dir: str | None = None) -> "Paths":
        if profile_dir:
            return cls(profile_dir)
        if os.environ.get(ENV_HOME):
            return cls(os.environ[ENV_HOME])
        return cls(Path.home() / ".autoapply")

    profile = property(lambda s: s.root / "profile.yaml")
    answers = property(lambda s: s.root / "answers.yaml")
    resumes = property(lambda s: s.root / "resumes")
    secrets = property(lambda s: s.root / "secrets.env")
    ledger = property(lambda s: s.root / "ledger.sqlite")
    submissions = property(lambda s: s.root / "submissions")
    calibration = property(lambda s: s.root / "calibration.json")
    browser_profile = property(lambda s: s.root / "browser-profile")
    extension = property(lambda s: s.root / "extensions" / "simplify")
    logs = property(lambda s: s.root / "logs")
    generated = property(lambda s: s.root / "generated")
    screenshots = property(lambda s: s.root / "screenshots")
    lock = property(lambda s: s.root / "autoapply.lock")
    state = property(lambda s: s.root / "state.json")

    def ensure(self) -> None:
        for d in (self.root, self.resumes, self.submissions, self.logs,
                  self.generated, self.screenshots, self.browser_profile):
            d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------- profile.yaml ----
class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceCfg(_Strict):
    type: Literal["jobmonitor", "jobmonitor_public", "csv"]
    repo: str | None = None              # owner/repo
    branch: str = "main"
    tracker: Literal["software"] = "software"
    writeback: bool = False
    path: str | None = None              # csv path, or a local jobs.json (offline/testing)
    h1b_path: str | None = None          # local h1b.json (offline/testing)
    companies_path: str | None = None    # local config/companies.yaml (offline/testing)
    name: str | None = None

    @model_validator(mode="after")
    def _check(self):
        if self.type in ("jobmonitor", "jobmonitor_public") and not (self.repo or self.path):
            raise ValueError(f"{self.type} source needs `repo: owner/repo`")
        if self.type == "csv" and not self.path:
            raise ValueError("csv source needs `path`")
        if self.type != "jobmonitor" and self.writeback:
            raise ValueError("writeback is only possible for a `jobmonitor` source")
        return self

    @property
    def label(self) -> str:
        return self.name or (f"{self.type}:{self.repo}" if self.repo else f"{self.type}:{self.path}")


class FiltersCfg(_Strict):
    tiers: list[str] = ["newgrad", "experienced"]
    roles: list[str] = ["software", "backend", "fullstack", "frontend", "ml-ai", "data"]
    location: Literal["us_or_remote", "metros", "remote_only"] = "us_or_remote"
    metros: list[str] = []
    ats_allowed: list[Literal["greenhouse", "ashby", "lever"]] = ["greenhouse", "ashby", "lever"]
    per_company_cap: int | None = None


class VisaCfg(_Strict):
    status: Literal["citizen_gc", "opt_stem_opt", "needs_sponsorship_now"]
    needs_sponsorship_future: bool
    sponsor_filter: Literal["skip_no_record", "known_filers_only", "off"] = "skip_no_record"
    skip_us_person_roles: bool = True
    confirmed: bool = False


class FitCfg(_Strict):
    cutoff: int = Field(65, ge=0, le=100)
    order: Literal["newest_first"] = "newest_first"


class ResumesCfg(_Strict):
    default: str
    map: dict[str, str] = {}

    def for_role(self, role: str | None) -> str:
        return self.map.get(role or "", self.default)


class BrowserCfg(_Strict):
    parallel_tabs: int = Field(3, ge=1, le=8)
    headless: bool = False
    executable_path: str | None = None   # override Playwright's bundled Chromium
    # Simplify forgets its data-consent card on every launch (see browser/simplify.py);
    # the tool re-applies this choice: essentials = "Essentials only", accept = "Accept"
    simplify_consent: Literal["essentials", "accept"] = "essentials"
    autofill_timeout_s: float = 45
    form_timeout_s: float = 420          # one application, open to screenshot; then it goes to Stuck
    page_timeout_s: float = 45

    @field_validator("headless")
    @classmethod
    def _headed(cls, v):
        if v:
            raise ValueError("browser.headless must stay false in v1 (Simplify needs a visible window)")
        return v


class PriceCfg(_Strict):
    input: float | None = None
    output: float | None = None
    cache_read: float | None = None      # defaults to 0.1 x input
    cache_write: float | None = None     # defaults to 1.25 x input


class LlmCfg(_Strict):
    score_model: str = "claude-haiku-5-5"
    write_model: str = "claude-sonnet-5-5"
    monthly_budget_usd: float = 20
    server_fallback: bool = True         # write_model refusal fallback (Claude API only)
    pricing: dict[str, PriceCfg] = {}


class ScheduleCfg(_Strict):
    mode: Literal["manual", "daily", "twice_daily"] = "manual"
    times: list[str] = ["08:00"]
    time_limit_hours: int = 2

    @model_validator(mode="after")
    def _check(self):
        for t in self.times:
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t):
                raise ValueError(f"schedule time {t!r} is not HH:MM")
        if self.mode == "twice_daily" and len(self.times) != 2:
            raise ValueError("twice_daily needs exactly two times")
        return self


class NotificationsCfg(_Strict):
    discord: bool = False
    needs_input_ping: Literal["immediate", "summary_only"] = "immediate"
    ping_group_threshold: int = 5


class TestingCfg(_Strict):
    never_submit: bool = False


class ReviewCfg(_Strict):
    port: int = 8765


class Profile(_Strict):
    sources: list[SourceCfg]
    filters: FiltersCfg = FiltersCfg()
    visa: VisaCfg
    fit: FitCfg = FitCfg()
    resumes: ResumesCfg
    browser: BrowserCfg = BrowserCfg()
    llm: LlmCfg = LlmCfg()
    schedule: ScheduleCfg = ScheduleCfg()
    notifications: NotificationsCfg = NotificationsCfg()
    testing: TestingCfg = TestingCfg()
    review: ReviewCfg = ReviewCfg()

    @property
    def writeback_source(self) -> SourceCfg | None:
        return next((s for s in self.sources if s.type == "jobmonitor" and s.writeback), None)


def load_profile(paths: Paths) -> Profile:
    if not paths.profile.exists():
        raise ConfigError(f"No profile at {paths.profile}. Run `autoapply setup` first.")
    try:
        data = yaml.safe_load(paths.profile.read_text(encoding="utf-8")) or {}
        return Profile.model_validate(data)
    except yaml.YAMLError as e:
        raise ConfigError(f"profile.yaml is not valid YAML: {e}") from e
    except ValueError as e:
        raise ConfigError(f"profile.yaml is invalid:\n{e}") from e


def save_profile(paths: Paths, profile: Profile) -> None:
    data = profile.model_dump(mode="json", exclude_defaults=False)
    paths.profile.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


# ---------------------------------------------------------------- secrets ----
def load_secrets(paths: Paths) -> dict[str, str]:
    """KEY=VALUE lines from secrets.env; real environment variables win."""
    out: dict[str, str] = {}
    if paths.secrets.exists():
        for line in paths.secrets.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    for k in SECRET_KEYS:
        if os.environ.get(k):
            out[k] = os.environ[k]
    return {k: v for k, v in out.items() if v}


def save_secret(paths: Paths, key: str, value: str) -> None:
    lines = paths.secrets.read_text(encoding="utf-8").splitlines() if paths.secrets.exists() else []
    lines = [ln for ln in lines if not ln.strip().startswith(f"{key}=")]
    lines.append(f"{key}={value}")
    paths.secrets.write_text("\n".join(lines) + "\n", encoding="utf-8")


def redact(text: str, secrets: dict[str, str]) -> str:
    for v in secrets.values():
        if v and len(v) > 6:
            text = text.replace(v, "***")
    return text


# ------------------------------------------------------------ answer bank ----
AnswerKind = Literal["yes_no", "choice", "number", "short_text", "long_text"]


class Answer(BaseModel):
    question: str          # normalized (§8.6)
    original: str
    kind: AnswerKind
    answer: str
    added: date = Field(default_factory=date.today)
    source: Literal["user", "wizard"] = "user"


class AnswerBank:
    def __init__(self, path: Path):
        self.path = path
        self.items: list[Answer] = []
        if path.exists():
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
            if not isinstance(raw, list):
                raise ConfigError("answers.yaml must be a list of entries")
            self.items = [Answer.model_validate(r) for r in raw]

    def lookup(self, label: str, kind: str, options: list[str] | None = None) -> str | None:
        """Strict v1 match: exact normalized question + compatible kind (§8.5.1)."""
        q = normalize_question(label)
        for a in self.items:
            if a.question != q or not _kind_compatible(a.kind, kind):
                continue
            if options:
                norm = {normalize_question(o): o for o in options}
                hit = norm.get(normalize_question(a.answer))
                if hit is None:
                    continue
                return hit
            return a.answer
        return None

    def add(self, original: str, kind: str, answer: str, source: str = "user") -> Answer:
        q = normalize_question(original)
        self.items = [a for a in self.items if not (a.question == q and a.kind == kind)]
        entry = Answer(question=q, original=original, kind=kind, answer=answer, source=source)
        self.items.append(entry)
        self.save()
        return entry

    def save(self) -> None:
        data = [a.model_dump(mode="json") for a in self.items]
        self.path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _kind_compatible(saved: str, field_kind: str) -> bool:
    if saved == field_kind:
        return True
    # a yes/no answer can fill a 2-option choice, a short text can fill a long one
    return {saved, field_kind} in ({"yes_no", "choice"}, {"short_text", "long_text"})
