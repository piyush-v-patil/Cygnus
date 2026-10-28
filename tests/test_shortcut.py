"""`autoapply shortcut`: the launcher PowerShell runs and the desktop icon that opens it.

Only Windows can create the icon, so the scripts themselves are checked: the right
folder and venv, the command typed in but never run, and paths with spaces or
apostrophes quoted so PowerShell reads them as written.
"""
import sys
from pathlib import Path

from autoapply import shortcut
from autoapply.cli import main
from autoapply.config import Paths


def test_the_launcher_activates_the_venv_and_types_the_command_without_running_it():
    s = shortcut.launcher_script("autoapply run --review --limit ", r"C:\Users\Jane\Cygnus",
                                 Path(r"C:\Users\Jane\Cygnus\.venv"), None)
    assert r"Set-Location -LiteralPath 'C:\Users\Jane\Cygnus'" in s
    activate = next(line for line in s.splitlines() if line.endswith("Activate.ps1'"))
    assert activate.startswith(". 'C:\\Users\\Jane\\Cygnus\\.venv")     # dot-sourced: stays active
    assert "[Microsoft.PowerShell.PSConsoleReadLine]::Insert('autoapply run --review --limit ')" in s
    assert "-MaxTriggerCount 1" in s                       # typed once, not on every idle
    assert "\nautoapply run" not in s                      # never run by the script itself
    assert "AUTOAPPLY_HOME" not in s                       # the default profile needs nothing


def test_a_chosen_profile_folder_rides_along_and_odd_paths_are_quoted():
    s = shortcut.launcher_script("autoapply run --review --limit ", r"C:\Users\O'Neil\My Apps", None,
                                 r"D:\job hunt\profile")
    assert r"Set-Location -LiteralPath 'C:\Users\O''Neil\My Apps'" in s
    assert r"$env:AUTOAPPLY_HOME = 'D:\job hunt\profile'" in s
    assert "Activate.ps1" not in s                          # no venv: autoapply is already on PATH


def test_the_icon_opens_the_launcher_in_a_window_that_stays_open():
    s = shortcut.shortcut_script("Cygnus", Path(r"C:\Users\Jane\.autoapply\start-autoapply.ps1"),
                                 r"C:\Users\Jane\Cygnus", "autoapply run --review --limit ")
    assert "GetFolderPath('Desktop')" in s and "'Cygnus.lnk'" in s
    assert '-NoExit' in s and '-ExecutionPolicy Bypass' in s
    assert r'-File "C:\Users\Jane\.autoapply\start-autoapply.ps1"' in s
    assert r"$s.WorkingDirectory = 'C:\Users\Jane\Cygnus'" in s


def test_off_windows_it_shows_what_it_would_write(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.chdir(tmp_path)
    assert main(["--profile-dir", str(tmp_path / "p"), "shortcut", "--command", "autoapply run --limit"]) == 1
    out = capsys.readouterr().out
    assert "Insert('autoapply run --limit ')" in out         # a trailing space is added for the number
    assert not (tmp_path / "p" / shortcut.LAUNCHER).exists()


def test_on_windows_it_writes_the_launcher_and_asks_powershell_for_the_icon(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(shortcut.subprocess, "call", lambda argv: calls.append(argv) or 0)
    monkeypatch.chdir(tmp_path)
    paths = Paths(tmp_path / "p")
    paths.ensure()
    assert shortcut.shortcut_command(paths, None) == 0
    launcher = paths.root / shortcut.LAUNCHER
    assert launcher.read_bytes().startswith(b"\xef\xbb\xbf")        # BOM for Windows PowerShell
    assert "CreateShortcut" in calls[0][-1] and calls[0][0] == "powershell"
