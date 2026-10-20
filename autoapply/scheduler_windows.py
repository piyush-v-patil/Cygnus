"""Windows Task Scheduler integration (§16).

Registers `autoapply run` at schedule.times with "Wake the computer to run this
task", run-only-when-logged-on (the browser window is visible) and a time
limit. A missed run (PC off) is not caught up: the ledger carries everything
to the next run.
"""
from __future__ import annotations

import subprocess
import sys

from .context import Context

TASK_NAME = "Cygnus AutoApply"


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def install_script(times: list[str], python: str, profile_dir: str | None, hours: int) -> str:
    args = "-m autoapply run"
    if profile_dir:
        args += f' --profile-dir "{profile_dir}"'
    triggers = ", ".join(f"(New-ScheduledTaskTrigger -Daily -At {_ps_quote(t)})" for t in times)
    return "\n".join([
        f"$action = New-ScheduledTaskAction -Execute {_ps_quote(python)} -Argument {_ps_quote(args)}",
        f"$triggers = @({triggers})",
        f"$settings = New-ScheduledTaskSettingsSet -WakeToRun -ExecutionTimeLimit (New-TimeSpan -Hours {hours})"
        " -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries",
        '$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\\$env:USERNAME" -LogonType Interactive',
        f"Register-ScheduledTask -TaskName {_ps_quote(TASK_NAME)} -Action $action -Trigger $triggers "
        "-Settings $settings -Principal $principal -Force | Out-Null",
        f"Write-Output 'Registered {TASK_NAME}'",
    ])


def remove_script() -> str:
    return (f"Unregister-ScheduledTask -TaskName {_ps_quote(TASK_NAME)} -Confirm:$false -ErrorAction SilentlyContinue; "
            f"Write-Output 'Removed {TASK_NAME}'")


def show_script() -> str:
    return (f"$t = Get-ScheduledTask -TaskName {_ps_quote(TASK_NAME)} -ErrorAction SilentlyContinue; "
            "if (-not $t) { Write-Output 'Not installed' } else { "
            "$i = $t | Get-ScheduledTaskInfo; "
            "Write-Output ('State: ' + $t.State); Write-Output ('Next run: ' + $i.NextRunTime); "
            "Write-Output ('Last run: ' + $i.LastRunTime + ' (result ' + $i.LastTaskResult + ')'); "
            "Write-Output ('Wake to run: ' + $t.Settings.WakeToRun) }")


def _powershell(script: str) -> int:
    return subprocess.call(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script])


def schedule_command(action: str, ctx: Context, profile_dir: str | None) -> int:
    sched = ctx.profile.schedule
    if action == "install" and sched.mode == "manual":
        print("schedule.mode is manual - nothing to install. Set daily or twice_daily in profile.yaml.")
        return 0
    if action == "install":
        script = install_script(sched.times, sys.executable, profile_dir or str(ctx.paths.root),
                                sched.time_limit_hours)
    elif action == "remove":
        script = remove_script()
    else:
        script = show_script()
    if sys.platform != "win32":
        print("Task Scheduler is Windows-only. On this system, schedule `autoapply run` with cron/launchd.\n"
              "The PowerShell this command would run on Windows:\n")
        print(script)
        return 0 if action == "show" else 1
    return _powershell(script)
