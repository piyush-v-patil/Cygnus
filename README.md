# Cygnus (AutoApply)

Cygnus applies to software jobs for you, and you approve every application before it goes out.

1. It reads postings from your [job-monitor](https://github.com/Piyush-v-patil/job-monitor) tracker, or from a CSV of links.
2. It drops postings with cheap rules first (tier, role, US location, H-1B record, blocker phrases). Claude then scores the rest for fit.
3. For each good fit, it opens the Greenhouse / Ashby / Lever form in a Chromium window that has **Simplify Copilot** loaded. Simplify autofills the form, the tool swaps in the right resume, and Claude fills what Simplify left blank.
4. Every filled application goes into a local **review queue**. Nothing is submitted until you click *Approve & submit*.
5. Every posting is recorded in a ledger, so nothing is applied to twice. A copy of each submitted application is saved, and the posting is marked `applied` or `skip` back in job-monitor.

Everything runs on your PC. The repository contains no personal data. Your resumes, answers, keys and history live in a private profile folder, `%USERPROFILE%\.autoapply\` by default.

> **Rules the tool follows.** It never makes up a fact. Claude may write prose answers from your resume, but a factual answer (yes/no, numbers, dates, eligibility) must come from Simplify, your saved answers, or a quote from your resume. If none of those has it, the application waits in *Needs input*. Values Simplify fills are never overwritten, except the resume file. CAPTCHAs and login walls are never bypassed: those applications go to *Stuck*.

---

## Setup (Windows)

You need Python 3.11+, Chrome, Brave or Edge with **Simplify Copilot** installed, an Anthropic API key, and (for write-back) a GitHub fine-grained token for your job-monitor fork with *Contents: read and write*.

```powershell
git clone https://github.com/piyush-v-patil/cygnus.git
cd cygnus
py -m venv .venv
.venv\Scripts\activate
pip install -e .
playwright install chromium          # Playwright's own Chromium; branded Chrome cannot side-load extensions
autoapply setup
```

`autoapply setup` walks you through:

- **Resumes.** Pick your PDFs and the role family each one is for (`backend`, `ml-ai`, …). One is the default.
- **Preferences.** Claude drafts your tiers, roles and location from your resume, and you accept or edit each value.
- **Work authorization.** Your visa answers are **never saved until you confirm them**. They decide which postings are skipped, and they are checked against what Simplify fills on every form.
- **Sources.** Your job-monitor fork (with the GitHub token) and/or a CSV.
- **Discord.** A webhook, checked with a test message.
- **Simplify.** The wizard copies the extension from your Chrome, Brave or Edge profile (or downloads it from the Chrome Web Store). It asks once how to answer Simplify's "Before you autofill" data-consent card (Simplify asks again on every browser start, so the tool answers it for you each time from `browser.simplify_consent`). It then opens the tool's browser so you can log in to Simplify **once** and finish your Simplify profile: work authorization, sponsorship, demographics, salary, education and experience. Most form answers come from Simplify.
- **Fit cutoff.** Default 65, with an optional calibration run.
- **Schedule.** Optional; see below.

Before the first run:

- **Sync your manual Applied marks.** Marks you made on the job-monitor dashboard stay in your browser until you add a GitHub token in the dashboard's ⚙ settings and they sync to `jobs.json`. Until then, the tool cannot see them and may re-attempt jobs you already applied to by hand.
- Run `autoapply health`.
- Try a dry run: `autoapply run --test --limit 3`. It fills real forms but never clicks Submit.

## Daily use

```
autoapply run [--limit N] [--no-fill] [--test]   # health → sync → filter → score → fill → queue → notify
autoapply run --review [--limit N] [--test]      # same, then review right away with the filled forms still open
autoapply review                                 # open the review queue + the tool's browser
autoapply status                                 # counts per state, spend this month, last run
autoapply calibrate [--rescore]                  # label ~40 jobs blind; suggests a cutoff
autoapply answers list|edit                      # your answer bank
autoapply requeue [--lane stuck|needs_input|ready|all]  # fill queued applications again next run
autoapply requeue --lane tested                  # after testing: let test-approved jobs be applied for real
autoapply browser [--url URL]                    # open the tool's browser (Simplify login, profile)
autoapply schedule install|remove|show           # Windows Task Scheduler
autoapply health                                 # the pre-run checks on their own
```

Global options: `--profile-dir PATH` (or `AUTOAPPLY_HOME`) and `--verbose`. Each run writes a log file to `logs\`.

### The review queue

`autoapply review` opens `http://127.0.0.1:8765` (localhost only) and the tool's browser.

| Lane | What you do |
|---|---|
| **Ready** | Check the fields. Each one is labelled with its source: Simplify, Saved answer, Claude (with the resume evidence or facts it used), or You. **Open form to check** shows the real application in the tool's browser, where you can change anything. Then **Approve & submit** (it sends the form as it stands) or **Reject**. *Approve all visible* still submits them one at a time. |
| **Needs input** | Questions about you that Simplify, your saved answers and Claude may not answer (work authorization, salary, start date, consent...). Answer them on the card and click **Save & open the form to check**, or click **Open form**, answer in the form itself and click **I answered in the form**. Either way the answers are saved to `answers.yaml`, complete any other queued application that asks the same question, and the application moves to Ready. Answering never submits: you approve from Ready after checking the form. |
| **Stuck** | A CAPTCHA, login wall, Simplify failure, failed upload, or a form that changed. **Open form** brings the tab up, or reopens it and refills it from the plan. Finish it by hand, then click **I submitted it** (a screenshot is recorded) or **Give up**. |
| **History** | Every submitted application, with its saved copy. |

**Review the live forms (`autoapply run --review`).** The run keeps each filled form open in the tool's browser (up to `browser.keep_tabs_max`, default 15) and then opens the review queue in the same session; cards with an open form show **open tab**. Approving submits that tab as it stands, so there is no refill and you can switch to the tab and change anything by hand first. What you send is what is on the page, and the saved copy records it. Answers you give in *Needs input* are typed straight into the open tab (and into any other open tab asking the same question). A tab that is gone, older than `browser.tab_max_age_min` (default 4 h), or now has an empty required field is refilled as below. Hand edits made in a tab are not added to the answer bank. Scheduled runs keep the plain `autoapply run`, which closes each form after filling.

Otherwise approval **replays** the form, because filled tabs don't survive hours of waiting. Replay reopens the page, runs Simplify again, and re-applies your saved answers and Claude's answers without new Claude calls. It then checks that every planned value is present before clicking Submit. If anything differs, the application goes to Stuck with the reason.

Each submission is saved under `submissions\<date>_<company>_<title>_<id>\`. The folder holds `form.png`, `after_submit.png`, `fields.json`, the resume and any cover letter, `job.txt` and `meta.json`.

### Write-back to job-monitor

When `writeback: true`, a submitted application sets `status: applied`, `applied_on` and `applied_via: autoapply` on that job in your fork's `jobs.json`. Skips after scoring, regex/Claude blockers and *Reject* set `status: skip` with `skip_reason` (`low_fit`, `us_person_required`, `no_sponsorship`, `yoe`, `blocker_other`, `user_rejected`) and `skipped_by: autoapply`.

Only these short codes and dates are written, because the repository is public. There is one commit per run or review session, plus a flush every 25 changes. Failed writes are retried next time.

Postings scored below `fit.cutoff` are **held**, not skipped (`fit.low_fit: hold`, the default): job-monitor still shows them as new, and every run re-checks them against the current cutoff from their stored score, for free, so lowering the cutoff (or adding a resume that fits them better) brings them back. `fit.low_fit: skip` drops them for good and writes `skip / low_fit`. Switching back to `hold` releases earlier low-fit skips and undoes the `skip` marks Cygnus wrote for them (never a status you set yourself).

Narrowing `filters:` (for example `roles: [software]`) takes effect on the next run for postings already scored too: they are left where they are, not scored or filled, and come back if you widen the filters again.

Three things are not written back:

- `no_h1b_record` (rule-stage skips are re-checked on every run, and job-monitor's README notes a `null` there can be a data gap).
- Rule-filtered postings.
- Needs input / Stuck / error.

### Notifications (Discord)

- An immediate ping for each Needs-input application, listing the missing questions. After `ping_group_threshold` pings in one run, the rest arrive grouped by company, 10 to a message.
- A summary after every run.
- The submitted count after each review session.
- One alert when a health check fails.

### Scheduling

With `schedule.mode: daily` or `twice_daily`, `autoapply schedule install` registers a Task Scheduler task called **Cygnus AutoApply**:

- It runs `autoapply run` at your local `schedule.times`.
- **Wake the computer to run this task** is on.
- It runs only while you are logged on, because the browser window is visible.
- It has a 2-hour limit.

A run missed while the PC was off is not made up. The next run carries on from the ledger. Only one run can happen at a time (`autoapply.lock`).

### Budget

Every Claude call's tokens and cost are recorded (`llm.pricing`; empty uses the built-in table). `status` and the review header show this month's spend.

- When this month's spend reaches `monthly_budget_usd`, the run stops before any Claude call and you get an alert.
- If the budget runs out mid-run, the tool stops starting new calls and finishes what is in progress.
- Postings are never scored twice: scores are keyed by posting, resume and prompt version.
- If the backlog doesn't fit in one month's budget, it drains over several months, newest postings first.

---

## How it fits together

```
autoapply/
  cli.py            commands, run lock, logging (secrets redacted)
  config.py         profile.yaml schema (pydantic), secrets.env, answer bank
  ledger.py         SQLite ledger + posting state machine (posting_key is the primary key)
  pipeline.py       sync → prefilter → enrich → blockers → score → fill selection
  sources/          jobmonitor (raw jobs.json + h1b.json + companies.yaml), csv_links
  ats/              detect.py (ATS + posting_key), greenhouse/lever/ashby API clients + form hints
  filters.py        rule prefilter (US-location logic ported from job-monitor)
  h1b.py            three-state H-1B lookup
  blockers.py       citizenship / clearance / sponsorship regexes (confident vs ambiguous)
  llm/              client (JSON output, retries, spend, budget), score, answer, cover_letter, prompts/
  browser/          launch (Chromium + Simplify), simplify.py (ALL Simplify selectors), fields.js/.py,
                    fill (lanes, plan, consistency check, worker pool), replay, submit
  review/           FastAPI review app, actions (answers learning/propagation), templates
  writeback.py      job-monitor status write-back
  notify.py         Discord
  health.py         pre-run checks
  calibrate.py      cutoff calibration
  setup_wizard.py   `autoapply setup`
  scheduler_windows.py
```

Posting states: `discovered → filtered_out | unsupported | enriched → closed | skipped(reason) | scored → filling → ready | needs_input | stuck | error`. From the review lanes: `→ submitted | submitted_manual | would_submit (test) | rejected_by_user | closed`. `error` is retried on the next run, up to 3 times, then becomes `stuck`.

### What was verified while building (2026-10-20)

- **Simplify Copilot 3.1.10.** Loaded unpacked in Playwright's bundled Chromium 141 under a real X display. The panel renders in an open shadow root on `.simplify-jobs-shadow-root`. The autofill button is `#fill-button` (aria-label "Autofill this page"; reads "Autofilling" while running). Logged out shows `#login-button`, and an unfinished profile shows `#onboarding-button`. A first run shows a "Before you autofill" consent card. All of this lives in `autoapply/browser/simplify.py`.
- **ATS endpoints.** Greenhouse `boards-api …/jobs/{id}?questions=true`, Lever `api.lever.co/v0/postings/{co}/{id}` (404 when closed), Ashby `api.ashbyhq.com/posting-api/job-board/{org}`.
- **Application pages.** Greenhouse `job-boards.greenhouse.io/{token}/jobs/{id}`, Lever `…/{id}/apply`, Ashby `…/{id}/application`.
- **Form enumeration and filling** were run on live forms from all three ATSs. They handle Greenhouse react-select comboboxes, Ashby Yes/No buttons, radio and checkbox groups, and Lever cards and selects.

### Still to verify on your PC

- That autofill runs end to end while you are **logged in** to Simplify. This was not possible here without your account. `autoapply run --test --limit 3` is the check. If Simplify changes, the health check notes the version and the next run fills one form as a probe before continuing.
- That headed Chromium works when Task Scheduler wakes the PC with the session locked.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Health: *Simplify is logged out* | `autoapply browser`, log in to Simplify, press Enter. |
| Simplify's "Before you autofill" card keeps coming back | Expected from Simplify itself (it resets on every browser start); the tool answers it from `browser.simplify_consent`. If it still shows, Simplify changed: see `apply_consent` in `autoapply/browser/simplify.py`. |
| Many forms Stuck with `simplify_*` after a Simplify update | Re-run the Simplify step of `autoapply setup` to copy the new version. If the widget changed, update the selectors at the top of `autoapply/browser/simplify.py`. |
| Health: *token rejected (expired?)* | Make a new fine-grained token and put it in `secrets.env` as `GITHUB_TOKEN=`. Pending write-backs are retried automatically. |
| Health: *budget reached* | Wait for next month or raise `llm.monthly_budget_usd`. Nothing is lost; the backlog waits in the ledger. |
| Run stopped: *Simplify's legal answers disagree with your profile* | Your Simplify profile's sponsorship/authorization answers differ from `visa:` in profile.yaml. Fix whichever is wrong. The affected application is in Stuck. |
| Stuck: `simplify_not_ready` / `simplify_did_not_fill` | Simplify's profile did not load, or its autofill filled nothing (the tool clicks up to 3 times). Check `autoapply browser` shows your Simplify profile, then `autoapply requeue --lane stuck` and run again. |
| Stuck: `timed_out` | One form took longer than `browser.form_timeout_s` (default 7 min); the reason says which step. `autoapply requeue --lane stuck` to try again. |
| The run looks frozen | It prints each application and step as it goes (`[2/3] Company - Title`, `Simplify autofill`, `asking Claude ...`); a Claude answer for long essays can take a minute. Nothing asks for confirmation during a run: approve afterwards in `autoapply review`. |
| *Another autoapply run is in progress* | Only one run at a time. If none is running, delete `autoapply.lock` in the profile folder. |
| A job you applied to by hand was attempted | Sync your dashboard marks to `jobs.json` (see Setup), or Reject it in review. |

## Development

```
pip install -e ".[dev]"
pytest                       # unit tests + browser tests on local fixture forms (Simplify mocked, Claude recorded)
pytest -m "not browser"      # without Chromium
```

The browser tests use `tests/fixtures/forms/*.html`, local copies of the Greenhouse, Ashby and Lever form structures. LLM calls go through `FakeTransport`, so the tests never touch the network. A test-mode submit asserts that Submit is never clicked.

**Terms of service.** Automating Simplify and ATS sites may conflict with their terms. You accept that risk by using this tool. LinkedIn postings are never attempted, and the tool never bypasses CAPTCHAs or creates accounts.
