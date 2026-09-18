#!/usr/bin/env python3
"""
Analyze scraped jobs with a small local LLM (Ollama).

Extracts structured fields from each job description:
  title, location, salary, skills, requirements, experience

Results are stored in a `job_analysis` table keyed by the job URL
(unique identifier linking back to the `jobs` table).

Requirements:
  - Ollama installed & running (https://ollama.com)
  - a small model pulled, e.g. `ollama pull qwen2.5:3b`
"""
import json
import sqlite3
import time
from datetime import datetime, timezone

import requests

# ── Config ──────────────────────────────────────────────────────────────────
OLLAMA_URL = "http://localhost:11434"
MODEL = "qwen2.5:3b"        # small CPU-friendly model; alts: llama3.2:3b, phi3:mini, gemma2:2b
DB_PATH = "jobs.db"
MAX_DESC_CHARS = 8000       # chars of description sent per job
TIMEOUT = 180               # seconds per model call
MAX_RETRIES = 3

SYSTEM_PROMPT = (
    "You are a precise job-posting parser. Extract structured fields from the "
    "job posting and return ONLY a valid JSON object with no extra text."
)

USER_TEMPLATE = """Extract the following fields from this job posting and return ONLY JSON with these keys:
- "title": job title (string)
- "location": job location (string or null)
- "salary": salary/compensation if mentioned (string or null)
- "skills": list of required skills and technologies (array of strings)
- "requirements": list of key requirements/qualifications (array of strings)
- "experience": required years of experience (string or null)

Title: {title}

Description:
{description}
"""


def init_analysis_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS job_analysis (
            job_url TEXT PRIMARY KEY,
            title TEXT,
            location TEXT,
            salary TEXT,
            skills TEXT,
            requirements TEXT,
            experience TEXT,
            analyzed_at TEXT,
            FOREIGN KEY (job_url) REFERENCES jobs(url)
        )
    """)
    conn.commit()


def analyze_job(title, description):
    """Send one job to the model, return a dict of extracted fields."""
    prompt = USER_TEMPLATE.format(
        title=title,
        description=(description or "")[:MAX_DESC_CHARS],
    )
    payload = {
        "model": MODEL,
        "system": SYSTEM_PROMPT,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {
            "temperature": 0,
            "num_ctx": 8192,     # enough context for long descriptions
            "num_predict": 2048, # hard cap on output so the model can't loop
        },
    }
    r = requests.post(f"{OLLAMA_URL}/api/generate", json=payload, timeout=TIMEOUT)
    r.raise_for_status()
    raw = r.json().get("response", "").strip()

    # strip markdown code fences if the model added them
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return json.loads(raw)


def main():
    conn = sqlite3.connect(DB_PATH)
    init_analysis_table(conn)

    rows = conn.execute("""
        SELECT url, title, description
        FROM jobs
        WHERE title IS NOT NULL AND description IS NOT NULL
          AND url NOT IN (SELECT job_url FROM job_analysis)
        ORDER BY scraped_at DESC
    """).fetchall()

    print(f"{len(rows)} jobs to analyze\n")

    for i, (url, title, desc) in enumerate(rows, 1):
        data = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                data = analyze_job(title, desc)
                break
            except Exception as e:
                print(f"  [{i}] attempt {attempt} failed: {e}")
                time.sleep(3)

        if not data:
            print(f"  [{i}] SKIP {title}")
            continue

        conn.execute("""
            INSERT OR REPLACE INTO job_analysis
            (job_url, title, location, salary, skills, requirements, experience, analyzed_at)
            VALUES (?,?,?,?,?,?,?,?)
        """, (
            url,
            data.get("title"),
            data.get("location"),
            data.get("salary"),
            json.dumps(data.get("skills") or [], ensure_ascii=False),
            json.dumps(data.get("requirements") or [], ensure_ascii=False),
            data.get("experience"),
            datetime.now(timezone.utc).isoformat(),
        ))
        conn.commit()
        print(f"  [{i}] OK  {data.get('title')} | {data.get('location')} | exp: {data.get('experience')}")

    conn.close()
    print("\nDone. Analysis stored in `job_analysis` table.")


if __name__ == "__main__":
    main()
