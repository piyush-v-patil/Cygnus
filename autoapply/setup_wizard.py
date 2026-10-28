"""`autoapply setup` (§5.2): an interactive wizard that writes the profile folder.

Every file it writes stays hand-editable. Visa answers are never saved until
the user explicitly confirms them.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import zipfile
from pathlib import Path
from typing import Callable

import yaml

from .config import ConfigError, Paths, Profile, load_secrets, read_yaml, save_secret
from .llm.score import ROLES

TIERS = ["intern", "newgrad", "experienced"]
VISA_STATUS = ["citizen_gc", "opt_stem_opt", "needs_sponsorship_now"]
CRX_URL = ("https://clients2.google.com/service/update2/crx?response=redirect&prodversion=141.0"
           "&acceptformat=crx2,crx3&x=id%3D{id}%26uc")

PROFILE_HEADER = """\
# Cygnus / AutoApply profile. Written by `autoapply setup`; safe to edit by hand.
# Full reference: config.example/profile.yaml in the repository.
"""


class IO:
    """Terminal prompts; tests substitute scripted answers."""

    def __init__(self, ask: Callable[[str], str] = input, say: Callable[[str], None] = print):
        self._ask, self.say = ask, say

    def ask(self, prompt: str, default: str | None = None) -> str:
        suffix = f" [{default}]" if default not in (None, "") else ""
        v = self._ask(f"{prompt}{suffix}: ").strip()
        return v if v else (default or "")

    def yes(self, prompt: str, default: bool = True) -> bool:
        v = self._ask(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
        return default if not v else v.startswith("y")

    def choose(self, prompt: str, options: list[str], default: str | None = None) -> str:
        while True:
            v = self.ask(f"{prompt} ({' | '.join(options)})", default)
            if v in options:
                return v
            self.say(f"  Please type one of: {', '.join(options)}")

    def secret(self, prompt: str) -> str:
        """A prompt whose typing is not shown (tests: the scripted answers)."""
        if self._ask is input:
            import getpass
            return getpass.getpass(f"{prompt}: ").strip()
        return self._ask(f"{prompt}: ").strip()

    def many(self, prompt: str, options: list[str], default: list[str]) -> list[str]:
        while True:
            v = self.ask(f"{prompt} (comma-separated from: {', '.join(options)})", ", ".join(default))
            picked = [x.strip() for x in v.split(",") if x.strip()]
            bad = [x for x in picked if x not in options]
            if picked and not bad:
                return picked
            self.say(f"  Unknown: {', '.join(bad) or '(empty)'}")


# ------------------------------------------------------------ Simplify --
# Chromium-family browsers install Chrome Web Store extensions under the same id,
# so Simplify can be copied from whichever of them the user has.
BROWSER_DIRS = {
    "win32": [("Chrome", ("Google", "Chrome", "User Data")),
              ("Brave", ("BraveSoftware", "Brave-Browser", "User Data")),
              ("Edge", ("Microsoft", "Edge", "User Data")),
              ("Chromium", ("Chromium", "User Data"))],
    "darwin": [("Chrome", ("Google", "Chrome")), ("Brave", ("BraveSoftware", "Brave-Browser")),
               ("Edge", ("Microsoft Edge",)), ("Chromium", ("Chromium",))],
    "linux": [("Chrome", ("google-chrome",)), ("Brave", ("BraveSoftware", "Brave-Browser")),
              ("Edge", ("microsoft-edge",)), ("Chromium", ("chromium",))],
}


def browser_extension_roots() -> list[tuple[str, Path]]:
    """(browser name, <profile>/Extensions) for every installed Chromium-family browser profile."""
    home = Path.home()
    if sys.platform == "win32":
        base, key = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local")), "win32"
    elif sys.platform == "darwin":
        base, key = home / "Library" / "Application Support", "darwin"
    else:
        base, key = home / ".config", "linux"
    out = []
    for name, parts in BROWSER_DIRS[key]:
        root = base.joinpath(*parts)
        if root.is_dir():
            out += [(name, p / "Extensions") for p in root.iterdir() if (p / "Extensions").is_dir()]
    return out


def _vkey(name: str) -> tuple:
    return tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[._]", name))


def find_installed_simplify(ext_id: str) -> tuple[str, Path] | None:
    """(browser, folder) of the newest installed version of the extension in any browser profile."""
    found = []
    for browser, root in browser_extension_roots():
        d = root / ext_id
        if d.is_dir():
            found += [(browser, v) for v in d.iterdir() if (v / "manifest.json").exists()]
    return max(found, key=lambda bv: _vkey(bv[1].name)) if found else None


def install_extension(src: Path, dest: Path) -> str:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    return json.loads((dest / "manifest.json").read_text(encoding="utf-8"))["version"]


def download_extension(ext_id: str, dest: Path) -> str:
    """Fallback: fetch the CRX from the Chrome Web Store update service and unpack it."""
    import io as _io

    import requests
    r = requests.get(CRX_URL.format(id=ext_id), timeout=120)
    r.raise_for_status()
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    with zipfile.ZipFile(_io.BytesIO(r.content)) as z:     # a CRX3 is a zip with a signed header
        for m in z.infolist():
            target = (dest / m.filename).resolve()
            if dest.resolve() not in target.parents and target != dest.resolve():
                raise ValueError("unsafe path in extension archive")
        z.extractall(dest)
    return json.loads((dest / "manifest.json").read_text(encoding="utf-8"))["version"]


# --------------------------------------------------------------- steps --
def step_resumes(paths: Paths, io: IO) -> dict:
    from .resumes import extract_text
    io.say("\n== Resumes ==")
    existing = sorted(p.name for p in paths.resumes.glob("*.pdf"))
    if existing:
        io.say(f"Already in {paths.resumes}: {', '.join(existing)}")
    mapping: dict[str, str] = {}
    default = existing[0] if existing else None
    while True:
        src = io.ask("Path to a resume PDF (empty when done)").strip('"')
        if not src:
            if default:
                break
            io.say("  At least one resume is needed.")
            continue
        p = Path(src).expanduser()
        if not p.exists() or p.suffix.lower() != ".pdf":
            io.say("  Not a PDF file.")
            continue
        dest = paths.resumes / p.name
        shutil.copy2(p, dest)
        try:
            text = extract_text(dest)
        except ConfigError as e:
            io.say(f"  {e}")
            dest.unlink()
            continue
        io.say(f"  Copied {p.name} ({len(text.split())} words extracted).")
        fams = io.ask("  Role families this resume is for (e.g. backend, ml-ai; empty = default for all)")
        if not fams or default is None:
            default = p.name
        for f in [x.strip() for x in fams.split(",") if x.strip()]:
            if f in ROLES:
                mapping[f] = p.name
            else:
                io.say(f"  ignoring unknown role family {f!r}")
    default = io.choose("Default resume", sorted({default, *mapping.values(), *existing}), default)
    return {"default": default, "map": mapping}


def draft_from_resume(api_key: str, paths: Paths, resumes: dict, model: str) -> dict | None:
    """Claude reads the resume(s) and drafts filters + visa (§5.2 step 3)."""
    from .ledger import Ledger
    from .llm import AnthropicTransport, LLM
    from .llm import prompts
    from .config import LlmCfg
    from .resumes import extract_text
    texts = []
    for name in sorted({resumes["default"], *resumes["map"].values()}):
        texts.append(f"<resume file=\"{name}\">\n{extract_text(paths.resumes / name)}\n</resume>")
    schema = {"type": "object", "additionalProperties": False,
              "required": ["tiers", "roles", "location", "visa_status", "needs_sponsorship_future",
                           "visa_evidence", "notes"],
              "properties": {
                  "tiers": {"type": "array", "items": {"type": "string", "enum": TIERS}},
                  "roles": {"type": "array", "items": {"type": "string", "enum": ROLES}},
                  "location": {"type": "string", "enum": ["us_or_remote", "metros", "remote_only"]},
                  "visa_status": {"type": "string", "enum": [*VISA_STATUS, "unknown"]},
                  "needs_sponsorship_future": {"anyOf": [{"type": "boolean"}, {"type": "null"}]},
                  "visa_evidence": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                  "notes": {"type": "string"}}}
    led = Ledger(paths.ledger)
    llm = LLM(LlmCfg(write_model=model), led, AnthropicTransport(api_key))
    system = [{"type": "text", "text": prompts.load("wizard", roles=", ".join(ROLES), resumes="\n".join(texts))}]
    try:
        return llm.call("wizard", model, system, "Draft my profile values.", schema=schema, max_tokens=4000).data
    finally:
        led.close()


def step_preferences(io: IO, draft: dict | None) -> tuple[dict, dict]:
    io.say("\n== What to apply to ==")
    d = draft or {}
    if d.get("notes"):
        io.say(f"Claude's notes on your resume: {d['notes']}")
    tiers = io.many("Tiers", TIERS, d.get("tiers") or ["newgrad", "experienced"])
    roles = io.many("Role families", ROLES, d.get("roles") or ["software", "backend", "fullstack"])
    location = io.choose("Location", ["us_or_remote", "metros", "remote_only"], d.get("location") or "us_or_remote")
    metros = []
    if location == "metros":
        metros = [m.strip() for m in io.ask("Metros (comma-separated, e.g. Seattle, Austin)").split(",") if m.strip()]
    filters = {"tiers": tiers, "roles": roles, "location": location, "metros": metros,
               "ats_allowed": ["greenhouse", "ashby", "lever"], "per_company_cap": None}

    io.say("\n== Work authorization (please read carefully) ==")
    io.say("These answers decide which jobs are skipped and are checked against what Simplify fills on\n"
           "every form. A wrong answer here is the costliest mistake the tool can make.")
    if d.get("visa_status") and d["visa_status"] != "unknown":
        io.say(f"  Claude's reading of your resume: {d['visa_status']}"
               + (f" (evidence: \"{d['visa_evidence']}\")" if d.get("visa_evidence") else "")
               + " — this is only a draft, NOT saved until you confirm.")
    status = io.choose("Your status", VISA_STATUS, d.get("visa_status") if d.get("visa_status") in VISA_STATUS else None)
    future = status == "needs_sponsorship_now"
    if status == "opt_stem_opt":
        future = io.yes("Will you need H-1B (or other) sponsorship in the future?", True)
    elif status == "citizen_gc":
        future = False
    sponsor = "off" if status == "citizen_gc" else io.choose(
        "Sponsor filter", ["skip_no_record", "known_filers_only", "off"], "skip_no_record")
    skip_us = io.yes("Skip roles that require US citizenship / US-person status / clearance?", status != "citizen_gc")
    visa = {"status": status, "needs_sponsorship_future": future, "sponsor_filter": sponsor,
            "skip_us_person_roles": skip_us, "confirmed": False}
    io.say(f"\n  status: {status}\n  needs sponsorship in future: {'yes' if future else 'no'}\n"
           f"  sponsor filter: {sponsor}\n  skip US-person roles: {'yes' if skip_us else 'no'}")
    if not io.yes("Confirm these work-authorization answers are correct?", False):
        io.say("  Not confirmed - let's go through it again.")
        return step_preferences(io, {**d, "visa_status": status})
    visa["confirmed"] = True
    return filters, visa


YES_NO = {"y", "yes", "n", "no"}
REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


WORK_QUESTIONS = (
    ("relocate", "Willing to relocate", "yes"),
    ("anywhere_in_us", "Open to any US location", "yes"),
    ("onsite", "Will work on-site (in the office every day)", "yes"),
    ("hybrid", "Will work hybrid", "yes"),
    ("remote", "Will work remotely", "yes"),
    ("relocation_assistance", "Need relocation assistance", "no"),
)


def step_work(io: IO, current: dict | None = None) -> dict | None:
    """Standing answers to work-preference questions (profile `work:`); None = skipped.
    `current` (the saved ones) are the defaults offered."""
    io.say("\n== Work preferences ==\n  Forms ask the same things again and again: will you relocate, work on-site,\n"
           "  hybrid or remote, have you worked here before. Answer once and Cygnus answers them\n"
           "  for you (never visa, legal or demographic questions). \"ask\" leaves one to you.")
    if not io.yes("Set your work preferences now?", True):
        return None
    cur = current or {}
    work: dict = {}
    for key, prompt, default in WORK_QUESTIONS:
        if key in cur:
            default = "ask" if cur[key] is None else ("yes" if cur[key] else "no")
        v = io.choose(prompt, ["yes", "no", "ask"], default)
        work[key] = None if v == "ask" else v == "yes"
    work["only_resume_employers"] = io.yes(
        "Is every employer you have worked for on your resume? (then \"have you worked for <company>?\" "
        "is answered No when the resume does not name it)", cur.get("only_resume_employers", True))
    return work


def step_fit(io: IO, current: dict | None = None) -> dict:
    io.say("\n== Fit ==")
    cutoff = ask_number(io, "Fit cutoff 0-100 (postings scoring lower are skipped)",
                        (current or {}).get("cutoff", 65), 0, 100)
    return {"cutoff": cutoff, "order": "newest_first"}


def step_limits(io: IO, current: dict | None = None) -> dict:
    cur = current or {}
    io.say("  Big employers post dozens of openings; these keep one company from taking your runs.")
    per_run = ask_number(io, "Most applications to one company in a run (0 = no limit)",
                         cur.get("per_company_per_run", 1) or 0, 0, 50)
    company_max = ask_number(io, "Pause a company after this many applications (0 = never)",
                             cur.get("company_max") or 0, 0, 100)
    company_days = (ask_number(io, "  ...sent within how many days", cur.get("company_days", 30), 1, 365)
                    if company_max else cur.get("company_days", 30))
    return {"per_company_per_run": per_run or None, "company_max": company_max or None,
            "company_days": company_days}


def step_schedule(io: IO, current: dict | None = None) -> dict:
    cur = current or {}
    mode = io.choose("Schedule", ["manual", "daily", "twice_daily"], cur.get("mode", "daily"))
    times = cur.get("times") or (["08:00"] if mode != "twice_daily" else ["08:00", "18:00"])
    if mode == "twice_daily" and len(times) < 2:
        times = ["08:00", "18:00"]
    if mode != "manual":
        times = ask_times(io, "Run time(s), local HH:MM", times)
    return {"mode": mode, "times": times}


def ask_repo(io: IO, prompt: str, default: str) -> str:
    """A text prompt answered with 'y' would save 'y' as the repo name; ask again instead."""
    while True:
        v = io.ask(prompt, default).strip().removeprefix("https://github.com/").strip("/")
        if REPO_RE.fullmatch(v):
            return v
        io.say(f"  {v!r} is not owner/repo (for example {default}). Press Enter to use {default}.")


def ask_branch(io: IO, prompt: str, default: str) -> str:
    while True:
        v = io.ask(prompt, default).strip()
        if v and v.lower() not in YES_NO and " " not in v:
            return v
        io.say(f"  {v!r} is not a branch name. Press Enter to use {default}.")


def step_sources(paths: Paths, io: IO) -> list[dict]:
    from .config import SourceCfg
    from .sources import CsvSource, JobMonitorSource
    io.say("\n== Job sources ==")
    sources = []
    if io.yes("Use your job-monitor fork on GitHub?", True):
        repo = ask_repo(io, "job-monitor repo (owner/repo) - press Enter for the default", "Piyush-v-patil/job-monitor")
        branch = ask_branch(io, "Branch - press Enter for the default", "main")
        wb = io.yes("Mark applied/skipped jobs back in job-monitor (needs a GitHub token with Contents: write)?", True)
        token = load_secrets(paths).get("GITHUB_TOKEN")
        if wb and token and io.yes(f"  A GitHub token is saved ({mask(token)}). Replace it (e.g. after a leak)?", False):
            token = None
        if wb and not token:
            token = io.secret("GitHub fine-grained token (Contents: read and write on the fork; typing is "
                              "hidden; empty to skip)")
            if token:
                save_secret(paths, "GITHUB_TOKEN", token)     # only what was typed here is saved
            else:
                wb = False
        cfg = {"type": "jobmonitor", "repo": repo, "branch": branch, "tracker": "software", "writeback": wb}
        try:
            io.say("  " + JobMonitorSource(SourceCfg(**cfg), token).check())
        except Exception as e:  # noqa: BLE001
            io.say(f"  Could not read it yet ({e}); check the name or token later with `autoapply health`.")
        sources.append(cfg)
    if io.yes("Also read a CSV / text file of job links?", False):
        path = io.ask("Path to the file").strip('"')
        cfg = {"type": "csv", "path": path}
        try:
            io.say("  " + CsvSource(SourceCfg(**cfg)).check())
            sources.append(cfg)
        except Exception as e:  # noqa: BLE001
            io.say(f"  Skipped: {e}")
    if not sources:
        io.say("  No source chosen - add one to profile.yaml later.")
    return sources


def step_discord(paths: Paths, io: IO) -> bool:
    if not io.yes("Send notifications to Discord?", True):
        return False
    if load_secrets(paths).get("DISCORD_WEBHOOK_URL"):
        return True
    url = io.secret("Discord webhook URL (typing is hidden; empty to skip)")
    if not url:
        return False
    from .notify import Discord
    Discord(url).send("✅ Cygnus is connected. You will get Needs-input pings and run summaries here.")
    if io.yes("Did a test message arrive in Discord?", True):
        save_secret(paths, "DISCORD_WEBHOOK_URL", url)
        return True
    io.say("  Not saved - check the webhook URL and rerun setup.")
    return False


def step_workday(paths: Paths, io: IO) -> bool:
    """Workday sites need an account per company. True when a login is saved."""
    io.say("\n== Workday ==\n  Many large companies (Capital One, NVIDIA, Salesforce...) take applications on\n"
           "  Workday, which needs an account on each company's site, all with one email and\n"
           "  password. Cygnus signs in with them; where you have no account yet it creates one\n"
           "  with them, ticking that company's account agreement box (set workday.create_accounts:\n"
           "  false in profile.yaml to create them yourself). Passwords need 8+ characters with an\n"
           "  uppercase and a lowercase letter, a number and a special character.")
    if not io.yes("Apply on Workday sites too?", True):
        return False
    have = load_secrets(paths)
    if have.get("WORKDAY_EMAIL") and have.get("WORKDAY_PASSWORD"):
        return True
    email = io.ask("Email for your Workday accounts")
    pw = io.secret("Password for your Workday accounts (typing is hidden)")
    if not email or "@" not in email or not pw:
        io.say("  Skipped - add it later with `autoapply workday login`.")
        return False
    from .browser.workday import password_problems
    weak = password_problems(pw)
    if weak:
        io.say(f"  Workday passwords need {', '.join(weak)}. Skipped - set one with `autoapply workday login`.")
        return False
    save_secret(paths, "WORKDAY_EMAIL", email)
    save_secret(paths, "WORKDAY_PASSWORD", pw)
    return True


def step_mail(paths: Paths, io: IO) -> bool:
    """Read-only access to the mailbox the sites write to. True when it works."""
    io.say("\n== Email ==\n  Greenhouse and Ashby sometimes email a security code after Submit, and a new Workday\n"
           "  account may need its email confirmed. Cygnus can read just those emails (read-only,\n"
           "  only mail that arrives after it acts) with a Gmail app password: turn on 2-Step\n"
           "  Verification, then create one at https://myaccount.google.com/apppasswords.")
    if not io.yes("Let Cygnus read those emails?", False):
        return False
    have = load_secrets(paths)
    address = io.ask("Email address", have.get("MAIL_ADDRESS") or have.get("WORKDAY_EMAIL") or "")
    pw = io.secret("Gmail app password (16 letters, typing is hidden)").replace(" ", "")
    if "@" not in address or not pw:
        io.say("  Skipped - set it up later with `autoapply mail login`.")
        return False
    from .mailbox import MailError, Mailbox
    try:
        io.say("  " + Mailbox("imap.gmail.com", address, pw).check())
    except MailError as e:
        io.say(f"  That did not work ({e}). Skipped - try again with `autoapply mail login`.")
        return False
    save_secret(paths, "MAIL_ADDRESS", address)
    save_secret(paths, "MAIL_APP_PASSWORD", pw)
    return True


def ensure_anthropic_key(paths: Paths, io: IO) -> str | None:
    key = load_secrets(paths).get("ANTHROPIC_API_KEY")       # file or environment; never re-saved
    while not key:
        key = io.secret("Anthropic API key (sk-ant-...; typing is hidden; empty to skip)")
        if not key:
            return None
        try:
            from .llm import AnthropicTransport
            AnthropicTransport(key).ping("claude-haiku-5-5")
        except Exception as e:  # noqa: BLE001
            io.say(f"  That key did not work: {type(e).__name__}")
            key = None
            continue
        save_secret(paths, "ANTHROPIC_API_KEY", key)
    return key


def step_simplify(paths: Paths, io: IO) -> str:
    """Install the extension and log in. Returns the data-consent choice for browser.simplify_consent."""
    from .browser.simplify import EXTENSION_ID
    io.say("\n== Simplify Copilot ==")
    found = find_installed_simplify(EXTENSION_ID)
    try:
        if found is not None:
            browser, src = found
            v = install_extension(src, paths.extension)
            io.say(f"  Copied Simplify {v} from your {browser} profile.")
        elif io.yes("Simplify was not found in Chrome, Brave or Edge. Download it from the Chrome Web Store instead?",
                    True):
            v = download_extension(EXTENSION_ID, paths.extension)
            io.say(f"  Downloaded Simplify {v}.")
        else:
            io.say("  Install Simplify Copilot in Chrome, Brave or Edge (chromewebstore.google.com), then rerun setup.")
            return "essentials"
    except Exception as e:  # noqa: BLE001
        io.say(f"  Could not install the extension: {e}")
        return "essentials"
    io.say("  Simplify asks once per browser start whether it may collect anonymous usage and error data\n"
           "  ('Accept') or only what autofill needs ('Essentials only'). The tool answers it for you.")
    consent = "accept" if io.choose("  Your answer", ["essentials", "accept"], "essentials") == "accept" else "essentials"
    if io.yes("Open the tool's browser now to log in to Simplify?", True):
        login_browser(paths, io, consent=consent)
    return consent


def login_browser(paths: Paths, io: IO, url: str | None = None, executable_path: str | None = None,
                  consent: str = "essentials", note: str | None = None, extension: bool = True,
                  prepare=None) -> None:
    """Open the tool's own Chromium (with Simplify and its saved login) until the user presses Enter."""
    import asyncio
    from .browser.simplify import LOGIN_URL

    async def go():
        from playwright.async_api import async_playwright
        ext = str(paths.extension)
        async with async_playwright() as pw:
            kw = dict(ignore_default_args=["--disable-extensions"],
                      args=[f"--disable-extensions-except={ext}", f"--load-extension={ext}"]) if extension else {}
            ctx = await pw.chromium.launch_persistent_context(
                str(paths.browser_profile), headless=False, no_viewport=True,
                executable_path=executable_path or None, **kw)
            if extension:
                from .browser.launch import settle_extension
                await settle_extension(ctx, consent)
            page = await ctx.new_page()
            await page.goto(url or LOGIN_URL)
            if prepare is not None:
                try:
                    await prepare(page)
                except Exception as e:  # noqa: BLE001 - the user can still do it by hand
                    io.say(f"  (Could not prepare the page: {type(e).__name__}; do it by hand.)")
            io.say(note or "  In the browser window: log in to Simplify and check your Simplify profile is\n"
                   "  complete (work authorization, sponsorship, demographics, salary, education,\n"
                   "  experience). Simplify's answers are what most forms get. Its 'Before you\n"
                   "  autofill' data-consent card is answered for you from browser.simplify_consent.")
            await asyncio.to_thread(io.ask, "  Press Enter here when you are done")
            await ctx.close()
    asyncio.run(go())


def mask(secret: str) -> str:
    return f"{secret[:4]}…{secret[-4:]}" if len(secret) > 12 else "…"


def ask_number(io: IO, prompt: str, default: int, lo: int, hi: int) -> int:
    while True:
        v = io.ask(prompt, str(default))
        if v.strip().isdigit() and lo <= int(v) <= hi:
            return int(v)
        io.say(f"  Please type a whole number from {lo} to {hi}.")


def ask_times(io: IO, prompt: str, default: list[str]) -> list[str]:
    """Times as HH:MM; "8:00" and "8" are fine."""
    while True:
        out, bad = [], []
        for t in (x.strip() for x in io.ask(prompt, ", ".join(default)).split(",")):
            m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?", t)
            if m and int(m.group(1)) < 24 and int(m.group(2) or 0) < 60:
                out.append(f"{int(m.group(1)):02d}:{m.group(2) or '00'}")
            elif t:
                bad.append(t)
        if out and not bad:
            return out
        io.say(f"  Not a time: {', '.join(bad) or '(empty)'} - use 24-hour HH:MM, e.g. 08:00, 18:30.")


def _merge(old: dict, new: dict) -> dict:
    """`new` wins; settings only the old file had (hand edits) are kept."""
    out = dict(old)
    for k, v in new.items():
        out[k] = _merge(old[k], v) if isinstance(v, dict) and isinstance(old.get(k), dict) else v
    return out


def write_profile(paths: Paths, data: dict) -> str:
    """Write profile.yaml. An existing one is backed up first and its other settings kept.
    Returns a note for the user."""
    Profile.model_validate(data)            # fail here, not on the first run
    note = ""
    if paths.profile.exists():
        from datetime import datetime
        backup = paths.profile.with_name(f"profile.yaml.{datetime.now():%Y%m%d-%H%M%S}.bak")
        old_text = paths.profile.read_text(encoding="utf-8-sig")
        backup.write_text(old_text, encoding="utf-8")
        note = f"Your previous profile.yaml is saved as {backup.name}"
        try:
            old = yaml.safe_load(old_text) or {}
            merged = _merge(old, data) if isinstance(old, dict) else data
            Profile.model_validate(merged)
            data = merged
            note += "; settings this setup did not ask about were kept."
        except Exception:  # noqa: BLE001 - an old file that no longer validates is not merged
            note += "; it could not be merged, so only the answers given here are in the new one."
    paths.profile.write_text(PROFILE_HEADER + yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return note


def run_wizard(paths: Paths, io: IO | None = None) -> int:
    io = io or IO()
    io.say(f"Cygnus setup. Profile folder: {paths.root}")
    if not io.yes("Use this folder?", True):
        paths = Paths(io.ask("Profile folder"))
    paths.ensure()
    resumes = step_resumes(paths, io)
    key = ensure_anthropic_key(paths, io)
    draft = None
    if key and io.yes("Let Claude draft your preferences from your resume?", True):
        try:
            draft = draft_from_resume(key, paths, resumes, "claude-sonnet-5-5")
        except Exception as e:  # noqa: BLE001
            io.say(f"  Draft failed ({type(e).__name__}); continuing with defaults.")
    filters, visa = step_preferences(io, draft)
    work = step_work(io)
    sources = step_sources(paths, io)
    discord = step_discord(paths, io)
    if step_workday(paths, io):
        allowed = list(filters.get("ats_allowed") or ["greenhouse", "ashby", "lever"])
        filters["ats_allowed"] = allowed + ([] if "workday" in allowed else ["workday"])
    mail = step_mail(paths, io)
    consent = step_simplify(paths, io)
    fit = step_fit(io)
    limits = step_limits(io)
    schedule = step_schedule(io)
    mode = schedule["mode"]
    data = {
        "sources": sources or [{"type": "csv", "path": str(paths.root / "jobs.csv")}],
        "filters": filters, "visa": visa, "fit": fit,
        "resumes": resumes, "browser": {"parallel_tabs": 3, "headless": False, "simplify_consent": consent},
        "llm": {"score_model": "claude-haiku-5-5", "write_model": "claude-sonnet-5-5", "monthly_budget_usd": 20},
        "schedule": schedule,
        "notifications": {"discord": discord,
                          "needs_input_ping": "immediate", "ping_group_threshold": 5},
        "testing": {"never_submit": False},
        "mail": {"enabled": mail},
    }
    if work is not None:
        data["work"] = work
    data["limits"] = limits
    note = write_profile(paths, data)
    if not paths.answers.exists():
        paths.answers.write_text("[]\n", encoding="utf-8")
    io.say(f"\nWrote {paths.profile}." + (f" {note}" if note else ""))
    from .context import Context
    ctx = Context(paths)
    if key and io.yes("Calibrate the fit cutoff now (label ~40 jobs, about 10 minutes)?", False):
        from .calibrate import run_calibration
        run_calibration(ctx, ask=lambda p: io.ask(p.rstrip(": ")), say=io.say)
    if sys.platform == "win32" and io.yes("Put a Cygnus icon on your desktop (PowerShell with "
                                          "`autoapply run --review --limit` typed in)?", True):
        from .shortcut import shortcut_command
        shortcut_command(paths, str(paths.root) if paths.root.resolve() != Paths.resolve(None).root.resolve()
                         else None)
    if mode != "manual" and sys.platform == "win32" and io.yes("Install the Windows scheduled task now?", True):
        from .scheduler_windows import schedule_command
        schedule_command("install", ctx, str(paths.root))
    from .config import ENV_HOME
    default_home = Paths.resolve(None).root
    flag = "" if paths.root.resolve() == default_home.resolve() else f' --profile-dir "{paths.root}"'
    io.say(f"\nNext: `autoapply{flag} health`, then a dry run with `autoapply{flag} run --test --limit 3`."
           + (f"\n(Or set {ENV_HOME}={paths.root} once and leave out --profile-dir.)" if flag else ""))
    return 0


# ------------------------------------------------------ one part at a time --
# `autoapply setup work limits` asks only those parts and changes only their settings;
# `autoapply setup new` asks the parts your profile.yaml does not have yet (after an update).
def step_speed(io: IO, cur: dict) -> dict:
    """How much happens at once: browser windows (each fills one form in front of it) and
    Claude scoring calls. Neither changes what goes into a form."""
    b, l = cur.get("browser") or {}, cur.get("llm") or {}
    io.say("\n== Speed ==\n  Simplify only works in a window's front tab, so each browser window fills one form\n"
           "  at a time. 2-3 windows fill 2-3 at once; keep them open, not minimised.")
    windows = ask_number(io, "Browser windows (1-4)", b.get("windows", 1), 1, 4)
    tabs = ask_number(io, "Forms open at once in all (parallel_tabs, 1-8)",
                      max(b.get("parallel_tabs", 3), windows), 1, 8)
    calls = ask_number(io, "Claude scoring calls at once (1-8)", l.get("parallel_calls", 4), 1, 8)
    return {"browser": {"windows": windows, "parallel_tabs": max(tabs, windows)},
            "llm": {"parallel_calls": calls}}


def _sec_work(paths, io, cur):
    w = step_work(io, cur.get("work"))
    return {"work": w} if w is not None else None


def _sec_workday(paths, io, cur):
    if not step_workday(paths, io):
        return None
    allowed = list((cur.get("filters") or {}).get("ats_allowed") or ["greenhouse", "ashby", "lever"])
    return {"filters": {"ats_allowed": allowed + ([] if "workday" in allowed else ["workday"])}}


def _sec_preferences(paths, io, cur):
    filters, visa = step_preferences(io, None)
    for k in ("ats_allowed", "per_company_cap"):     # not asked here: keep what you have
        filters.pop(k, None)
    return {"filters": filters, "visa": visa}


def _sec_sources(paths, io, cur):
    s = step_sources(paths, io)
    return {"sources": s} if s else None


# name: (the profile.yaml section it fills, what it asks, how)
SECTIONS: dict[str, tuple[str, str, Callable]] = {
    "work": ("work", "relocation, on-site/hybrid/remote, past employers",
             _sec_work),
    "limits": ("limits", "applications per company per run, automatic company pause",
               lambda paths, io, cur: {"limits": step_limits(io, cur.get("limits"))}),
    "mail": ("mail", "reading security codes and Workday emails from Gmail",
             lambda paths, io, cur: {"mail": {"enabled": step_mail(paths, io)}}),
    "discord": ("notifications", "Discord notifications",
                lambda paths, io, cur: {"notifications": {"discord": step_discord(paths, io)}}),
    "schedule": ("schedule", "when scheduled runs happen",
                 lambda paths, io, cur: {"schedule": step_schedule(io, cur.get("schedule"))}),
    "fit": ("fit", "the fit cutoff",
            lambda paths, io, cur: {"fit": step_fit(io, cur.get("fit"))}),
    "workday": ("", "your Workday login (and applying on Workday)", _sec_workday),
    "preferences": ("", "tiers, roles, location and work authorization", _sec_preferences),
    "sources": ("", "where jobs come from (job-monitor, CSV)", _sec_sources),
    "resumes": ("", "your resume files",
                lambda paths, io, cur: {"resumes": step_resumes(paths, io)}),
    "speed": ("", "browser windows and forms filled at once, Claude calls at once",
              lambda paths, io, cur: step_speed(io, cur)),
    "simplify": ("", "the Simplify extension",
                 lambda paths, io, cur: {"browser": {"simplify_consent": step_simplify(paths, io)}}),
}
NEW_ORDER = ("work", "limits", "mail", "discord", "schedule", "fit")   # the parts a new release may add


def sections_help() -> str:
    return "; ".join(f"{n}: {what}" for n, (_, what, _) in SECTIONS.items())


def update_profile(paths: Paths, partial: dict) -> str:
    """Merge `partial` into profile.yaml: only those settings change; the old file is backed up."""
    old = read_yaml(paths.profile) or {}
    merged = _merge(old, partial)
    try:
        Profile.model_validate(merged)
    except ValueError as e:
        raise ConfigError(f"those answers do not make a valid profile: {str(e).splitlines()[0]}") from e
    from datetime import datetime
    backup = paths.profile.with_name(f"profile.yaml.{datetime.now():%Y%m%d-%H%M%S}.bak")
    backup.write_text(paths.profile.read_text(encoding="utf-8-sig"), encoding="utf-8")
    paths.profile.write_text(PROFILE_HEADER + yaml.safe_dump(merged, sort_keys=False), encoding="utf-8")
    return f"The previous profile.yaml is saved as {backup.name}."


def run_sections(paths: Paths, names: list[str], io: IO | None = None) -> int:
    io = io or IO()
    if not paths.profile.exists():
        io.say(f"No profile at {paths.profile} yet - run `autoapply setup` (without a part) first.")
        return 1
    cur = read_yaml(paths.profile) or {}
    names = [n.lower() for n in names]
    if names == ["new"]:
        names = [n for n in NEW_ORDER if SECTIONS[n][0] not in cur]
        if not names:
            io.say("Your profile.yaml already has every part. `autoapply settings` shows all the values in use.")
            return 0
        io.say("New since your profile was written: " + ", ".join(f"{n} ({SECTIONS[n][1]})" for n in names))
    unknown = [n for n in names if n not in SECTIONS]
    if unknown:
        io.say(f"No part called {', '.join(unknown)}. The parts: {', '.join(SECTIONS)}, or `new`.")
        return 2
    partial: dict = {}
    for n in names:
        part = SECTIONS[n][2](paths, io, cur)
        if part:
            partial = _merge(partial, part)
    if not partial:
        io.say("Nothing to change.")
        return 0
    note = update_profile(paths, partial)
    io.say(f"\nSaved {', '.join(names)} to {paths.profile}; nothing else was changed. {note}")
    if "schedule" in names and sys.platform == "win32":
        io.say("Run `autoapply schedule install` to apply the new times.")
    return 0
