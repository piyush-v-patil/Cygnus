"""Profile folder: layout, profile.yaml schema, secrets and the answer bank.

Everything personal lives in the profile folder (default ~/.autoapply, or
--profile-dir / AUTOAPPLY_HOME). The code never holds personal data (spec §2.1).
"""
from __future__ import annotations

import logging
import os
import re
from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .normalize import normalize_question

log = logging.getLogger(__name__)
ENV_HOME = "AUTOAPPLY_HOME"
SECRET_KEYS = ("ANTHROPIC_API_KEY", "GITHUB_TOKEN", "DISCORD_WEBHOOK_URL", "WORKDAY_EMAIL", "WORKDAY_PASSWORD",
               "MAIL_ADDRESS", "MAIL_APP_PASSWORD")


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
    # workday also needs WORKDAY_EMAIL / WORKDAY_PASSWORD (autoapply setup) and an account per company
    ats_allowed: list[Literal["greenhouse", "ashby", "lever", "workday"]] = ["greenhouse", "ashby", "lever"]
    per_company_cap: int | None = None


class VisaCfg(_Strict):
    status: Literal["citizen_gc", "opt_stem_opt", "needs_sponsorship_now"]
    needs_sponsorship_future: bool
    sponsor_filter: Literal["skip_no_record", "known_filers_only", "off"] = "skip_no_record"
    skip_us_person_roles: bool = True
    # employers skipped up front when skip_us_person_roles is on (built-in list in us_person.py)
    us_person_companies_add: list[str] = []
    us_person_companies_remove: list[str] = []
    confirmed: bool = False


class FitCfg(_Strict):
    cutoff: int = Field(65, ge=0, le=100)
    # below the cutoff: hold = park it (not marked skip in job-monitor; filled later if the
    # cutoff drops or a better-matching resume raises its score) | skip = drop it for good
    low_fit: Literal["hold", "skip"] = "hold"
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
    workday_form_timeout_s: float = 900     # a Workday wizard has 5-7 pages
    form_timeout_s: float = 420          # one application, open to screenshot; then it goes to Stuck
    # `autoapply run --review`: filled tabs stay open for approval (beyond the cap, or older
    # than the age limit, approval refills the form from its saved plan as usual)
    keep_tabs_max: int = Field(15, ge=0, le=60)
    tab_max_age_min: float = 240
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
    limit: int | None = Field(50, ge=1)    # --limit for each scheduled run; null = no limit

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


class WorkdayCfg(_Strict):
    # when your Workday login is refused at a company, create the account there with it,
    # wait, and sign in (false: you create accounts yourself with `autoapply workday open`)
    create_accounts: bool = True
    after_create_wait_s: float = Field(5, ge=0, le=60)


class MailCfg(_Strict):
    # read the emails sites send (security codes, Workday verify/reset links) from your
    # mailbox: read-only IMAP with an app password (`autoapply mail login`)
    enabled: bool = False
    imap_host: str = "imap.gmail.com"
    wait_s: float = Field(180, ge=10, le=900)


class LimitsCfg(_Strict):
    """How much of a run, and of a month, one company may take (autoapply/companies.py)."""
    per_company_per_run: int | None = Field(1, ge=1)  # postings filled per company in one run; null = no limit
    # automatic pause: `company_max` applications sent to a company within `company_days`
    # days pause it until the oldest of them is `company_days` old. null = off
    company_max: int | None = Field(None, ge=1)
    company_days: int = Field(30, ge=1, le=365)


class WorkCfg(_Strict):
    """Standing answers to work-preference questions (autoapply/questions.py).
    null means "ask me": that question goes to Needs input as before."""
    relocate: bool | None = None               # "Are you willing to relocate?"
    anywhere_in_us: bool | None = None         # "...open to any of our US locations?"
    onsite: bool | None = None                 # "...able to work on-site / in office 5 days a week?"
    hybrid: bool | None = None
    remote: bool | None = None
    relocation_assistance: bool | None = None  # "Will you need relocation assistance?"
    # every employer you have had is on your resume, so "have you worked for <company>?"
    # is No when the resume does not name it (and yours to answer when it does)
    only_resume_employers: bool = False


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
    workday: WorkdayCfg = WorkdayCfg()
    mail: MailCfg = MailCfg()
    work: WorkCfg = WorkCfg()
    limits: LimitsCfg = LimitsCfg()

    @property
    def writeback_source(self) -> SourceCfg | None:
        return next((s for s in self.sources if s.type == "jobmonitor" and s.writeback), None)


class _UniqueKeys(yaml.SafeLoader):
    """A key written twice in the same section is an error, not "the last one wins"."""


def _unique_mapping(loader, node, deep=False):
    seen: dict = {}
    for k, _ in node.value:
        key = loader.construct_object(k, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"`{key}` is written twice (lines {seen[key] + 1} and {k.start_mark.line + 1}): "
                "keep one and delete the other", k.start_mark)
        seen[key] = k.start_mark.line
    return loader.construct_mapping(node, deep=deep)


_UniqueKeys.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def read_yaml(path: Path):
    """YAML a person edits: a Notepad byte-order mark is fine, a repeated key is not."""
    return yaml.load(path.read_text(encoding="utf-8-sig"), Loader=_UniqueKeys)  # noqa: S506 - SafeLoader subclass


def load_profile(paths: Paths) -> Profile:
    if not paths.profile.exists():
        raise ConfigError(f"No profile at {paths.profile}. Run `autoapply setup` first.")
    try:
        data = read_yaml(paths.profile) or {}
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
        for line in paths.secrets.read_text(encoding="utf-8-sig").splitlines():
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
    lines = paths.secrets.read_text(encoding="utf-8-sig").splitlines() if paths.secrets.exists() else []
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

    @field_validator("question", "original", "answer", mode="before")
    @classmethod
    def _text(cls, v):
        # hand edits: `answer: yes` reads as True and `answer: 3` as a number
        if isinstance(v, bool):
            return "Yes" if v else "No"
        return str(v) if isinstance(v, (int, float)) else v

    @model_validator(mode="before")
    @classmethod
    def _fill_question(cls, d):
        if isinstance(d, dict) and d.get("original") and not d.get("question"):
            d = {**d, "question": normalize_question(str(d["original"]))}
        return d


class AnswerBank:
    def __init__(self, path: Path):
        self.path = path
        self.items: list[Answer] = []
        self._unreadable: list = []        # hand-edited entries we could not use, kept as written
        if path.exists():
            try:
                raw = read_yaml(path) or []
            except yaml.YAMLError as e:
                raise ConfigError(f"answers.yaml is not valid YAML: {e}") from e
            if not isinstance(raw, list):
                raise ConfigError("answers.yaml must be a list of entries (each starting with `- `)")
            for i, r in enumerate(raw):
                try:
                    self.items.append(Answer.model_validate(r))
                except ValueError as e:
                    self._unreadable.append(r)
                    log.warning("answers.yaml entry %d is skipped (fix it to use it): %s", i + 1,
                                str(e).splitlines()[0] if str(e) else type(e).__name__)

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
        data = [a.model_dump(mode="json") for a in self.items] + self._unreadable
        self.path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _kind_compatible(saved: str, field_kind: str) -> bool:
    if saved == field_kind:
        return True
    # a yes/no answer can fill a 2-option choice, a short text can fill a long one
    return {saved, field_kind} in ({"yes_no", "choice"}, {"short_text", "long_text"})
