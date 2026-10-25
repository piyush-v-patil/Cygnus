"""Everything a command needs, built lazily from the profile folder."""
from __future__ import annotations

import json
import logging
import time
from functools import cached_property
from pathlib import Path

from .config import AnswerBank, Paths, Profile, load_profile, load_secrets
from .github import GitHub
from .ledger import Ledger
from .notify import Discord, Notifier

log = logging.getLogger(__name__)


class Context:
    def __init__(self, paths: Paths, profile: Profile | None = None, secrets: dict | None = None,
                 llm_transport=None, test_mode: bool = False):
        self.paths = paths
        self._profile = profile
        self._secrets = secrets
        self._llm_transport = llm_transport
        self.test_mode = test_mode
        self.open_tabs: dict = {}            # posting_key -> filled page, in review sessions
        self.tab_opened: dict = {}           # posting_key -> time.monotonic() when it was filled
        self.keep_tabs = False
        self.simplify_probe_pending = False
        self.browser_overrides: dict | None = None
        self.simplify_mock = None            # tests: async callable(page) standing in for Simplify
        self.form_url_for = None             # tests: posting_key -> local fixture form URL

    @cached_property
    def profile(self) -> Profile:
        return self._profile or load_profile(self.paths)

    @cached_property
    def secrets(self) -> dict:
        return self._secrets if self._secrets is not None else load_secrets(self.paths)

    @cached_property
    def ledger(self) -> Ledger:
        self.paths.ensure()
        return Ledger(self.paths.ledger)

    @cached_property
    def llm(self):
        from .llm import make_llm
        return make_llm(self.profile, self.ledger, self.secrets, self._llm_transport)

    @cached_property
    def sources(self) -> list:
        from .sources import build_sources
        return build_sources(self.profile, self.secrets)

    @cached_property
    def answers(self) -> AnswerBank:
        return AnswerBank(self.paths.answers)

    @cached_property
    def resumes(self):
        from .resumes import Resumes
        return Resumes(self.paths, self.profile)

    @property
    def mailbox(self):
        """The mailbox codes and Workday links are read from, or None (mail is off)."""
        from .mailbox import mailbox_for
        return mailbox_for(self)

    @cached_property
    def notifier(self) -> Notifier:
        n = self.profile.notifications
        return Notifier(Discord(self.secrets.get("DISCORD_WEBHOOK_URL"), n.discord, background=True),
                        n.needs_input_ping, n.ping_group_threshold)

    @cached_property
    def writeback(self):
        from .writeback import Writeback
        src = self.profile.writeback_source
        if src is None or not src.repo or not self.secrets.get("GITHUB_TOKEN"):
            return None
        return Writeback(self.ledger, src.label, src.repo, src.branch, GitHub(self.secrets["GITHUB_TOKEN"]))

    # ---- filled tabs kept open for review (`run --review`, review sessions) ----
    def live_tabs(self) -> dict:
        for k in [k for k, p in self.open_tabs.items() if p.is_closed()]:
            self.open_tabs.pop(k, None)
            self.tab_opened.pop(k, None)
        return self.open_tabs

    def can_keep_tab(self) -> bool:
        return self.keep_tabs and len(self.live_tabs()) < self.profile.browser.keep_tabs_max

    def remember_tab(self, key: str, page, fresh: bool = True) -> None:
        self.open_tabs[key] = page
        if fresh or key not in self.tab_opened:
            self.tab_opened[key] = time.monotonic()

    def take_tab(self, key: str):
        """(page, age in seconds) of a live tab, removed from the registry; (None, None) if none."""
        page = self.open_tabs.pop(key, None)
        opened = self.tab_opened.pop(key, None)
        if page is None or page.is_closed():
            return None, None
        return page, (time.monotonic() - opened) if opened is not None else None

    def has_live_tab(self, key: str) -> bool:
        p = self.open_tabs.get(key)
        return p is not None and not p.is_closed()

    @property
    def never_submit(self) -> bool:
        return self.test_mode or self.profile.testing.never_submit

    def calibration_examples(self) -> list[dict]:
        p = self.paths.calibration
        if not p.exists():
            return []
        try:
            return json.loads(p.read_text(encoding="utf-8")).get("examples", [])
        except ValueError:
            return []

    # small persistent key/value store (Simplify version of the last good run, ...)
    def state_get(self, key: str, default=None):
        try:
            return json.loads(self.paths.state.read_text(encoding="utf-8")).get(key, default)
        except (OSError, ValueError):
            return default

    def state_set(self, key: str, value) -> None:
        try:
            data = json.loads(self.paths.state.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data[key] = value
        Path(self.paths.state).write_text(json.dumps(data, indent=1), encoding="utf-8")
