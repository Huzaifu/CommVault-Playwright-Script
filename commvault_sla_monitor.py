#!/usr/bin/env python3
"""
Commvault Command Center - daily Missed SLA monitor (Playwright, browser based).

What it automates (the manual process):
  1. Open Command Center, log in with the admin account (settings from config.json, password from COMMVAULT_PASS)
  2. Explore > Dashboard > SLA tile  -> SLA report
  3. Open "Missed SLA" (breakdown by reason + "Unprotected servers" table)
  4. Keep every missed-SLA server that is NOT category "No Schedule"
  5. Write CSV / JSON / HTML report + screenshots, optionally e-mail the summary

Exit codes:  0 = no actionable missed SLAs   2 = actionable missed SLAs found
             1 = the automation itself failed (login, UI changed, ...)
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import logging
import os
import re
import smtplib
import sys
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

log = logging.getLogger("commvault-sla")


# --------------------------------------------------------------------------- config
def load_env_file(path: str) -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def env_list(name: str, default: str = "") -> list[str]:
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


@dataclass
class Config:
    base_url: str
    username: str
    password: str
    domain: str
    out_dir: Path
    headed: bool = False
    slow_mo: int = 0
    timeout_ms: int = 45_000
    ignore_tls: bool = True  # Command Center on an internal IP uses a self-signed cert
    dashboard_route: str = "/commandcenter/#/dashboard/hyperscalecommcell"
    # categories that are NOT counted as actionable (client's rule: ignore "No Schedule")
    ignore_categories: list[str] = field(default_factory=lambda: ["no schedule"])
    ignore_servers: list[str] = field(default_factory=list)
    collect_jobs: bool = False
    send_mail: bool = True
    mail: dict = field(default_factory=dict)


def build_config(args: argparse.Namespace) -> Config:
    """All non-secret settings come from config.json. Only the passwords come from the environment
    (GitLab CI/CD variables COMMVAULT_PASS and SMTP_PASS, or a local .env file)."""
    load_env_file(args.env_file)
    cfg_path = Path(args.config)
    if not cfg_path.exists():
        sys.exit(f"Config file not found: {cfg_path}")
    j: dict = json.loads(cfg_path.read_text(encoding="utf-8-sig"))  # utf-8-sig tolerates a BOM
    mail = j.get("email", {})
    if "password" in j or "smtp_pass" in mail:
        sys.exit("Do not store passwords in config.json - use GitLab variables COMMVAULT_PASS / SMTP_PASS")

    missing = [k for k in ("url", "username", "domain") if not j.get(k)]
    if missing:
        sys.exit(f"config.json is missing: {', '.join(missing)}")
    password = os.environ.get("COMMVAULT_PASS")
    if not password:
        sys.exit("Environment variable COMMVAULT_PASS is not set (add it as a GitLab CI/CD variable)")

    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    return Config(
        base_url=str(j["url"]).rstrip("/"),
        username=j["username"],
        password=password,
        domain=j["domain"],
        out_dir=Path(args.out_dir) / stamp,
        headed=args.headed,
        slow_mo=args.slow_mo,
        timeout_ms=int(j.get("timeout_seconds", 45)) * 1000,
        ignore_tls=bool(j.get("ignore_tls", True)),
        dashboard_route=j.get("dashboard_route", Config.dashboard_route),
        ignore_categories=[c.lower() for c in j.get("ignore_categories", ["No Schedule"])],
        ignore_servers=[x.lower() for x in j.get("ignore_servers", [])],
        collect_jobs=args.collect_jobs or bool(j.get("collect_jobs", False)),
        send_mail=not args.no_email and bool(mail.get("enabled", True)),
        mail={
            "host": mail.get("smtp_host", ""),
            "port": int(mail.get("smtp_port", 25)),
            "starttls": bool(mail.get("starttls", False)),
            "user": mail.get("smtp_user", ""),
            "password": os.environ.get("SMTP_PASS", ""),
            "from": mail.get("from", ""),
            "to": mail.get("to", []),
        },
    )


# --------------------------------------------------------------------------- helpers
class Shots:
    def __init__(self, page: Page, folder: Path):
        self.page, self.folder, self.n = page, folder, 0

    def __call__(self, name: str) -> None:
        self.n += 1
        try:
            self.page.screenshot(path=str(self.folder / f"{self.n:02d}_{name}.png"))
        except Exception as exc:  # screenshots must never break the run
            log.debug("screenshot failed: %s", exc)


def body_text(page: Page) -> str:
    return page.locator("body").inner_text()


# --------------------------------------------------------------------------- step 1: login
def select_domain(page: Page, domain: str) -> bool:
    """Pick the domain on the login form. Returns True if a domain control was found."""
    want = domain.lower()

    # native <select>
    selects = page.locator("select")
    for i in range(selects.count()):
        sel = selects.nth(i)
        if not sel.is_visible():
            continue
        for opt in sel.locator("option").all_inner_texts():
            if opt.strip().lower() == want:
                sel.select_option(label=opt)
                log.info("Domain selected (native select): %s", opt.strip())
                return True

    # custom dropdown (combobox / "Domain" label)
    for opener in (
        page.get_by_role("combobox"),
        page.get_by_label(re.compile("domain", re.I)),
        page.get_by_placeholder(re.compile("domain", re.I)),
    ):
        try:
            if opener.count() and opener.first.is_visible():
                opener.first.click()
                page.get_by_role("option", name=re.compile(rf"^\s*{re.escape(domain)}\s*$", re.I)).first.click(
                    timeout=5000
                )
                log.info("Domain selected (custom dropdown): %s", domain)
                return True
        except Exception:
            continue
    return False


def login(page: Page, cfg: Config, shot: Shots) -> None:
    log.info("Opening %s", cfg.base_url)
    page.goto(f"{cfg.base_url}/commandcenter/", wait_until="domcontentloaded")

    user_box = page.locator(
        "input[name='username'], input#username, input[autocomplete='username'], input[type='text']"
    ).first
    user_box.wait_for(state="visible")
    pwd_box = page.locator("input[type='password']").first

    domain_ok = select_domain(page, cfg.domain)
    # Fallback: Commvault also accepts DOMAIN\user in the username field
    user_box.fill(cfg.username if domain_ok else f"{cfg.domain}\\{cfg.username}")
    pwd_box.fill(cfg.password)
    shot("login_filled")  # password is masked by the browser

    btn = page.get_by_role("button", name=re.compile(r"^\s*(log ?in|sign ?in)\s*$", re.I))
    if btn.count():
        btn.first.click()
    else:
        pwd_box.press("Enter")

    # logged in when the left navigation ("Explore") shows up
    try:
        page.get_by_text("Explore", exact=True).first.wait_for(state="visible")
    except Exception:
        shot("login_failed")
        err = ""
        for pat in (r"invalid", r"incorrect", r"failed", r"locked", r"expired"):
            loc = page.get_by_text(re.compile(pat, re.I))
            if loc.count():
                err = loc.first.inner_text()
                break
        raise RuntimeError(f"Login did not complete. Page message: {err or 'none found'}")
    log.info("Logged in as %s\\%s", cfg.domain, cfg.username)
    shot("logged_in")


# --------------------------------------------------------------------------- step 2: SLA dashboard
def open_sla_report(page: Page, cfg: Config, shot: Shots) -> dict:
    page.goto(f"{cfg.base_url}{cfg.dashboard_route}", wait_until="domcontentloaded")
    sla_title = page.get_by_text("SLA", exact=True).first
    try:
        sla_title.wait_for(state="visible", timeout=20_000)
    except Exception:
        log.info("Dashboard route not found, using Explore > Dashboard")
        page.get_by_text("Explore", exact=True).first.click()
        page.get_by_text("Dashboard", exact=True).first.click()
        sla_title = page.get_by_text("SLA", exact=True).first
        sla_title.wait_for(state="visible")
    shot("dashboard")

    text = body_text(page)
    tile = {}
    if m := re.search(r"Met\s*:?\s*(\d+)\s*\(([\d.]+)\s*%\)", text):
        tile["met"], tile["met_pct"] = int(m[1]), float(m[2])
    if m := re.search(r"Missed\s*:?\s*(\d+)\s*\(([\d.]+)\s*%\)", text):
        tile["missed"], tile["missed_pct"] = int(m[1]), float(m[2])
    log.info("Dashboard SLA tile: %s", tile)

    sla_title.click()
    page.get_by_text(re.compile(r"Overall SLA is", re.I)).first.wait_for(state="visible")
    page.wait_for_timeout(1500)  # let the pie chart animate
    shot("sla_report")

    text = body_text(page)
    sla = dict(tile)
    if m := re.search(r"Overall SLA is\s*([\d.]+)\s*%", text, re.I):
        sla["overall_sla_pct"] = float(m[1])
    if m := re.search(r"Data as of:?\s*(.+)", text):
        sla["data_as_of"] = m[1].strip()
    return sla


# --------------------------------------------------------------------------- step 3: Missed SLA page
def open_missed_sla(page: Page, shot: Shots) -> None:
    """Click the red 'Missed SLA' slice (or its legend entry) on the SLA report."""
    unprotected = page.get_by_text(re.compile(r"Unprotected servers", re.I)).first

    def arrived() -> bool:
        try:
            unprotected.wait_for(state="visible", timeout=8000)
            return True
        except Exception:
            return False

    # 1) legend entry "Missed SLA: 35 (9.43%)"
    legend = page.get_by_text(re.compile(r"Missed SLA\s*:\s*\d+"))
    if legend.count():
        legend.first.click()
        if arrived():
            shot("missed_sla")
            return

    # 2) the red pie slice itself
    idx = page.evaluate(
        """() => {
            const paths = [...document.querySelectorAll('svg path')];
            for (let i = 0; i < paths.length; i++) {
                const m = getComputedStyle(paths[i]).fill.match(/\\d+/g);
                if (!m) continue;
                const [r, g, b] = m.map(Number);
                if (r > 170 && g < 130 && b < 130) return i;
            }
            return -1;
        }"""
    )
    if idx >= 0:
        page.locator("svg path").nth(idx).dispatch_event("click")
        if arrived():
            shot("missed_sla")
            return

    shot("missed_sla_not_found")
    raise RuntimeError("Could not open the Missed SLA page (legend and red slice both failed)")


def parse_breakdown(page: Page) -> dict[str, int]:
    """Legend of the Missed SLA pie, e.g. 'Backup Job Failed : 7'."""
    text = body_text(page)
    cut = text.lower().find("unprotected servers")
    segment = text[max(0, cut - 3000):cut] if cut > 0 else text
    out: dict[str, int] = {}
    for name, n in re.findall(r"([A-Za-z][A-Za-z /()\-]+?)\s*:\s*(\d+)", segment):
        name = name.strip()
        if name.lower() not in ("data as of", "overall sla"):
            out[name] = int(n)
    return out


# --------------------------------------------------------------------------- step 4: read the table
_READ_TABLE_JS = r"""
(headingPattern) => {
  const re = new RegExp(headingPattern, 'i');
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const tableSel = 'table,[role=grid],[role=table],[role=treegrid]';
  const tables = [...document.querySelectorAll(tableSel)];
  const heads = [...document.querySelectorAll('body *')]
      .filter(e => { const t = clean(e.textContent); return t.length < 60 && re.test(t); });
  for (const h of heads) {
    const t = tables.find(t => (h.compareDocumentPosition(t) & Node.DOCUMENT_POSITION_FOLLOWING)
                                && !t.contains(h));
    if (!t) continue;
    const headers = [...t.querySelectorAll('th,[role=columnheader]')].map(x => clean(x.textContent));
    const rows = [...t.querySelectorAll('tbody tr,[role=row]')]
        .filter(r => !r.querySelector('th,[role=columnheader]'))
        .map(r => [...r.querySelectorAll('td,[role=gridcell],[role=cell]')].map(c => clean(c.textContent)))
        .filter(r => r.length);
    return {headers, rows};
  }
  return null;
}
"""

_NEXT_SELECTORS = ", ".join(
    [
        "button[aria-label*='next' i]",
        "a[aria-label*='next' i]",
        "[title*='next' i]",
        "li.next a",
        ".pagination-next",
        "button:has(i.fa-chevron-right)",
        "button:has(.fa-angle-right)",
        "button:has(svg[class*='right' i])",
    ]
)


def read_table_all_pages(page: Page, heading: str, shot: Shots, max_pages: int = 60) -> list[dict]:
    headers: list[str] = []
    records: list[dict] = []
    seen_sigs: set[str] = set()

    for page_no in range(1, max_pages + 1):
        data = page.evaluate(_READ_TABLE_JS, heading)
        if not data or not data["rows"]:
            if page_no == 1:
                raise RuntimeError(f"Table under heading '{heading}' not found or empty")
            break
        if page_no == 1:
            headers = [h or f"col{i}" for i, h in enumerate(data["headers"])]
        sig = "|".join(data["rows"][0]) + f"#{len(data['rows'])}"
        if sig in seen_sigs:
            break  # next-click did not change the page
        seen_sigs.add(sig)

        for cells in data["rows"]:
            if len(cells) != len(headers):  # e.g. an extra checkbox cell
                cells = (cells[-len(headers):] if len(cells) > len(headers)
                         else cells + [""] * (len(headers) - len(cells)))
            records.append(dict(zip(headers, cells)))
        log.info("Table page %d: %d rows (total %d)", page_no, len(data["rows"]), len(records))
        if page_no == 1:
            shot("unprotected_table")

        nxt = page.locator(_NEXT_SELECTORS).last
        if not nxt.count() or not nxt.is_visible() or nxt.is_disabled():
            break
        cls = (nxt.get_attribute("class") or "").lower()
        if "disabled" in cls or nxt.get_attribute("aria-disabled") == "true":
            break
        nxt.click()
        page.wait_for_timeout(900)

    # sanity check against the "1-20 of 35" label
    m = re.search(r"\d+\s*[-–]\s*\d+\s+of\s+(\d+)", body_text(page))
    if m and int(m[1]) != len(records):
        log.warning("Pager says %s rows but %d were read - check pagination", m[1], len(records))
    # drop unnamed columns that are always empty (checkbox column)
    empty = {k for k in headers if re.fullmatch(r"col\d+", k) and not any(r.get(k) for r in records)}
    return [{k: v for k, v in r.items() if k not in empty} for r in records]


def col(records: list[dict], pattern: str) -> str | None:
    rx = re.compile(pattern, re.I)
    for k in records[0] if records else []:
        if rx.search(k):
            return k
    return None


def classify(records: list[dict], cfg: Config) -> tuple[list[dict], list[dict]]:
    cat_key = col(records, r"categor")
    srv_key = col(records, r"^(server|client|name)")
    if not cat_key:
        log.warning("No 'Category' column found - nothing can be excluded as 'No Schedule'")
    actionable, ignored = [], []
    for r in records:
        cat = (r.get(cat_key, "") if cat_key else "").lower()
        srv = (r.get(srv_key, "") if srv_key else "").lower()
        if any(c in cat for c in cfg.ignore_categories) or srv in cfg.ignore_servers:
            ignored.append(r)
        else:
            actionable.append(r)
    return actionable, ignored


# --------------------------------------------------------------------------- optional: job status
def collect_failed_jobs(page: Page, shot: Shots) -> list[dict]:
    """Best effort: Monitor > Jobs, list rows whose status mentions fail/kill/error."""
    page.get_by_text("Monitor", exact=True).first.click()
    page.get_by_text("Jobs", exact=True).first.click()
    page.locator("table,[role=grid]").first.wait_for(state="visible")
    page.wait_for_timeout(1500)
    shot("jobs")
    data = page.evaluate(_READ_TABLE_JS, r"^\s*jobs?\s*$") or page.evaluate(
        _READ_TABLE_JS, r"."
    )
    if not data:
        return []
    headers = [h or f"col{i}" for i, h in enumerate(data["headers"])]
    rows = [dict(zip(headers, c)) for c in data["rows"] if len(c) == len(headers)]
    status_key = col(rows, r"status")
    return [r for r in rows if status_key and re.search(r"fail|kill|error|critical", r[status_key], re.I)]


# --------------------------------------------------------------------------- reporting
def write_csv(path: Path, records: list[dict]) -> None:
    if not records:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(records[0].keys()))
        w.writeheader()
        w.writerows(records)


def html_table(records: list[dict]) -> str:
    if not records:
        return "<p><i>None</i></p>"
    head = "".join(f"<th>{html.escape(k)}</th>" for k in records[0])
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(v))}</td>" for v in r.values()) + "</tr>" for r in records
    )
    return (
        '<table border="1" cellpadding="5" cellspacing="0" '
        'style="border-collapse:collapse;font:13px Segoe UI,Arial">'
        f"<thead style='background:#eee'><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
    )


def build_html(summary: dict, actionable: list[dict], ignored: list[dict], jobs: list[dict]) -> str:
    s = summary
    breakdown = "".join(f"<li>{html.escape(k)}: <b>{v}</b></li>" for k, v in s["breakdown"].items())
    jobs_html = f"<h3>Failed jobs</h3>{html_table(jobs)}" if s.get("jobs_collected") else ""
    colour = "#c0392b" if actionable else "#27ae60"
    return f"""<html><body style="font:14px Segoe UI,Arial">
<h2>Commvault daily backup / SLA summary - {html.escape(s['run_time'])}</h2>
<p>Overall SLA: <b>{s['sla'].get('overall_sla_pct', 'n/a')}%</b> &nbsp;|&nbsp;
Met: {s['sla'].get('met', 'n/a')} &nbsp;|&nbsp; Missed: {s['sla'].get('missed', 'n/a')}
&nbsp;|&nbsp; Data as of: {html.escape(str(s['sla'].get('data_as_of', 'n/a')))}</p>
<p style="color:{colour};font-size:16px"><b>{len(actionable)} missed SLA(s) need attention</b>
(excluding &quot;No Schedule&quot;: {len(ignored)} ignored)</p>
<h3>Missed SLA by reason</h3><ul>{breakdown}</ul>
<h3>Action needed (not &quot;No Schedule&quot;)</h3>{html_table(actionable)}
{jobs_html}
<h3>Ignored (&quot;No Schedule&quot;)</h3>{html_table(ignored)}
</body></html>"""


def send_mail(mail: dict, subject: str, html_body: str, attachments: list[Path]) -> None:
    if not mail.get("host") or not mail.get("to"):
        log.info("SMTP host / recipients not configured - skipping e-mail")
        return
    msg = EmailMessage()
    msg["Subject"], msg["To"] = subject, ", ".join(mail["to"])
    msg["From"] = mail.get("from") or mail.get("user") or "commvault-monitor@localhost"
    msg.set_content("HTML mail - please view in an HTML capable client.")
    msg.add_alternative(html_body, subtype="html")
    for p in attachments:
        if p.exists() and p.stat().st_size:
            msg.add_attachment(p.read_bytes(), maintype="text", subtype="csv", filename=p.name)
    with smtplib.SMTP(mail["host"], mail["port"], timeout=30) as s:
        if mail.get("starttls"):
            s.starttls()
        if mail.get("user"):
            s.login(mail["user"], mail.get("password", ""))
        s.send_message(msg)
    log.info("Summary e-mailed to %s", ", ".join(mail["to"]))


# --------------------------------------------------------------------------- main
def run(cfg: Config) -> int:
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(cfg.out_dir / "run.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(fh)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not cfg.headed, slow_mo=cfg.slow_mo)
        ctx = browser.new_context(
            ignore_https_errors=cfg.ignore_tls, viewport={"width": 1920, "height": 1080}
        )
        ctx.set_default_timeout(cfg.timeout_ms)
        ctx.tracing.start(screenshots=True, snapshots=True)
        page = ctx.new_page()
        shot = Shots(page, cfg.out_dir)
        try:
            login(page, cfg, shot)
            sla = open_sla_report(page, cfg, shot)
            open_missed_sla(page, shot)
            breakdown = parse_breakdown(page)
            log.info("Missed SLA breakdown: %s", breakdown)
            records = read_table_all_pages(page, r"^\s*Unprotected servers", shot)
            jobs = collect_failed_jobs(page, shot) if cfg.collect_jobs else []
            ctx.tracing.stop()  # success: discard trace
        except Exception:
            shot("error")
            ctx.tracing.stop(path=str(cfg.out_dir / "trace.zip"))  # open with: playwright show-trace
            log.exception("Automation failed - see %s", cfg.out_dir)
            return 1
        finally:
            browser.close()

    actionable, ignored = classify(records, cfg)

    # cross-check table vs legend for "No Schedule"
    legend_ns = next((v for k, v in breakdown.items() if "no schedule" in k.lower()), None)
    if legend_ns is not None and legend_ns != len(ignored):
        log.warning("Legend says %d 'No Schedule' but table has %d", legend_ns, len(ignored))

    summary = {
        "run_time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "sla": sla,
        "breakdown": breakdown,
        "missed_total": len(records),
        "actionable": len(actionable),
        "ignored_no_schedule": len(ignored),
        "jobs_collected": cfg.collect_jobs,
        "failed_jobs": len(jobs),
    }
    (cfg.out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    write_csv(cfg.out_dir / "missed_sla_actionable.csv", actionable)
    write_csv(cfg.out_dir / "missed_sla_no_schedule.csv", ignored)
    write_csv(cfg.out_dir / "missed_sla_all.csv", records)
    if cfg.collect_jobs:
        write_csv(cfg.out_dir / "failed_jobs.csv", jobs)
    report = build_html(summary, actionable, ignored, jobs)
    (cfg.out_dir / "report.html").write_text(report, encoding="utf-8")

    log.info("DONE  missed=%d  actionable=%d  no-schedule=%d  -> %s",
             len(records), len(actionable), len(ignored), cfg.out_dir)
    if cfg.send_mail:
        subject = (f"[Commvault] Daily SLA summary {summary['run_time'][:10]} - "
                   f"{len(actionable)} missed SLA(s) need attention")
        try:
            send_mail(cfg.mail, subject, report, [cfg.out_dir / "missed_sla_actionable.csv"])
        except Exception:
            log.exception("E-mail failed (report files are still saved)")
    return 2 if actionable else 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.json", help="JSON file with all non-secret settings")
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--out-dir", default="reports")
    ap.add_argument("--headed", action="store_true", help="show the browser (use while debugging)")
    ap.add_argument("--slow-mo", type=int, default=0, help="ms delay between actions (with --headed)")
    ap.add_argument("--no-email", action="store_true")
    ap.add_argument("--collect-jobs", action="store_true", help="also list failed jobs from Monitor > Jobs (best effort)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s")
    sys.exit(run(build_config(args)))


if __name__ == "__main__":
    main()
