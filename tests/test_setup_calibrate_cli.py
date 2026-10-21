"""Calibration, wizard, scheduler script, CLI lock and health (§5.2, §5.3, §13, §15, §16)."""
import os
from pathlib import Path

import pytest
from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas

from autoapply.calibrate import agreement, best_cutoff, run_calibration, set_cutoff_in_yaml, spread
from autoapply.cli import RunLock, main
from autoapply.config import Paths, load_profile
from autoapply.health import run_health
from autoapply.scheduler_windows import install_script
from autoapply.setup_wizard import IO, run_wizard

from .conftest import FIXTURES
from .test_pipeline_llm import clients


def test_spread_and_best_cutoff():
    items = [{"score": s} for s in range(0, 100, 2)]
    picked = spread(items, 10)
    assert len(picked) == 10 and picked[0]["score"] == 0 and picked[-1]["score"] == 98
    ex = [{"score": s, "label": "yes" if s >= 72 else "no"} for s in (40, 55, 60, 70, 74, 80, 90, 95)]
    cut, acc = best_cutoff(ex, 65)
    assert acc == 1.0 and 71 <= cut <= 74
    assert agreement(ex, 65) == 7 / 8


def test_set_cutoff_keeps_comments():
    text = "# my notes\nfit:\n  cutoff: 65   # tuned\n  order: newest_first\nllm:\n  x: 1\n"
    out = set_cutoff_in_yaml(text, 72)
    assert "cutoff: 72   # tuned" in out and "# my notes" in out


def test_calibration_run(make_ctx, transport, monkeypatch):
    import autoapply.ats as ats_pkg
    import autoapply.pipeline as pl
    cl, _ = clients()
    monkeypatch.setattr(ats_pkg, "clients", lambda http=None: cl)
    monkeypatch.setattr(pl.ats, "clients", lambda http=None: cl)
    scores = iter([20, 45, 60, 70, 75, 85, 90, 95, 30, 50, 65, 80] * 5)
    transport.responder = lambda p: {"score": next(scores), "role_family": "backend", "reasons": ["r"],
                                     "blockers": [], "yoe_required": None}
    ctx = make_ctx()
    # more eligible postings than the tiny fixture has: add some to the ledger directly
    for i in range(14):
        key = f"greenhouse:extra{i}:{i}"
        ctx.ledger.upsert_discovered(key, {"source_name": "x", "company": f"Co{i}", "title": f"Eng {i}"})
    answers = iter(["y great"] * 5 + ["n too senior"] * 5 + ["s"] * 10 + ["y"])
    out = []
    rc = run_calibration(ctx, count=10, ask=lambda p: next(answers), say=out.append)
    assert rc == 0
    data = ctx.paths.calibration.read_text()
    assert '"label": "yes"' in data and '"reason": "too senior"' in data
    assert any("Suggested cutoff" in line for line in out)
    # calibration scores without deciding: no posting is skipped or written back on its account
    assert "low_fit" not in str(ctx.ledger.reason_counts())
    assert ctx.ledger.db.execute("SELECT COUNT(*) FROM writeback_queue").fetchone()[0] == 0


def _pdf(path: Path, text: str):
    c = canvas.Canvas(str(path), pagesize=LETTER)
    y = 720
    for line in text.splitlines():
        c.drawString(72, y, line)
        y -= 16
    c.save()


@pytest.fixture(autouse=True)
def _no_env_secrets(monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "GITHUB_TOKEN", "DISCORD_WEBHOOK_URL"):
        monkeypatch.delenv(k, raising=False)


def test_wizard_writes_a_valid_profile(tmp_path, monkeypatch):
    pdf = tmp_path / "jane.pdf"
    _pdf(pdf, "Jane Doe\nSoftware Engineer\nPython, Go")
    csv = tmp_path / "links.txt"
    csv.write_text("https://job-boards.greenhouse.io/acme/jobs/1\n")
    monkeypatch.setattr("autoapply.setup_wizard.find_installed_simplify", lambda ext_id: None)
    script = iter([
        "y",                    # use this folder
        str(pdf), "", "",       # one resume, default for all, done
        "jane.pdf",             # default resume
        "",                     # no Anthropic key -> no draft
        "newgrad, experienced", "software, backend", "us_or_remote",
        "opt_stem_opt", "y", "skip_no_record", "y",
        "y",                    # confirm visa
        "n",                    # no job-monitor
        "y", str(csv),          # csv source
        "n",                    # no discord
        "n",                    # do not download Simplify
        "70", "manual",
    ])
    paths = Paths(tmp_path / "prof")
    said = []
    assert run_wizard(paths, IO(ask=lambda p: next(script), say=said.append)) == 0
    prof = load_profile(paths)
    assert prof.visa.confirmed and prof.visa.status == "opt_stem_opt" and prof.visa.needs_sponsorship_future
    assert prof.fit.cutoff == 70 and prof.resumes.default == "jane.pdf"
    assert prof.sources[0].type == "csv"
    assert (paths.resumes / "jane.txt").read_text().startswith("Jane Doe")


def test_wizard_does_not_save_unconfirmed_visa(tmp_path, monkeypatch):
    pdf = tmp_path / "r.pdf"
    _pdf(pdf, "Someone\nEngineer")
    monkeypatch.setattr("autoapply.setup_wizard.find_installed_simplify", lambda ext_id: None)
    script = iter(["y", str(pdf), "", "", "r.pdf", "",
                   "newgrad", "software", "us_or_remote", "citizen_gc", "n", "n",   # not confirmed
                   "newgrad", "software", "us_or_remote", "opt_stem_opt", "n", "off", "y", "y",
                   "n", "n", "n", "n", "65", "manual"])
    paths = Paths(tmp_path / "p")
    run_wizard(paths, IO(ask=lambda p: next(script), say=lambda s: None))
    prof = load_profile(paths)
    assert prof.visa.status == "opt_stem_opt" and prof.visa.confirmed


def test_task_scheduler_script():
    s = install_script(["08:00", "18:30"], r"C:\Python311\python.exe", r"C:\Users\me\.autoapply", 2)
    assert "-WakeToRun" in s and "-LogonType Interactive" in s and "New-TimeSpan -Hours 2" in s
    assert "-At '08:00'" in s and "-At '18:30'" in s
    assert "-m autoapply run --profile-dir" in s


def test_run_lock(tmp_path):
    lock = tmp_path / "x.lock"
    with RunLock(lock):
        with pytest.raises(SystemExit):
            with RunLock(lock):
                pass
    assert not lock.exists()
    lock.write_text("999999")               # stale lock from a crashed run
    with RunLock(lock):
        assert lock.read_text() == str(os.getpid())


def test_health_reports_missing_pieces(make_ctx):
    ctx = make_ctx(secrets={})
    ctx._llm_transport = None
    rep = run_health(ctx, probe_browser=False)
    names = {c.name: c.ok for c in rep.checks}
    assert names["profile"] and names["visa"] and names["resumes"]
    assert names["anthropic"] is False and not rep.ok
    assert "ANTHROPIC_API_KEY" in rep.first_failure()


def test_health_visa_unconfirmed(make_ctx):
    from .conftest import base_profile
    ctx = make_ctx(base_profile(visa={"confirmed": False}))
    rep = run_health(ctx, probe_browser=False)
    assert any(c.name == "visa" and not c.ok for c in rep.checks)


def test_cli_status_and_answers(profile_dir, capsys):
    assert main(["--profile-dir", str(profile_dir.root), "status"]) == 0
    assert "Spend this month" in capsys.readouterr().out
    assert main(["--profile-dir", str(profile_dir.root), "answers", "list"]) == 0
    assert "0 saved answers" in capsys.readouterr().out


def test_cli_bad_profile(tmp_path, capsys):
    assert main(["--profile-dir", str(tmp_path / "nothing"), "status"]) == 2
    assert "autoapply setup" in capsys.readouterr().err


def test_fixture_paths_exist():
    assert (FIXTURES / "jobs.json").exists() and (FIXTURES / "forms" / "greenhouse.html").exists()


def test_config_example_profile_is_valid():
    import yaml
    from autoapply.config import Profile
    root = Path(__file__).parent.parent / "config.example"
    Profile.model_validate(yaml.safe_load((root / "profile.yaml").read_text()))
    from autoapply.config import AnswerBank
    assert AnswerBank(root / "answers.yaml").items[0].kind == "yes_no"


def test_simplify_found_in_brave(tmp_path, monkeypatch):
    from autoapply import setup_wizard as w
    ext = tmp_path / ".config" / "BraveSoftware" / "Brave-Browser" / "Default" / "Extensions" / "abc"
    for v in ("3.1.9_0", "3.1.10_0"):
        (ext / v).mkdir(parents=True)
        (ext / v / "manifest.json").write_text('{"version": "%s"}' % v[:-2])
    monkeypatch.setattr(w.sys, "platform", "linux")
    monkeypatch.setattr(w.Path, "home", classmethod(lambda cls: tmp_path))
    browser, src = w.find_installed_simplify("abc")
    assert browser == "Brave" and src.name == "3.1.10_0"
    assert w.install_extension(src, tmp_path / "out") == "3.1.10"
