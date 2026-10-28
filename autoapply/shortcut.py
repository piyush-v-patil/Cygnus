"""`autoapply shortcut`: a desktop icon that opens PowerShell ready to run.

Clicking it opens a PowerShell window in the folder you made it from, with this
virtual environment active, and `autoapply run --review --limit ` already typed
at the prompt: type a number and press Enter. Nothing runs until you do.

Two files: a small launcher script in the profile folder (start-autoapply.ps1,
safe to edit) and the shortcut on the desktop that opens it. The text is typed
by PSReadLine (part of Windows PowerShell 5.1 and PowerShell 7) once the prompt
is up; where PSReadLine is missing, the command is printed to copy instead.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .config import ENV_HOME, Paths

DEFAULT_COMMAND = "autoapply run --review --limit "
DEFAULT_NAME = "Cygnus"
LAUNCHER = "start-autoapply.ps1"
POWERSHELL = r"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"


def _q(s: str) -> str:
    """A PowerShell single-quoted string."""
    return "'" + str(s).replace("'", "''") + "'"


def venv_dir() -> Path | None:
    """The virtual environment this autoapply runs from, or None outside one."""
    return Path(sys.prefix) if sys.prefix != getattr(sys, "base_prefix", sys.prefix) else None


def launcher_script(command: str, workdir: str, venv: Path | None, profile_dir: str | None) -> str:
    lines = ["# Opened by the Cygnus desktop shortcut (`autoapply shortcut`). Safe to edit; run that",
             "# command again to rewrite it.",
             f"Set-Location -LiteralPath {_q(workdir)}"]
    if venv is not None:
        lines.append(f". {_q(venv / 'Scripts' / 'Activate.ps1')}")
    if profile_dir:
        lines.append(f"$env:{ENV_HOME} = {_q(profile_dir)}")
    lines += [
        "$host.UI.RawUI.WindowTitle = 'Cygnus'",
        "Write-Host 'Cygnus is ready. Type how many applications and press Enter.' -ForegroundColor Cyan",
        "Write-Host '(Other commands: autoapply cheatsheet)' -ForegroundColor DarkGray",
        "if (Get-Module -ListAvailable PSReadLine) {",
        # typed once the prompt is waiting; nothing is run until you press Enter
        "    $null = Register-EngineEvent -SourceIdentifier PowerShell.OnIdle -MaxTriggerCount 1 -Action {",
        f"        [Microsoft.PowerShell.PSConsoleReadLine]::Insert({_q(command)})",
        "    }",
        "} else {",
        f"    Write-Host ('Run: ' + {_q(command)} + '<number>')",
        "}",
    ]
    return "\r\n".join(lines) + "\r\n"


def shortcut_script(name: str, launcher: Path, workdir: str, command: str) -> str:
    """PowerShell that writes the .lnk on the desktop (OneDrive-redirected desktops too)."""
    args = f'-NoExit -NoLogo -ExecutionPolicy Bypass -File "{launcher}"'
    return "\n".join([
        f"$lnk = Join-Path ([Environment]::GetFolderPath('Desktop')) {_q(name + '.lnk')}",
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)",
        f"$s.TargetPath = [Environment]::ExpandEnvironmentVariables({_q(POWERSHELL)})",
        f"$s.Arguments = {_q(args)}",
        f"$s.WorkingDirectory = {_q(workdir)}",
        f"$s.Description = {_q('Cygnus: PowerShell with ' + command.strip() + ' typed in')}",
        "$s.IconLocation = $s.TargetPath + ',0'",
        "$s.Save()",
        "Write-Output ('Shortcut: ' + $lnk)",
    ])


def shortcut_command(paths: Paths, profile_dir: str | None, name: str = DEFAULT_NAME,
                     command: str = DEFAULT_COMMAND) -> int:
    workdir = os.getcwd()
    venv = venv_dir()
    if not command.endswith(" "):
        command += " "                               # the cursor waits after it, ready for the number
    launcher = paths.root / LAUNCHER
    # a profile chosen with --profile-dir rides along; the default one needs nothing
    home = str(paths.root) if profile_dir or os.environ.get(ENV_HOME) else None
    script = launcher_script(command, workdir, venv, home)
    lnk = shortcut_script(name, launcher, workdir, command)
    if sys.platform != "win32":
        print("Desktop shortcuts are made on Windows. What this command writes there:\n")
        print(f"--- {launcher}\n{script}\n--- the shortcut\n{lnk}")
        return 1
    launcher.write_text(script, encoding="utf-8-sig")       # BOM: Windows PowerShell reads it as UTF-8
    rc = subprocess.call(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", lnk])
    if rc == 0:
        print(f"Click {name} on your desktop: PowerShell opens in {workdir}"
              + (" with the virtual environment active" if venv else "")
              + f" and `{command.strip()}` typed in. Type a number, press Enter.")
        if venv is None:
            print("(No virtual environment: autoapply is run from wherever it is installed.)")
    return rc
