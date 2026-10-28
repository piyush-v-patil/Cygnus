"""`autoapply setup <part>`: ask one part, change only its settings, keep everything else."""
import yaml

from autoapply.cli import main
from autoapply.config import Paths, load_profile
from autoapply.setup_wizard import IO, run_sections


def script(*answers):
    it = iter(answers)
    said = []
    return IO(ask=lambda p: next(it), say=said.append), said


def test_one_part_changes_only_its_settings_and_backs_the_file_up(profile_dir):
    before = load_profile(profile_dir)
    io, _ = script("2", "3", "14")                     # per run, pause after 3, within 14 days
    assert run_sections(profile_dir, ["limits"], io) == 0
    after = load_profile(profile_dir)
    assert (after.limits.per_company_per_run, after.limits.company_max, after.limits.company_days) == (2, 3, 14)
    assert after.visa == before.visa and after.sources == before.sources and after.llm == before.llm
    assert list(profile_dir.root.glob("profile.yaml.*.bak"))


def test_new_asks_only_the_parts_your_profile_does_not_have(profile_dir):
    # the test profile has limits but no work, mail, notifications, schedule or fit
    io, said = script("y", "", "", "", "", "", "", "",   # work: yes, defaults (yes x5, assistance no, resume yes)
                      "n",                               # mail: no
                      "n",                               # discord: no
                      "manual",                          # schedule
                      "70")                              # fit
    assert run_sections(profile_dir, ["new"], io) == 0
    assert any(s.startswith("New since your profile was written: work") and "limits" not in s for s in said)
    p = load_profile(profile_dir)
    assert p.work.relocate and p.work.onsite and p.work.relocation_assistance is False
    assert p.fit.cutoff == 70 and p.schedule.mode == "manual" and p.mail.enabled is False
    io, said = script()
    assert run_sections(profile_dir, ["new"], io) == 0                  # nothing left to ask
    assert any("already has every part" in s for s in said)


def test_your_current_answers_are_the_defaults(profile_dir):
    run_sections(profile_dir, ["work"], script("y", "no", "", "ask", "", "", "", "n")[0])
    w = load_profile(profile_dir).work
    assert (w.relocate, w.anywhere_in_us, w.onsite, w.only_resume_employers) == (False, True, None, False)
    run_sections(profile_dir, ["work"], script("y", "", "", "", "", "", "", "")[0])   # Enter throughout
    assert load_profile(profile_dir).work == w


def test_preferences_keep_the_boards_you_apply_on(profile_dir):
    data = yaml.safe_load(profile_dir.profile.read_text())
    data["filters"]["ats_allowed"] = ["greenhouse", "ashby", "lever", "workday"]
    profile_dir.profile.write_text(yaml.safe_dump(data))
    io, _ = script("newgrad", "software", "us_or_remote", "opt_stem_opt", "y", "skip_no_record", "y", "y")
    assert run_sections(profile_dir, ["preferences"], io) == 0
    p = load_profile(profile_dir)
    assert "workday" in p.filters.ats_allowed and p.filters.tiers == ["newgrad"] and p.visa.confirmed


def test_unknown_parts_and_a_missing_profile(tmp_path, profile_dir):
    io, said = script()
    assert run_sections(profile_dir, ["nosuch"], io) == 2 and "The parts: work" in said[-1]
    assert run_sections(Paths(tmp_path / "none"), ["work"], script()[0]) == 1


def test_cli_setup_with_parts(profile_dir, monkeypatch, capsys):
    from autoapply import setup_wizard
    answers = iter(["1", "0"])
    monkeypatch.setattr(setup_wizard, "IO", lambda: IO(ask=lambda p: next(answers), say=print))
    assert main(["--profile-dir", str(profile_dir.root), "setup", "limits"]) == 0
    assert "Saved limits to" in capsys.readouterr().out
    assert load_profile(profile_dir).limits.company_max is None


def test_setup_speed_sets_windows_tabs_and_calls(profile_dir):
    assert run_sections(profile_dir, ["speed"], script("3", "2", "6")[0]) == 0
    b, l = load_profile(profile_dir).browser, load_profile(profile_dir).llm
    assert (b.windows, b.parallel_tabs, l.parallel_calls) == (3, 3, 6)   # tabs never fewer than windows
