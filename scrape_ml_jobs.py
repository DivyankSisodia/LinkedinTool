#!/usr/bin/env python3
"""
LinkedIn Job Scraper — works with LinkedIn's 2026 obfuscated class names.
Scrapes ML/AI roles, last 24h, prints JSON, pushes to SQLite every 20 records.
"""
import asyncio
import json
import random
import re
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlencode

from linkedin_scraper.core.browser import BrowserManager


# ── Config ──────────────────────────────────────────────────────────────────
# Single combined OR query (matches your LinkedIn search URL)
KEYWORDS = (
    '("Machine Learning" OR "ML Engineer" OR "GenAI" OR "Generative AI" '
    'OR "LLM" OR "NLP" OR "AI Engineer" OR "Applied Scientist" OR "MLOps")'
)
LOCATION = "India"       # "" = no location filter (worldwide); e.g. "India"
EXPERIENCE = ""          # "" = no experience filter; "2,3" = Entry+Associate
TIME_FILTER = "r86400"   # f_TPR: r86400=24h, r604800=7d, r2592000=30d; "" = any time
SEARCH_LIMIT = 100       # URLs from the combined search
MAX_TOTAL = 100          # safety cap on detail scrapes
MIN_EMPLOYEES = 100      # skip companies with fewer than this many employees
BATCH_SIZE = 20
DELAY_MIN = 8            # min seconds between detail scrapes (human-like)
DELAY_MAX = 15           # max seconds between detail scrapes
NAV_TIMEOUT = 30000      # ms timeout for a single page navigation
CHALLENGE_BACKOFF = 60   # seconds to wait when LinkedIn serves a security check
DB_PATH = "jobs.db"
SESSION_FILE = "linkedin_session.json"


class ChallengeError(Exception):
    """Raised when LinkedIn serves a bot-detection / security-check page."""


# ── DB helpers ──────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            url TEXT PRIMARY KEY,
            title TEXT,
            company TEXT,
            location TEXT,
            posted_date TEXT,
            applicants TEXT,
            employment_type TEXT,
            description TEXT,
            keyword TEXT,
            scraped_at TEXT
        )
    """)
    conn.commit()
    return conn


def flush(conn, batch):
    if not batch:
        return 0
    cur = conn.executemany("""
        INSERT OR IGNORE INTO jobs
        (url, title, company, location, posted_date, applicants,
         employment_type, description, keyword, scraped_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, batch)
    conn.commit()
    return cur.rowcount


# ── Employee count helper ───────────────────────────────────────────────────
def employee_range(text):
    """Parse a company-size string like '51-200 employees' -> (51, 200).
    Returns None if not found. '+'-ranges get an infinite upper bound."""
    if not text:
        return None
    m = re.search(r'(\d[\d,]*)\s*(?:[-–—]\s*(\d[\d,]*))?', text)
    if not m:
        return None
    lo = int(m.group(1).replace(',', ''))
    hi = int(m.group(2).replace(',', '')) if m.group(2) else lo
    if '+' in text:
        hi = float('inf')
    return (lo, hi)


async def human_delay():
    """Random human-like pause between scrapes to avoid triggering LinkedIn's bot detection."""
    await asyncio.sleep(random.uniform(DELAY_MIN, DELAY_MAX))


async def is_challenge_page(page):
    """Return True if the current page is a LinkedIn security/checkpoint page."""
    try:
        url = page.url
        if any(k in url for k in ("checkpoint", "challenge", "authwall", "/login", "captcha")):
            return True
        text = await page.evaluate(
            "() => document.body ? document.body.innerText.slice(0, 2000) : ''"
        )
        return any(s in text for s in (
            "Let's do a quick security check",
            "Verify you're a human",
            "unusual activity",
            "Pardon the interruption",
            "security verification",
        ))
    except Exception:
        return False


# ── Scraper using JS evaluation (immune to class name changes) ─────────────
async def scrape_job_details(page, url):
    """Navigate to a job page and extract data via JS DOM traversal."""
    await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
    await page.wait_for_timeout(3000)

    if await is_challenge_page(page):
        raise ChallengeError("LinkedIn security check / challenge page detected")

    data = await page.evaluate("""
    () => {
        const body = document.body.innerText;
        const result = {title: null, company: null, location: null,
                        posted: null, applicants: null, emp_type: null,
                        description: null, employees: null, headcount: null};

        // --- Company: first link to /company/ ---
        const coLinks = [...document.querySelectorAll('a[href*="/company/"]')];
        for (const a of coLinks) {
            const t = a.innerText.trim();
            if (t && t.length > 1 && !t.startsWith('Show more')) {
                result.company = t.split('\\n')[0].trim();
                break;
            }
        }

        // --- Company size, self-reported (e.g. "11-50 employees") ---
        const empMatch = body.match(/(\\d[\\d,]*(?:\\s*[-–—]\\s*\\d[\\d,]*)?)\\s*\\+?\\s+employees/i);
        if (empMatch) result.employees = empMatch[0].trim();

        // --- Real headcount from the hiring-insights chart ---
        // The Y-axis line reads "Data ranges from <lo> to <hi>" (hi = latest count).
        // The X-axis line has dates ("2024-09-01 ...") so it won't match [\\d,]+.
        const rangeMatch = body.match(/Data ranges from ([\\d,]+) to ([\\d,]+)/);
        if (rangeMatch) result.headcount = parseInt(rangeMatch[2].replace(/,/g, ''), 10);

        // --- Title: h1 first, then document.title fallback ---
        // 2026 layout renders the title in an obfuscated <p> (no h1 on page),
        // but document.title is reliable: "<Title> | <Company> | LinkedIn".
        let rawTitle = null;
        const h1 = document.querySelector('h1');
        if (h1) rawTitle = h1.innerText.trim().split('\\n')[0].trim();
        if (!rawTitle) {
            const dt = (document.title || '').split('|')[0].trim();
            if (dt) rawTitle = dt;
        }
        result.title = (rawTitle && !/^(Join LinkedIn|Join now|Sign in|Sign up|Log in|LinkedIn|Feed|Jobs)$/i.test(rawTitle))
            ? rawTitle : null;

        // --- Location, Posted, Applicants from page text ---
        // New 2026 layout (no "·" separator):
        //   "<title>" then "<Company>  <Location>" then "<posted>  <applicants>"
        // Old layout fallback: "Location · Posted · Applicants"
        const lines = body.split('\\n').map(l => l.trim()).filter(Boolean);

        // Third fallback: 2026 layout puts company on one line, title on the next.
        const titleSkip = /^(Apply|Save|Remote|On-site|Hybrid|Full-time|Part-time|Contract|Internship|Promoted|Try Premium|Use AI|Get AI-powered|Show match|Tailor my|Help me|About the job|People you can reach|Meet the hiring|Message|Home|My Network|Jobs|Messaging|Notifications|Me|For Business)/i;
        if (!result.title && result.company) {
            const coIdx = lines.findIndex(l =>
                l === result.company || l.startsWith(result.company + ' ')
            );
            if (coIdx >= 0 && coIdx + 1 < lines.length) {
                const candidate = lines[coIdx + 1];
                if (candidate && !candidate.includes('·') && !titleSkip.test(candidate)) {
                    result.title = candidate;
                }
            }
        }

        const dotIdx = lines.findIndex(l => l.includes('·'));
        if (dotIdx >= 0) {
            const parts = lines[dotIdx].split('·').map(s => s.trim());
            result.location = parts[0] || null;
            result.posted = parts[1] || null;
            result.applicants = parts[2] || null;
        }

        const titleIdx = result.title ? lines.findIndex(l => l === result.title) : -1;
        if (titleIdx >= 0) {
            const locParts = (lines[titleIdx + 1] || '')
                .split(/\\s{2,}/).map(s => s.trim()).filter(Boolean);
            if (locParts.length >= 2 && !result.location) {
                result.location = locParts[locParts.length - 1];
            }
            const metaParts = (lines[titleIdx + 2] || '')
                .split(/\\s{2,}/).map(s => s.trim()).filter(Boolean);
            if (metaParts.length >= 2) {
                if (!result.posted) result.posted = metaParts[0];
                if (!result.applicants) result.applicants = metaParts[1];
            }
        }

        // --- Employment type (Remote/On-site/Hybrid, Full-time/Part-time) ---
        const etype = lines.find(l =>
            /^(Remote|On-site|Hybrid|Full-time|Part-time|Contract|Internship)/i.test(l));
        if (etype) result.emp_type = etype;

        // --- Description ---
        // LinkedIn clamps long descriptions behind a "Show more" toggle.
        // Expand it, then read textContent (innerText hides clamped text).
        const descEl = document.querySelector('[class*="description"]');
        let descText = null;
        if (descEl) {
            for (const b of descEl.querySelectorAll('button')) {
                if (/see more|show more/i.test(b.innerText)) {
                    try { b.click(); } catch (e) {}
                }
            }
            descText = descEl.textContent;
        }

        // Fallback: text between a heading and the insights section
        if (!descText) {
            let ds = -1;
            for (const m of ['About the job', 'Job Summary', 'Job description', 'Description']) {
                const i = body.indexOf(m);
                if (i >= 0) { ds = i + m.length; break; }
            }
            if (ds < 0) ds = 0;
            let de = -1;
            for (const m of ['Seniority level', 'Set alert for similar jobs',
                             'About the company', 'employee growth']) {
                const i = body.indexOf(m, ds);
                if (i > ds) { de = i; break; }
            }
            if (de < 0) de = ds + 20000;
            descText = body.substring(ds, de);
        }

        if (descText) {
            descText = descText.trim();
            // strip leading heading labels like "Description -" / "Job Summary"
            descText = descText.replace(/^Description\\s*-\\s*/i, '');
            descText = descText.replace(/^Job Summary\\s*/i, '');
            descText = descText.replace(/^About the job\\s*/i, '');
            // drop the trailing "Show more" toggle label
            descText = descText.replace(/\\s*Show more\\s*$/i, '');
            result.description = descText.trim().substring(0, 20000);
        }

        return result;
    }
    """)

    return data


async def _collect_current_page_urls(page, seen):
    """Scroll the results pane + accumulate /jobs/view/ URLs into seen."""
    before = len(seen)
    stagnant = 0
    for _ in range(8):
        await page.evaluate("""
            () => {
                const links = [...document.querySelectorAll('a[href*="/jobs/view/"]')];
                if (links.length) links[links.length - 1].scrollIntoView({block: 'end'});
                const lists = [...document.querySelectorAll('ul, div')]
                    .filter(d => d.querySelectorAll('a[href*="/jobs/view/"]').length > 3
                             && d.scrollHeight > d.clientHeight + 100);
                const list = lists.sort((a,b) =>
                    b.querySelectorAll('a[href*="/jobs/view/"]').length -
                    a.querySelectorAll('a[href*="/jobs/view/"]').length)[0];
                if (list) list.scrollTo(0, list.scrollHeight);
                window.scrollTo(0, document.body.scrollHeight);
            }
        """)
        await page.wait_for_timeout(1200)
        links = await page.evaluate(
            "() => [...document.querySelectorAll('a[href*=\"/jobs/view/\"]')].map(a => a.href.split('?')[0])"
        )
        seen.update(links)
        if len(seen) == before:
            stagnant += 1
            if stagnant >= 3:
                break
        else:
            stagnant = 0
            before = len(seen)


async def _click_next_page(page):
    """Click LinkedIn's pagination Next / next page number. Returns False when on last page."""
    return await page.evaluate("""() => {
        const btns = [...document.querySelectorAll('button')];
        // Preferred: explicit Next button
        let next = btns.find(b => /^\\s*next\\s*$/i.test(b.innerText || '') && !b.disabled);
        if (next) { next.scrollIntoView({block: 'center'}); next.click(); return true; }
        // Fallback: numbered pages — click the page after the currently selected one
        const nums = btns.filter(b => /^\\s*\\d+\\s*$/.test(b.innerText || ''));
        if (!nums.length) return false;
        const cur = nums.findIndex(b => b.getAttribute('aria-current') === 'true'
            || /active|selected/i.test(b.className));
        const target = nums[cur >= 0 ? cur + 1 : 0];
        if (!target || target === nums[cur]) return false;
        target.scrollIntoView({block: 'center'}); target.click(); return true;
    }""")
async def search_urls(page, keywords, location, limit):
    """Search LinkedIn jobs, paginating (1, 2, 3 ... Next) — not just page 1."""
    params = {"keywords": keywords, "f_TPR": TIME_FILTER, "sortBy": "DD"}
    if location:
        params["location"] = location
    if EXPERIENCE:
        params["f_E"] = EXPERIENCE
    url = f"https://www.linkedin.com/jobs/search/?{urlencode(params)}"
    await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
    await page.wait_for_timeout(5000)

    # LinkedIn paginates (~25/page) + virtualizes cards per page.
    # Old code only scrolled page 1 (≈7-11 URLs) and quit — hence 7 vs 214.
    seen = set()
    max_pages = max(1, (limit // 25) + 3)
    for _ in range(max_pages):
        await _collect_current_page_urls(page, seen)
        if len(seen) >= limit:
            break
        try:
            moved = await _click_next_page(page)
        except Exception:
            break
        if not moved:
            break
        await page.wait_for_timeout(4000)

    return list(seen)[:limit]


async def main():
    conn = init_db()
    batch = []
    total_new = 0
    seen_urls = set()

    async with BrowserManager(headless=True) as browser:
        await browser.load_session(SESSION_FILE)
        # Warm session on feed before search/detail scrapes (reduces authwall hits).
        await browser.page.goto(
            "https://www.linkedin.com/feed/",
            wait_until="domcontentloaded",
            timeout=NAV_TIMEOUT,
        )
        await browser.page.wait_for_timeout(2000)
        if await is_challenge_page(browser.page):
            print("Session expired or blocked — rerun: python samples/create_session.py\n")
            conn.close()
            return
        print("Session loaded.\n")

        # ── Phase 1: Collect URLs from the combined OR search ──
        all_jobs = []  # (url, keyword)
        print("Searching combined query ...", end=" ")
        try:
            urls = await search_urls(browser.page, KEYWORDS, LOCATION, SEARCH_LIMIT)
        except Exception as e:
            print(f"FAILED: {e}")
            urls = []
        new = [u for u in urls if u not in seen_urls]
        seen_urls.update(new)
        all_jobs = [(u, KEYWORDS) for u in new]
        print(f"{len(urls)} found, {len(new)} new")

        all_jobs = all_jobs[:MAX_TOTAL]
        print(f"\nTotal unique URLs to scrape: {len(all_jobs)}\n")

        # ── Phase 2: Scrape details, batch to DB ──
        for i, (url, kw) in enumerate(all_jobs, 1):
            try:
                d = await scrape_job_details(browser.page, url)
            except ChallengeError as e:
                print(f"  [{i}] CHALLENGE: {e} — backing off {CHALLENGE_BACKOFF}s")
                await asyncio.sleep(CHALLENGE_BACKOFF)
                continue
            except Exception as e:
                print(f"  [{i}] SKIP {e}")
                await human_delay()
                continue

            if not d.get("title") or not d.get("company"):
                print(f"  [{i}] SKIP (no job data — authwall/empty page)")
                await human_delay()
                continue

            print(f"[{i}] {d.get('title','?')} @ {d.get('company','?')} | {kw}")

            # Skip companies below the employee threshold.
            # Prefer the real headcount from the insights chart; fall back to
            # the self-reported "N employees" range when the chart is absent.
            hc = d.get("headcount")
            if hc is not None:
                below = hc < MIN_EMPLOYEES
                size_label = f"{hc} employees"
            else:
                emp_range = employee_range(d.get("employees"))
                below = emp_range is not None and emp_range[1] < MIN_EMPLOYEES
                size_label = d.get("employees") or "unknown size"

            if below:
                print(f"  → SKIP (company size {size_label})")
                await human_delay()
                continue

            batch.append((
                url,
                d.get("title"),
                d.get("company"),
                d.get("location"),
                d.get("posted"),
                d.get("applicants"),
                d.get("emp_type"),
                d.get("description"),
                kw,
                datetime.now(timezone.utc).isoformat(),
            ))

            if len(batch) >= BATCH_SIZE:
                new_rows = flush(conn, batch)
                total_new += new_rows
                print(f"  → DB flush: {len(batch)} rows ({new_rows} new)")
                batch = []

            await human_delay()

        # Final flush
        if batch:
            new_rows = flush(conn, batch)
            total_new += new_rows
            print(f"  → Final flush: {len(batch)} rows ({new_rows} new)")

    # ── Print all results as JSON ──
    print("\n" + "=" * 70)
    print("ALL SCRAPED JOBS (JSON)")
    print("=" * 70)
    rows = conn.execute(
        "SELECT * FROM jobs ORDER BY scraped_at DESC"
    ).fetchall()
    cols = [d[0] for d in conn.execute("SELECT * FROM jobs LIMIT 0").description]
    jobs_json = [dict(zip(cols, r)) for r in rows]
    print(json.dumps(jobs_json, indent=2, ensure_ascii=False))

    conn.close()
    print(f"\nDone. {total_new} new rows in {DB_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
