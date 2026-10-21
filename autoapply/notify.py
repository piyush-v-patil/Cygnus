"""Discord webhook notifications (§14). The webhook URL is a secret: never logged."""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Callable

import requests

log = logging.getLogger(__name__)
MAX_CONTENT = 2000
GROUP_SIZE = 10


def chunk_lines(lines: list[str], limit: int = MAX_CONTENT) -> list[str]:
    """Pack lines into messages under Discord's 2000-char content limit."""
    out, cur = [], ""
    for ln in lines:
        ln = ln if len(ln) <= limit else ln[: limit - 1] + "…"
        if cur and len(cur) + 1 + len(ln) > limit:
            out.append(cur)
            cur = ln
        else:
            cur = f"{cur}\n{ln}" if cur else ln
    if cur:
        out.append(cur)
    return out


class Discord:
    def __init__(self, webhook_url: str | None, enabled: bool = True,
                 post: Callable | None = None, sleep: Callable[[float], None] = time.sleep):
        self.url = webhook_url
        self.enabled = bool(enabled and webhook_url)
        self._post = post or (lambda url, json: requests.post(url, json=json, timeout=20))
        self._sleep = sleep
        self.sent: list[str] = []

    def send(self, content: str) -> bool:
        if not self.enabled:
            return False
        ok = True
        for part in chunk_lines(content.splitlines() or [content]):
            ok = self._send_one(part) and ok
        return ok

    def _send_one(self, content: str) -> bool:
        payload = {"content": content, "allowed_mentions": {"parse": []}}
        for attempt in range(5):
            try:
                r = self._post(self.url, payload)
            except requests.RequestException as e:
                log.warning("Discord unreachable: %s", type(e).__name__)
                self._sleep(2 ** attempt)
                continue
            if r.status_code == 429:
                try:
                    wait = float(r.json().get("retry_after", 1))
                except ValueError:
                    wait = 1.0
                self._sleep(min(wait, 60))
                continue
            if r.status_code >= 500:
                self._sleep(2 ** attempt)
                continue
            if r.status_code >= 400:
                log.warning("Discord rejected the message: HTTP %s", r.status_code)
                return False
            self.sent.append(content)
            return True
        return False


class Notifier:
    """Run-scoped notification policy on top of Discord."""

    def __init__(self, discord: Discord, mode: str = "immediate", group_threshold: int = 5):
        self.discord = discord
        self.mode = mode
        self.threshold = group_threshold
        self.needs_input: list[dict] = []
        self._immediate_sent = 0

    def needs_input_item(self, company: str, title: str, questions: list[str]):
        item = {"company": company, "title": title, "questions": questions}
        self.needs_input.append(item)
        if self.mode == "immediate" and self._immediate_sent < self.threshold:
            self._immediate_sent += 1
            self.discord.send(format_needs_input([item]))

    def flush_needs_input(self):
        """Above the threshold the rest are sent grouped, per company, ≤10 per message."""
        rest = self.needs_input[self._immediate_sent:] if self.mode == "immediate" else self.needs_input
        if not rest:
            return
        for group in group_needs_input(rest):
            self.discord.send(format_needs_input(group))
        self.needs_input.clear()
        self._immediate_sent = 0

    def run_summary(self, counts: dict, spend: float, budget: float):
        skipped = counts.get("skipped_by_reason", {})
        lines = ["**AutoApply run finished**",
                 f"Scored {counts.get('scored', 0)} · Ready {counts.get('ready', 0)} · "
                 f"Needs input {counts.get('needs_input', 0)} · Stuck {counts.get('stuck', 0)} · "
                 f"Errors {counts.get('error', 0)}"]
        if skipped:
            lines.append("Skipped: " + ", ".join(f"{k} {v}" for k, v in sorted(skipped.items())))
        if counts.get("held") or counts.get("unheld"):
            lines.append(f"Below the cutoff, kept for later: {counts.get('held', 0)} new"
                         + (f" · {counts['unheld']} now above it" if counts.get("unheld") else ""))
        if counts.get("closed"):
            lines.append(f"Closed: {counts['closed']}")
        if counts.get("stopped"):
            lines.append(f"Stopped early: {counts['stopped']}")
        lines.append(f"Spend this month: ${spend:.2f} of ${budget:.2f}")
        self.discord.send("\n".join(lines))

    def review_summary(self, submitted: int, manual: int = 0):
        self.discord.send(f"**AutoApply review session**: submitted {submitted}"
                          + (f" (+{manual} by hand)" if manual else ""))

    def alert(self, message: str):
        self.discord.send(f"⚠️ **AutoApply stopped**: {message}")


def group_needs_input(items: list[dict]) -> list[list[dict]]:
    by_co: dict[str, list[dict]] = defaultdict(list)
    for it in items:
        by_co[it["company"]].append(it)
    groups, cur = [], []
    for co in sorted(by_co):
        for it in by_co[co]:
            cur.append(it)
            if len(cur) == GROUP_SIZE:
                groups.append(cur)
                cur = []
    if cur:
        groups.append(cur)
    return groups


def format_needs_input(items: list[dict]) -> str:
    lines = ["**Needs input**" if len(items) == 1 else f"**Needs input ({len(items)})**"]
    for it in items:
        lines.append(f"• {it['company']} — {it['title']}")
        for q in it["questions"][:5]:
            lines.append(f"   ◦ {q[:180]}")
    return "\n".join(lines)
