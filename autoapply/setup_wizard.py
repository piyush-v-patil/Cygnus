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

from .config import ConfigError, Paths, Profile, load_secrets, save_secret
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


def step_sources(paths: Paths, io: IO) -> list[dict]:
    from .config import SourceCfg
    from .sources import CsvSource, JobMonitorSource
    io.say("\n== Job sources ==")
    sources = []
    if io.yes("Use your job-monitor fork on GitHub?", True):
        repo = io.ask("job-monitor repo (owner/repo)", "Piyush-v-patil/job-monitor")
        branch = io.ask("Branch", "main")
        wb = io.yes("Mark applied/skipped jobs back in job-monitor (needs a GitHub token with Contents: write)?", True)
        token = load_secrets(paths).get("GITHUB_TOKEN")
        if wb and not token:
            token = io.ask("GitHub fine-grained token (Contents: read and write on the fork; empty to skip)")
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
    url = io.ask("Discord webhook URL (empty to skip)")
    if not url:
        return False
    from .notify import Discord
    Discord(url).send("✅ Cygnus is connected. You will get Needs-input pings and run summaries here.")
    if io.yes("Did a test message arrive in Discord?", True):
        save_secret(paths, "DISCORD_WEBHOOK_URL", url)
        return True
    io.say("  Not saved - check the webhook URL and rerun setup.")
    return False


def ensure_anthropic_key(paths: Paths, io: IO) -> str | None:
    key = load_secrets(paths).get("ANTHROPIC_API_KEY")       # file or environment; never re-saved
    while not key:
        key = io.ask("Anthropic API key (sk-ant-...; empty to skip)")
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


def step_simplify(paths: Paths, io: IO) -> None:
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
            return
    except Exception as e:  # noqa: BLE001
        io.say(f"  Could not install the extension: {e}")
        return
    if io.yes("Open the tool's browser now to log in to Simplify?", True):
        login_browser(paths, io)


def login_browser(paths: Paths, io: IO) -> None:
    import asyncio
    from .browser.simplify import LOGIN_URL

    async def go():
        from playwright.async_api import async_playwright
        ext = str(paths.extension)
        async with async_playwright() as pw:
            ctx = await pw.chromium.launch_persistent_context(
                str(paths.browser_profile), headless=False, no_viewport=True,
                args=[f"--disable-extensions-except={ext}", f"--load-extension={ext}"])
            page = await ctx.new_page()
            await page.goto(LOGIN_URL)
            io.say("  In the browser window: log in to Simplify, then open any job application page,\n"
                   "  click Accept on Simplify's 'Before you autofill' card, and complete your Simplify\n"
                   "  profile there (work authorization, sponsorship, demographics, salary, education,\n"
                   "  experience). Simplify's answers are what most forms get.")
            await asyncio.to_thread(io.ask, "  Press Enter here when you are done")
            await ctx.close()
    asyncio.run(go())


def write_profile(paths: Paths, data: dict) -> None:
    Profile.model_validate(data)            # fail here, not on the first run
    paths.profile.write_text(PROFILE_HEADER + yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


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
    sources = step_sources(paths, io)
    discord = step_discord(paths, io)
    step_simplify(paths, io)
    io.say("\n== Fit ==")
    cutoff = int(io.ask("Fit cutoff 0-100 (postings scoring lower are skipped)", "65"))
    mode = io.choose("Schedule", ["manual", "daily", "twice_daily"], "daily")
    times = ["08:00"] if mode != "twice_daily" else ["08:00", "18:00"]
    if mode != "manual":
        times = [t.strip() for t in io.ask("Run time(s), local HH:MM", ", ".join(times)).split(",")]
    data = {
        "sources": sources or [{"type": "csv", "path": str(paths.root / "jobs.csv")}],
        "filters": filters, "visa": visa, "fit": {"cutoff": cutoff, "order": "newest_first"},
        "resumes": resumes, "browser": {"parallel_tabs": 3, "headless": False},
        "llm": {"score_model": "claude-haiku-5-5", "write_model": "claude-sonnet-5-5", "monthly_budget_usd": 20},
        "schedule": {"mode": mode, "times": times},
        "notifications": {"discord": discord,
                          "needs_input_ping": "immediate", "ping_group_threshold": 5},
        "testing": {"never_submit": False},
    }
    write_profile(paths, data)
    if not paths.answers.exists():
        paths.answers.write_text("[]\n", encoding="utf-8")
    io.say(f"\nWrote {paths.profile}.")
    from .context import Context
    ctx = Context(paths)
    if key and io.yes("Calibrate the fit cutoff now (label ~40 jobs, about 10 minutes)?", False):
        from .calibrate import run_calibration
        run_calibration(ctx, ask=lambda p: io.ask(p.rstrip(": ")), say=io.say)
    if mode != "manual" and sys.platform == "win32" and io.yes("Install the Windows scheduled task now?", True):
        from .scheduler_windows import schedule_command
        schedule_command("install", ctx, str(paths.root))
    io.say("\nNext: `autoapply health`, then a dry run with `autoapply run --test --limit 3`.")
    return 0
