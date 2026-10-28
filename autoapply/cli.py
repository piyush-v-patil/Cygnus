"""`autoapply` command line (§13)."""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from .config import ConfigError, Paths, load_secrets, redact

log = logging.getLogger("autoapply")


class LockHeld(SystemExit):
    """Another run (or a review session) holds the profile."""


class RunLock:
    """Only one run at a time (§16): an exclusive lock file in the profile folder."""

    def __init__(self, path: Path):
        self.path = path
        self.fd = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            pid = self.path.read_text(encoding="utf-8").strip() if self.path.exists() else "?"
            if pid.isdigit() and not _pid_alive(int(pid)):
                self.path.unlink(missing_ok=True)     # stale lock from a crashed run
                return self.__enter__()
            raise LockHeld(f"Another autoapply run is in progress (pid {pid}). "
                           f"If it is not, delete {self.path}.")
        os.write(self.fd, str(os.getpid()).encode())
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)
            self.path.unlink(missing_ok=True)


class _Held:
    """`with` for a RunLock that is already taken: releases it on the way out."""

    def __init__(self, lock: RunLock):
        self.lock = lock

    def __enter__(self):
        return self.lock

    def __exit__(self, *exc):
        self.lock.__exit__(*exc)


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class _Redact(logging.Filter):
    def __init__(self, secrets):
        super().__init__()
        self.secrets = secrets

    def filter(self, record):
        # format first: redacting the arguments one by one turned %d arguments into
        # strings and broke the line
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 - a bad format string still gets logged
            msg = f"{record.msg} {record.args}"
        record.msg, record.args = redact(msg, self.secrets), None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text, self.secrets)
        return True


def setup_logging(paths: Paths, verbose: bool, kind: str) -> Path:
    paths.logs.mkdir(parents=True, exist_ok=True)
    logfile = paths.logs / f"{datetime.now():%Y%m%d-%H%M%S}-{kind}.log"
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG)
    flt = _Redact(load_secrets(paths))
    fh = logging.FileHandler(logfile, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    fh.setLevel(logging.DEBUG)
    fh.addFilter(flt)
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("%(message)s"))
    sh.setLevel(logging.DEBUG if verbose else logging.INFO)
    sh.addFilter(flt)
    root.addHandler(fh)
    root.addHandler(sh)
    for noisy in ("urllib3", "httpx", "httpcore", "anthropic", "asyncio", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logfile


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="autoapply", description="Cygnus / AutoApply")
    p.add_argument("--profile-dir", help="profile folder (default ~/.autoapply or $AUTOAPPLY_HOME)")
    p.add_argument("--verbose", "-v", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("setup", help="interactive setup wizard; or only some parts: setup work limits / setup new")
    st.add_argument("parts", nargs="*", metavar="PART",
                    help="ask only these parts and change only their settings; `new` = the parts your "
                         "profile.yaml does not have yet. Parts: work, limits, mail, discord, schedule, fit, "
                         "workday, preferences, sources, resumes, speed, simplify")
    c = sub.add_parser("calibrate", help="label sample jobs to tune the fit cutoff")
    c.add_argument("--rescore", action="store_true")
    c.add_argument("--count", type=int, default=40)
    sub.add_parser("health", help="run the health check only")
    r = sub.add_parser("run", help="sync, filter, score, fill and queue for review")
    r.add_argument("--limit", type=int, help="process at most N postings past the prefilter")
    r.add_argument("--no-fill", action="store_true", help="stop after scoring")
    r.add_argument("--test", action="store_true", help="never click Submit in this run")
    r.add_argument("--review", action="store_true",
                   help="keep the filled forms open and review them right away in the same session")
    r.add_argument("--port", type=int, help="review page port (with --review)")
    r.add_argument("--no-browser", action="store_true", help="with --review: do not open the review page")
    r.add_argument("--company", action="append", metavar="NAME",
                   help="work only on this company this run (repeat, or comma-separated)")
    r.add_argument("--skip-company", action="append", metavar="NAME",
                   help="leave this company out of this run (repeat, or comma-separated)")
    rv = sub.add_parser("review", help="open the review UI")
    rv.add_argument("--port", type=int)
    rv.add_argument("--no-browser", action="store_true", help="do not open the UI in a web browser")
    rv.add_argument("--test", action="store_true", help="never click Submit in this session")
    sub.add_parser("status", help="counts per state, spend, last run")
    sub.add_parser("settings", help="every setting with the value in use, defaults included")
    rq = sub.add_parser("requeue", help="send queued applications back to be filled again on the next run")
    rq.add_argument("--lane", choices=["ready", "needs_input", "stuck", "all", "tested"], default="all",
                    help="all = ready + needs_input + stuck; tested = approved during --test runs (never sent)")
    b = sub.add_parser("browser", help="open the tool's browser (Simplify login, consent, profile)")
    b.add_argument("--url", help="page to open (default: the Simplify login page)")
    sc = sub.add_parser("shortcut", help="desktop icon: PowerShell in this venv with the run command typed in")
    sc.add_argument("--name", default="Cygnus", help="the icon's name (default Cygnus)")
    sc.add_argument("--command", default="autoapply run --review --limit ",
                    help='what is typed at the prompt (default "autoapply run --review --limit ")')
    s = sub.add_parser("schedule", help="Windows Task Scheduler")
    s.add_argument("action", choices=["install", "remove", "show"])
    a = sub.add_parser("answers", help="the answer bank")
    a.add_argument("action", choices=["list", "edit"])
    w = sub.add_parser("workday", help="Workday accounts: the login the tool uses, and which companies need one")
    w.add_argument("action", nargs="?", choices=["list", "login", "open"], default="list")
    w.add_argument("company", nargs="?", help="with open: the company (tenant) whose sign-up page to open")
    co = sub.add_parser("companies", help="pause a company (30-day limit reached), resume it, see names")
    co.add_argument("action", nargs="?", choices=["list", "pause", "resume"], default="list")
    co.add_argument("name", nargs="?", help="the company, as in `autoapply companies`")
    co.add_argument("--days", type=int, default=30, help="with pause: how many days (default 30)")
    m = sub.add_parser("mail", help="the mailbox security codes and Workday links are read from")
    m.add_argument("action", nargs="?", choices=["test", "login", "recent"], default="test")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = Paths.resolve(args.profile_dir)
    try:
        return _dispatch(args, paths)
    except ConfigError as e:
        print(f"Configuration problem: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except Exception as e:  # noqa: BLE001 - logged, kept in a crash file, and alerted
        return _crashed(paths, args.cmd, e)


def _crashed(paths: Paths, cmd: str, e: Exception) -> int:
    import traceback
    tb = "".join(traceback.format_exception(e))
    try:
        secrets = load_secrets(paths)
    except Exception:  # noqa: BLE001
        secrets = {}
    log.error("autoapply %s crashed: %s: %s", cmd, type(e).__name__, e, exc_info=e)
    crash = None
    try:
        paths.logs.mkdir(parents=True, exist_ok=True)
        crash = paths.logs / f"{datetime.now():%Y%m%d-%H%M%S}-crash.log"
        crash.write_text(redact(tb, secrets), encoding="utf-8")
    except OSError:
        pass
    print(f"autoapply {cmd} stopped on an unexpected error: {type(e).__name__}: {redact(str(e), secrets)}"
          + (f"\nDetails: {crash}" if crash else ""), file=sys.stderr)
    _alert(paths, f"`autoapply {cmd}` crashed: {type(e).__name__}: {redact(str(e), secrets)[:300]}")
    return 1


def _alert(paths: Paths, message: str) -> None:
    """Best effort: a Discord alert when the profile can still be read."""
    try:
        from .context import Context
        Context(paths).notifier.alert(message)
    except Exception:  # noqa: BLE001
        pass


def _dispatch(args, paths: Paths) -> int:
    from .context import Context

    if args.cmd == "setup":
        from .setup_wizard import run_sections, run_wizard
        return run_sections(paths, args.parts) if args.parts else run_wizard(paths)

    if args.cmd == "answers":
        from .config import AnswerBank
        bank = AnswerBank(paths.answers)
        if args.action == "list":
            for a in bank.items:
                print(f"[{a.kind}] {a.original}\n    -> {a.answer}   ({a.source}, {a.added})")
            print(f"{len(bank.items)} saved answers in {paths.answers}")
        else:
            if not paths.answers.exists():
                bank.save()
            _open_editor(paths.answers)
        return 0

    if args.cmd == "shortcut":
        from .shortcut import shortcut_command
        return shortcut_command(paths, args.profile_dir, args.name, args.command)

    if args.cmd == "schedule":
        from .scheduler_windows import schedule_command
        ctx = Context(paths)
        return schedule_command(args.action, ctx, args.profile_dir)

    if args.cmd == "browser":
        from .browser.simplify import extension_version
        from .setup_wizard import IO, login_browser
        if extension_version(paths.extension) is None:
            print(f"Simplify is not installed at {paths.extension}. Run `autoapply setup` first.", file=sys.stderr)
            return 1
        try:
            bcfg = Context(paths).profile.browser
            exe, consent = bcfg.executable_path, bcfg.simplify_consent
        except ConfigError:
            exe, consent = None, "essentials"
        with RunLock(paths.lock):          # one browser on this profile at a time
            login_browser(paths, IO(), url=args.url, executable_path=exe, consent=consent)
        return 0

    if args.cmd == "workday":
        from .workday_cmd import workday_command
        return workday_command(Context(paths), args.action, args.company)

    if args.cmd == "companies":
        from .companies_cmd import companies_command
        return companies_command(Context(paths), args.action, args.name, args.days)

    if args.cmd == "mail":
        from .mail_cmd import mail_command
        return mail_command(Context(paths), args.action)

    if args.cmd == "requeue":
        lanes = {"all": ["ready", "needs_input", "stuck"], "tested": ["would_submit"]}.get(args.lane, [args.lane])
        with RunLock(paths.lock):
            ctx = Context(paths)
            keys = ctx.ledger.requeue(lanes)
            from .browser.workday import clear_accounts
            clear_accounts(ctx, keys)         # a failed Workday sign-in gets another try
        print(f"{len(keys)} application(s) will be filled again on the next `autoapply run`."
              " Your saved answers are kept.")
        return 0

    if args.cmd == "settings":
        from .settings_cmd import settings_text
        print(settings_text(paths))
        return 0

    if args.cmd == "status":
        from .pipeline import status_report
        print(status_report(Context(paths)))
        return 0

    test_mode = bool(getattr(args, "test", False))
    ctx = Context(paths, test_mode=test_mode)
    logfile = setup_logging(paths, args.verbose, args.cmd)
    log.debug("log file %s", logfile)

    if args.cmd == "health":
        from .health import run_health
        rep = run_health(ctx, probe_browser=True)
        print(rep.render())
        return 0 if rep.ok else 1

    if args.cmd == "calibrate":
        from .calibrate import run_calibration
        with RunLock(paths.lock):
            return run_calibration(ctx, count=args.count, rescore=args.rescore)

    if args.cmd == "run":
        from .health import run_health
        from .pipeline import run
        lock = RunLock(paths.lock)
        try:
            lock.__enter__()
        except LockHeld as e:
            if not sys.stdin or not sys.stdin.isatty():    # a scheduled run: nobody sees the console
                ctx.notifier.warn(f"a scheduled run was skipped: {e} (a review session left open?)")
            raise
        with _Held(lock):
            rep = run_health(ctx, probe_browser=not args.no_fill)
            if not rep.ok:
                print(rep.render(), file=sys.stderr)
                ctx.notifier.alert(rep.first_failure())
                return 1
            for w in rep.warnings:
                print(f"Note: {w}", file=sys.stderr)
            _choose_companies(ctx, args)
            if args.review and not args.no_fill:
                from .review.server import run_and_review
                return run_and_review(ctx, limit=args.limit, port=args.port or ctx.profile.review.port,
                                      open_browser=not args.no_browser)
            counts = run(ctx, limit=args.limit, no_fill=args.no_fill)
        print(_summary(counts))
        from .pipeline import cost_line, time_line
        if counts.get("timing"):
            print(time_line(counts["timing"]))
        last = ctx.ledger.last_run()
        if last:
            print(cost_line(ctx.ledger.run_spend(last)))
        from .pipeline import status_report
        print(status_report(ctx))
        print(_next_step(ctx, test=args.test))
        return 0

    if args.cmd == "review":
        from .health import run_health
        from .review.server import serve
        with RunLock(paths.lock):
            rep = run_health(ctx, probe_browser=True, review=True)
            if not rep.ok:
                print(rep.render(), file=sys.stderr)
                ctx.notifier.alert(rep.first_failure())
                return 1
            for w in rep.warnings:
                print(f"Note: {w}", file=sys.stderr)
            return serve(ctx, port=args.port or ctx.profile.review.port, open_browser=not args.no_browser)
    return 1


def _choose_companies(ctx, args) -> None:
    """This run's --company / --skip-company, with a note on what they cover."""
    from .companies import matches, split_names
    only, skip = split_names(getattr(args, "company", None)), split_names(getattr(args, "skip_company", None))
    if not only and not skip:
        return
    ctx.run_companies = (only, skip)
    names = {r.get("company") for r in ctx.ledger.postings_in(
        ["discovered", "enriched", "scored", "held", "error"]) if r.get("company")}
    for w in only + skip:
        if not any(matches(c, w) for c in names):
            print(f"Note: no postings waiting for {w!r} (see `autoapply companies` for the names)", file=sys.stderr)
    if only:
        hit = sorted({c for c in names if any(matches(c, w) for w in only)})
        print(f"This run: only {', '.join(hit) or ', '.join(only)}")
    if skip:
        print(f"This run leaves out: {', '.join(skip)}")


def _next_step(ctx, test: bool) -> str:
    """The run never asks for confirmation: approving happens in the review queue."""
    led = ctx.ledger
    n = {lane: len(led.applications_in_lane(lane)) for lane in ("ready", "needs_input", "stuck")}
    errors = led.counts().get("error", 0)
    retry = (f"\n{errors} application(s) hit an error and will be retried on the next run "
             "(`autoapply status` lists why)." if errors else "")
    if not any(n.values()):
        return "Nothing is waiting for review." + retry
    cmd = "autoapply review --test" if test or ctx.never_submit else "autoapply review"
    return (f"\nWaiting for you: {n['ready']} ready, {n['needs_input']} need your answers, {n['stuck']} stuck.\n"
            f"Nothing has been submitted. Next: run `{cmd}` to check each application and approve it."
            + retry)


def _summary(counts: dict) -> str:
    parts = [f"{k}={v}" for k, v in sorted(counts.items()) if k not in ("skipped_by_reason", "timing") and v]
    sk = counts.get("skipped_by_reason") or {}
    if sk:
        parts.append("skipped: " + ", ".join(f"{k}={v}" for k, v in sorted(sk.items())))
    return "This run: " + (" · ".join(parts) or "nothing to do")


def _open_editor(path: Path):
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606 - opens in the user's default editor
        return
    import subprocess
    editor = os.environ.get("EDITOR") or ("open" if sys.platform == "darwin" else "nano")
    subprocess.call([editor, str(path)])


if __name__ == "__main__":
    sys.exit(main())
