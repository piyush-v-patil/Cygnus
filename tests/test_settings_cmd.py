"""`autoapply settings`: the values in use, with the ones not in profile.yaml marked."""
import yaml

from autoapply.cli import main
from autoapply.config import Paths, Profile, load_profile
from autoapply.settings_cmd import settings_text


def test_every_setting_is_shown_and_defaults_are_marked(profile_dir):
    text = settings_text(profile_dir)
    lines = text.splitlines()
    assert any(l.startswith("work:") and l.endswith("# default") for l in lines)      # not in the file
    assert any(l.strip().startswith("per_company_per_run:") for l in lines)
    visa = next(l for l in lines if l.startswith("visa:"))
    assert "# default" not in visa                                                     # yours
    status = next(l for l in lines if l.strip().startswith("status:"))
    assert "opt_stem_opt" in status and "# default" not in status
    budget = next(l for l in lines if l.strip().startswith("monthly_budget_usd:"))
    assert "# default" not in budget                                                   # set in the test profile
    tabs = next(l for l in lines if l.strip().startswith("keep_tabs_max:"))
    assert tabs.endswith("# default")


def test_the_output_is_yaml_that_reads_back_as_the_same_settings(profile_dir):
    shown = yaml.safe_load(settings_text(profile_dir))
    assert Profile.model_validate(shown).model_dump() == load_profile(profile_dir).model_dump()


def test_the_example_profile_is_complete_and_valid():
    from pathlib import Path
    example = yaml.safe_load((Path(__file__).parent.parent / "config.example" / "profile.yaml").read_text())
    assert set(example) == set(Profile.model_fields)                                  # no section left out
    Profile.model_validate(example)


def test_cli_settings(profile_dir, capsys):
    assert main(["--profile-dir", str(profile_dir.root), "settings"]) == 0
    assert "# Settings in use, from" in capsys.readouterr().out


def test_cli_settings_without_a_profile(tmp_path, capsys):
    assert main(["--profile-dir", str(tmp_path / "none"), "settings"]) == 2
    assert "Run `autoapply setup` first" in capsys.readouterr().err
