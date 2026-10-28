"""CHEATSHEET.md: every command it shows exists, with the options it shows."""
import re
import shlex
from pathlib import Path

import pytest

from autoapply.cli import build_parser, main

SHEET = Path(__file__).parent.parent / "CHEATSHEET.md"


def commands():
    text = SHEET.read_text(encoding="utf-8")
    for snippet in re.findall(r"`(autoapply [^`]*)`", text):
        if "<" in snippet or "…" in snippet:
            continue                              # placeholders such as <part>, <company>
        yield snippet


@pytest.mark.parametrize("snippet", list(commands()))
def test_every_command_on_the_sheet_parses(snippet):
    argv = shlex.split(snippet)[1:]
    if argv and argv[-1] in ("--help",):
        return
    build_parser().parse_args(argv)               # SystemExit on an unknown command or option


def test_every_setup_part_on_the_sheet_exists():
    from autoapply.setup_wizard import SECTIONS
    line = next(l for l in SHEET.read_text(encoding="utf-8").splitlines() if "autoapply setup <part>" in l)
    parts = re.findall(r"`(\w+)`", line.split(":", 1)[1])
    assert parts and set(parts) <= set(SECTIONS) and set(SECTIONS) <= set(parts)


def test_cheatsheet_command_prints_it(capsys):
    assert main(["cheatsheet"]) == 0
    assert "Cygnus cheat sheet" in capsys.readouterr().out
