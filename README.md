# Commvault Missed-SLA monitor (Playwright)

Automates the daily manual check: log in to Command Center (domain ECECLOUD) -> Dashboard -> SLA ->
Missed SLA -> list every missed server that is **not** "No Schedule" -> report + optional e-mail.

## Setup
```bash
pip install -r requirements.txt && playwright install chromium
# edit config.json (all non-secret settings), then provide the password:
cp .env.example .env      # set COMMVAULT_PASS (and SMTP_PASS if needed)
```
Passwords are the only values that are not in `config.json`.

## Run
```bash
python commvault_sla_monitor.py --headed --slow-mo 400   # first runs: watch it work
python commvault_sla_monitor.py                          # headless, for scheduling
python commvault_sla_monitor.py --collect-jobs           # also list failed jobs (Monitor > Jobs, best effort)
```
Output goes to `reports/<timestamp>/`: `missed_sla_actionable.csv`, `missed_sla_no_schedule.csv`,
`missed_sla_all.csv`, `summary.json`, `report.html`, step screenshots, `run.log`.
On failure it also writes `error.png` and `trace.zip` (`playwright show-trace trace.zip`).

Exit code: `0` nothing actionable, `2` actionable missed SLAs found, `1` automation failed.

## Run from GitLab (Windows runner, PowerShell)
Files at the repo root: `.gitlab-ci.yml`, `config.json`, `requirements.txt`, `commvault_sla_monitor.py`.
1. Edit `config.json` (username, e-mail, ignore list, `ci.tools_dir`) and commit it.
2. **Settings > CI/CD > Variables**: add `COMMVAULT_PASS` (Masked/Hidden); `SMTP_PASS` only if your SMTP needs a login.
3. In `.gitlab-ci.yml` change `tags: [commvault]` to your Windows runner's tag (GitLab only accepts tags in the YAML).
4. **Build > Pipeline schedules**: `0 8 * * *`, timezone Karachi. Test with **Build > Pipelines > Run pipeline**.
5. Results: job log shows `summary.json`; **Browse artifacts** has the CSVs, `report.html`, screenshots (30 days).
First run creates a venv and Chromium in `ci.tools_dir` (Python 3 + PyPI access needed); later runs reuse it.
Job colours: green = nothing actionable, orange = missed SLAs found (exit 2), red = automation failed (exit 1).

## Schedule without GitLab (daily 08:00)
Linux cron: `0 8 * * * cd /opt/commvault-automation && python3 commvault_sla_monitor.py`
Windows: Task Scheduler -> `python.exe C:\path\commvault_sla_monitor.py` (Start in: the script folder).

## If a step breaks after a Command Center upgrade
The selectors live in a few small functions: `select_domain`, `login`, `open_sla_report`,
`open_missed_sla`, `_READ_TABLE_JS` / `_NEXT_SELECTORS`. Run with `--headed -v`, look at the
screenshot of the last step, and adjust that one function.
