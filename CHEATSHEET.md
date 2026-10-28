# Cygnus cheat sheet

Quick commands for the common situations. Run them in PowerShell, in this folder, with the venv on (the **Cygnus** desktop icon does that). `autoapply cheatsheet` prints this page; `autoapply <command> --help` lists every option of a command; the README explains the details.

## Every day

| I want to… | Type |
|---|---|
| Apply: fill N forms, then review them right away | `autoapply run --review --limit 10` |
| Only look at what's waiting (no new filling) | `autoapply review` |
| Try it without sending anything | `autoapply run --review --limit 3 --test` |
| See counts, spend, last run's time and cost | `autoapply status` |
| Update Cygnus (new features) | `git pull`, then `autoapply setup new` |

## Choosing companies

| I want to… | Type |
|---|---|
| Apply only to some companies this run | `autoapply run --review --company "capital one" --company nvidia` |
| Leave some companies out of this run | `autoapply run --review --skip-company "capital one, acme"` |
| Pause a company (its 30-day limit is reached) | `autoapply companies pause "capital one"` (add `--days 14` for another length) |
| End a pause early (also an automatic one) | `autoapply companies resume "capital one"` |
| See paused companies and the exact names to use | `autoapply companies` |
| Max applications per company per run / auto-pause after N in X days | `autoapply setup limits` |

## Settings

| I want to… | Type |
|---|---|
| See every setting and the value in use | `autoapply settings` |
| Change just one part of setup | `autoapply setup <part>`: `work`, `limits`, `speed`, `mail`, `discord`, `schedule`, `fit`, `workday`, `preferences`, `sources`, `resumes`, `simplify` |
| Answer only what a new version added | `autoapply setup new` |
| Relocation / on-site / hybrid / remote / past-employer answers | `autoapply setup work` |
| Faster runs (browser windows, Claude calls at once) | `autoapply setup speed` |
| Change the fit cutoff | `autoapply setup fit` (or `autoapply calibrate` to tune it on ~40 jobs) |
| Redo everything | `autoapply setup` |

## Answers

| I want to… | Type |
|---|---|
| See saved answers | `autoapply answers list` |
| Edit saved answers in Notepad | `autoapply answers edit` |

## When something is stuck

| Situation | Type |
|---|---|
| Fill Stuck applications again on the next run | `autoapply requeue --lane stuck` (or `needs_input`, `ready`, `all`) |
| After a test run: let test-approved jobs be applied for real | `autoapply requeue --lane tested` |
| Check that everything is set up right | `autoapply health` |
| Simplify logged out / its profile needs a fix | `autoapply browser` |
| Workday login, or which companies need an account | `autoapply workday` / `autoapply workday login` |
| Open a company's Workday sign-up page yourself | `autoapply workday open <company>` |
| A security code wasn't found in your email | `autoapply mail recent` |
| Set up or test reading codes from Gmail | `autoapply mail login` / `autoapply mail test` |
| Something crashed | look in `C:\Users\<you>\.autoapply\logs\` (the newest `…-crash.log`) |

## Running on a schedule, and the desktop icon

| I want to… | Type |
|---|---|
| Install / remove / check the Windows scheduled run | `autoapply schedule install` / `remove` / `show` |
| (Re)make the desktop icon | `autoapply shortcut` |
| An icon that types something else | `autoapply shortcut --name "Cygnus review" --command "autoapply review"` |

## Where things live

- Settings: `C:\Users\<you>\.autoapply\profile.yaml` (`autoapply settings` shows them all)
- Keys and passwords: `C:\Users\<you>\.autoapply\secrets.env` (never shared, never in git)
- Saved answers: `C:\Users\<you>\.autoapply\answers.yaml`
- Logs and crash reports: `C:\Users\<you>\.autoapply\logs\`
