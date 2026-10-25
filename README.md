# Cygnus (AutoApply)

Cygnus applies to software jobs for you, and you approve every application before it goes out.

1. It reads postings from your [job-monitor](https://github.com/Piyush-v-patil/job-monitor) tracker, or from a CSV of links.
2. It drops postings with cheap rules first (tier, role, US location, H-1B record, blocker phrases). Claude then scores the rest for fit.
3. For each good fit, it opens the Greenhouse / Ashby / Lever / Workday form in a Chromium window that has **Simplify Copilot** loaded. Simplify autofills the form, the tool swaps in the right resume, and Claude fills what Simplify left blank.
4. Every filled application goes into a local **review queue**. Nothing is submitted until you click *Approve & submit*.
5. Every posting is recorded in a ledger, so nothing is applied to twice. A copy of each submitted application is saved, and the posting is marked `applied` or `skip` back in job-monitor.

Everything runs on your PC. The repository contains no personal data. Your resumes, answers, keys and history live in a private profile folder, `%USERPROFILE%\.autoapply\` by default.

> **Rules the tool follows.** It never makes up a fact. Claude may write prose answers from your resume, but a factual answer (yes/no, numbers, dates, eligibility) must come from Simplify, your saved answers, or a quote from your resume. If none of those has it, the application waits in *Needs input*. Values Simplify fills are never overwritten, except the resume file and a work-authorization or sponsorship answer that contradicts your visa answers: that one is replaced with yours, marked on the review card, and you get one warning per run to fix your Simplify profile. CAPTCHAs and login walls are never bypassed: those applications go to *Stuck*.

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

Keys, tokens and webhook URLs are typed hidden, so they can't end up in a screenshot. If a GitHub token is already saved, setup offers to replace it, for example after a leak. Running setup again keeps a dated copy of the old `profile.yaml` (`profile.yaml.<date>.bak`). It also keeps the settings it did not ask about, such as `review.port`, `workday.*` or `llm.pricing`.

Before the first run:

- **Sync your manual Applied marks.** Marks you made on the job-monitor dashboard stay in your browser until you add a GitHub token in the dashboard's ⚙ settings and they sync to `jobs.json`. Until then, the tool cannot see them and may re-attempt jobs you already applied to by hand.
- Run `autoapply health`.
- Try a dry run: `autoapply run --test --limit 3`. It fills real forms but never clicks Submit.

## Daily use

```
autoapply run [--limit N] [--no-fill] [--test]   # health → sync → filter → score → fill → queue → notify
autoapply run --review [--limit N] [--test]      # same, then review right away with the filled forms still open
autoapply review                                 # open the review queue + the tool's browser
autoapply status                                 # counts per state, spend, last run's numbers
autoapply calibrate [--rescore]                  # label ~40 jobs blind; suggests a cutoff
autoapply answers list|edit                      # your answer bank
autoapply requeue [--lane stuck|needs_input|ready|all]  # fill queued applications again next run
autoapply requeue --lane tested                  # after testing: let test-approved jobs be applied for real
autoapply browser [--url URL]                    # open the tool's browser (Simplify login, profile)
autoapply workday [login | open <company>]       # Workday: the login it uses, companies that need an account
autoapply mail [login | test | recent]           # read-only mailbox for security codes and Workday emails
autoapply schedule install|remove|show           # Windows Task Scheduler
autoapply health                                 # the pre-run checks on their own
```

Global options: `--profile-dir PATH` (or `AUTOAPPLY_HOME`) and `--verbose`. Each run writes a log file to `logs\`.

### The review queue

`autoapply review` opens `http://127.0.0.1:8765` (localhost only) and the tool's browser.

| Lane | What you do |
|---|---|
| **Ready** | Check the fields. Each one is labelled with its source: Simplify, Saved answer, Claude (with the resume evidence or facts it used), or You. **Open form to check** shows the real application in the tool's browser, where you can change anything. Then **Approve & submit** (it sends the form as it stands) or **Reject**. If you sent it yourself in the form's tab (after a code or a check there, say), click **I submitted it** to record it. *Approve all visible* still submits them one at a time. |
| **Needs input** | Questions about you that Simplify, your saved answers and Claude may not answer (work authorization, salary, start date, consent...). Answer them on the card and click **Save & open the form to check**, or click **Open form**, answer in the form itself and click **I answered in the form**. Either way the answers are saved to `answers.yaml`, complete any other queued application that asks the same question, and the application moves to Ready. Answering never submits: you approve from Ready after checking the form. **I submitted it** records one you sent yourself. **Fill again** (or **Fill all again**) queues it to be filled from scratch on the next run with your saved answers, for example when its tab is gone. |
| **Stuck** | A CAPTCHA, login wall, Simplify failure, failed upload, or a form that changed. **Open form** brings the tab up, or reopens it and refills it from the plan. Finish it by hand, then click **I submitted it** (a screenshot is recorded) or **Give up**. **Fill again** (or **Fill all again**) queues it to be filled from scratch on the next run. Temporary trouble (Simplify not ready, a page that did not load, a timeout) is filled again automatically by the next run, at most twice per application. |
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

### Workday

Workday sites (Capital One, NVIDIA, Salesforce and many more) need an account on each company's site. Use the same email and password everywhere. Cygnus signs in with them, and creates an account where you have none: if your login is refused at a company, it creates the account there with your email and password, waits `workday.after_create_wait_s` (5 s), and signs in. Creating an account ticks that company's account agreement box for you. If Workday wants the email address verified first, or the form shows a CAPTCHA, the card says so. Set `workday.create_accounts: false` to create accounts yourself instead.

**A new account that refuses its own password.** Many tenants refuse a new account's password until its email address is confirmed, often with a plain "wrong email or password". An account Workday says *already exists* (made by hand or by Simplify's button) also has a password Cygnus doesn't know. With [mail access](#mail-codes-and-workday-emails), Cygnus does what you would do by hand:

1. It opens Workday's verification email, if one came, and signs in.
2. Otherwise it clicks *Forgot your password?* and opens the reset email.
3. It sets your Workday password, the same one, and signs in.

Without mail access, it sends the reset email for you and the card says so: set your password from that email, then click **Fill again**. Either way, an account Cygnus knows exists is never created again, which used to be the requeue loop.

Each company is tried **once** until you requeue. After a failed sign-in or account creation, the company's other postings wait without opening, because repeated failed sign-ins are how Workday accounts get locked. Saving a new login with `autoapply workday login` lets those companies be tried again. Workday passwords need 8 or more characters with an uppercase letter, a lowercase letter, a number and a special character; `workday login` checks this.

1. `autoapply workday login` saves the email and password to `secrets.env`; they are hidden in logs. `autoapply setup` asks for them too.
2. Add `workday` to `filters.ats_allowed` in `profile.yaml`.
3. Run as usual. A company where the sign-in fails goes to *Stuck* (`workday_sign_in_failed`), and its other postings wait.
4. `autoapply workday` lists the companies with Workday postings waiting, and whether signing in worked.
5. `autoapply workday open <company>` opens that company's Create Account form with your email and password already typed in, without Simplify. Simplify's own *Create Account & Autofill* button would pick a password of its own. You tick the agreement box, click Create Account, and verify your email address if Workday asks.
6. Then run `autoapply requeue --lane stuck` to retry.

If an account was made with Simplify's button anyway, Cygnus also tries the password Simplify saved for it, and remembers which one worked for that company. If neither works, the Stuck card lists the usual causes: no account yet, Simplify's password (use *Forgot your password* and set yours), or an unverified email address.

A Workday application is several pages: My Information, My Experience, Application Questions, Voluntary Disclosures and Review. On each page, Simplify fills what it knows. Cygnus then fills the rest the usual way (your saved answers, your resume, Claude) and clicks *Save and Continue*. It stops on **Review**, and Submit is clicked only after you approve.

- A page with questions only you can answer stops there, as *Needs input*. When you answer, the tab carries on from that page, to Review or to the next questions.
- Repeated entries (Work Experience 1, 2, …; Education; Websites) are asked as "Work Experience 2 › From". Those answers fill this form only; they are never saved to `answers.yaml` or used on other forms.
- Pick-lists with sub-menus, such as *How did you hear about us*, offer their choices as "Job Board > LinkedIn Job Posting". Cygnus opens the sub-menu to pick it.
- Your role's resume goes into Workday's resume upload area. If a company's pages have none, the record says no resume was attached instead of naming one.
- Steps that share a name (two "Application Questions" pages) are told apart by their step number.
- A job page whose Apply button offers no *Apply Manually*, a page the tool doesn't know (named in the card), or an error Workday shows outside a question goes to *Stuck* with what the page said. The walk doesn't wait out the time limit.
- The tab may be gone by approval time. Workday keeps the unfinished application in your account, so Cygnus reopens it and walks to Review again with your answers.
- After Submit, Cygnus waits for Workday's "Application Submitted" confirmation. Without it, the application goes to *Stuck* (`submit_unconfirmed`) instead of being recorded as applied. Check the tab, then click **I submitted it** or **Not sent - Submit it again**.
- A Workday tab older than `browser.tab_max_age_min` is reopened and walked to Review again before it can be sent, because Workday's sign-in may have run out.
- A Workday form can take 5–10 minutes. `browser.workday_form_timeout_s` (900 s by default) is the limit per form.
- **One Workday application at a time.** Simplify fills each new Workday page by itself and pauses while its tab is hidden ("Please stay on this Workday page"). So a Workday application keeps the front of the tool's browser from sign-in to Review, and the next one waits. Greenhouse, Ashby and Lever forms in the same run are filled first.

### After Submit

**Never sent twice.** Cygnus records the moment it clicks Submit, before the click. Once that is recorded, the application is never sent again unless you say so:

- If Cygnus or the PC stops before the site confirms, the next run moves the card to *Stuck* (`submit_interrupted`). Check your email or the site, then click **I submitted it**, or **Not sent - Submit it again**.
- **Fill again**, **Fill all again**, `requeue` and the automatic retries all skip applications whose Submit was clicked.
- If the open tab already shows the site's confirmation (you sent it yourself), **Approve** records it as submitted by you instead of sending it again.
- On Workday, if Approve has to walk the application to Review first (its tab was gone, or it stopped on an earlier page), it stops on Review for you to look at. Click Approve again to send it.


Cygnus counts an application as sent only when the board confirms it: a confirmation page, a success message, or a confirmation address. Workday has its own check, described above.

- **Security code:** Greenhouse, and Ashby-style boards, sometimes email a code before accepting an application. With [mail access](#mail-codes-and-workday-emails), Cygnus reads the code after you click **Approve**, types it and finishes; **Approve all** runs through codes unattended. Without mail, or if no email arrives within `mail.wait_s` (180 s), the tab stays open and the card goes to *Stuck* with a box for the code, and Discord gets a ping. Type the code and click **Enter code & submit**, or **Get it from my email** for a late email. A wrong code shows the board's message, and you can try again.
- **No confirmation within 30 seconds:** the card goes to *Stuck* (`submit_unconfirmed`) with the tab open. Check it, and click **I submitted it** if it went through.

### Mail: codes and Workday emails

Cygnus can read the few emails a site sends right after it acts: security codes, and Workday's verify and password-reset links. It uses read-only IMAP with a Gmail **app password**:

1. Turn on 2-Step Verification for the Google account.
2. Create an app password at https://myaccount.google.com/apppasswords and name it Cygnus.
3. Run `autoapply mail login` (or answer yes in `autoapply setup`). It checks the login, saves the address and app password to `secrets.env` (hidden in logs), and sets `mail.enabled: true`.

What it reads:
- It only looks at mail that arrived after the Submit or the Workday request it is waiting for.
- It only uses a code next to "code" wording, or a link on that company's own Workday address.
- Nothing is marked read, moved or deleted.
- Codes and links are never logged.

`autoapply mail recent` lists the sender and subject (never the body) of recent Greenhouse, Ashby and Workday emails, which helps when a code or link isn't found. To stop, set `mail.enabled: false`, or delete the app password in your Google account.

### Simplify, one form at a time

Simplify has a setting, *Continue and submit automatically*, that would click Continue, or Submit, by itself. Cygnus switches it off every time it starts the browser. If it can't be switched off, the run stops instead of risking an application you did not approve.

Several forms are filled in parallel (`browser.parallel_tabs`), but Simplify works on one at a time. Simplify pauses while its tab is hidden, so each form comes to the front of the tool's browser for its turn. Cygnus waits for Simplify's own "finished" status. If Simplify doesn't report one, it falls back to watching the Autofill button and the form.

### US-person-only employers

With `visa.skip_us_person_roles: true`, postings at employers where nearly every role needs US citizenship, US-person status or a clearance are skipped before anything is fetched or scored. That covers defense, space and national labs, for example Anduril, SpaceX, Lockheed Martin, RTX and Booz Allen. The list is in `autoapply/us_person.py`. Add employers with `visa.us_person_companies_add` and exempt them with `visa.us_person_companies_remove`. A change takes effect on the next run, including for postings already skipped this way. These skips are not written back to job-monitor.

### Scheduling

With `schedule.mode: daily` or `twice_daily`, `autoapply schedule install` registers a Task Scheduler task called **Cygnus AutoApply**:

- It runs `autoapply run --limit <schedule.limit>` at your local `schedule.times`. `schedule.limit` defaults to 50 jobs per run; `null` removes the limit. After changing it, run `autoapply schedule install` again.
- **Wake the computer to run this task** is on.
- It runs only while you are logged on, because the browser window is visible.
- It has a 2-hour limit.

A run missed while the PC was off is not made up. The next run carries on from the ledger. Only one run can happen at a time (`autoapply.lock`). An open `autoapply review` also holds the lock, so a scheduled run that starts while review is open is skipped and sends a Discord note. Close review (Ctrl+C) when you are done with it.

### Budget

Every Claude call's tokens and cost are recorded (`llm.pricing`; empty uses the built-in table). `status` and the review header show this month's spend. Each run ends with what it cost, for example `Claude cost this run: $0.42 (form answers $0.27, 12 calls · scoring $0.10, 85 calls · cover letters $0.05, 2 calls)`. `autoapply status` repeats it for the last run, the Discord run summary includes it, and a review session prints what it spent at *Session over*. A model the table does not know, such as a new model id, is counted at the highest known rate, never as free; the health check says so. Set its price in `llm.pricing` to fix the count.

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
| Health: *budget reached* | Wait for next month or raise `llm.monthly_budget_usd`. Nothing is lost; the backlog waits in the ledger. `autoapply review` still opens, with a note, so you can approve what is already filled. A failing GitHub token or Discord webhook doesn't block review either. |
| Run stopped: *Simplify's legal answers disagree with your profile* | Your Simplify profile's sponsorship/authorization answers differ from `visa:` in profile.yaml. Fix whichever is wrong. The affected application is in Stuck. |
| Stuck: `simplify_not_ready` / `simplify_did_not_fill` | Simplify's profile did not load, or its autofill filled nothing (the tool clicks up to 3 times). Check `autoapply browser` shows your Simplify profile, then `autoapply requeue --lane stuck` and run again. |
| Stuck: `timed_out` | One form took longer than `browser.form_timeout_s` (default 7 min; Workday `browser.workday_form_timeout_s`); the reason says which step. Time spent waiting for another form to finish with Simplify does not count. `autoapply requeue --lane stuck` to try again. |
| The run looks frozen | It prints each application and step as it goes (`[2/3] Company - Title`, `Simplify autofill`, `asking Claude ...`); a Claude answer for long essays can take a minute. Nothing asks for confirmation during a run: approve afterwards in `autoapply review`. |
| *Another autoapply run is in progress* | Only one run at a time, and an open `autoapply review` counts as one. Close the review window's command prompt (Ctrl+C) first. Delete `autoapply.lock` in the profile folder only if nothing is running. |
| *Configuration problem: profile.yaml: `x` is written twice* | A setting appears twice in profile.yaml, so it is not clear which one you meant. The message gives both line numbers: delete one. |
| *answers.yaml entry N is skipped* | That saved answer has a typo (for example an unknown `kind:`). The others still work, and the entry is kept in the file until you fix it. |
| *autoapply run stopped on an unexpected error* | The message names a `...-crash.log` in the profile's `logs` folder, and Discord gets an alert. Share that file (secrets are already removed) when asking for help. |
| Stuck: `email_code ... (no code email arrived in 180s)` | The email was slow or went to another folder. Click **Get it from my email** once it is there, or type the code. `autoapply mail recent` shows whether it arrived. |
| Stuck: `workday_reset_sent` | Cygnus has no mail access, so it sent the password reset email for you. Open it, set your Workday password (the same one), then click **Fill again**. Or run `autoapply mail login` once, and Cygnus does this itself. |
| Review: *That did not work (...)* | An action failed. Nothing was submitted by the error, and the log has the details. If you closed the tool's browser window, the next action opens a new one. |
| Review: *... is already submitted; nothing was changed* | You clicked a card from an older page or another tab after that application had moved on. Reload the page. |
| Discord: *the H-1B list (h1b.json) could not be read* | New postings from that source wait for the next run instead of skipping the sponsor check. Postings already in progress continue. Usually a passing network problem. |
| A job you applied to by hand was attempted | Sync your dashboard marks to `jobs.json` (see Setup), or Reject it in review. |

## Development

```
pip install -e ".[dev]"
pytest                       # unit tests + browser tests on local fixture forms (Simplify mocked, Claude recorded)
pytest -m "not browser"      # without Chromium
```

The browser tests use `tests/fixtures/forms/*.html`, local copies of the Greenhouse, Ashby and Lever form structures, and a stand-in for a Workday apply flow (sign-in, pages, widgets, errors, Review, confirmation). LLM calls go through `FakeTransport`, so the tests never touch the network. A test-mode submit asserts that Submit is never clicked.

**Terms of service.** Automating Simplify and ATS sites may conflict with their terms. You accept that risk by using this tool. LinkedIn postings are never attempted, and the tool never bypasses CAPTCHAs. It creates Workday accounts only with your own Workday login, and only while `workday.create_accounts` is on.
