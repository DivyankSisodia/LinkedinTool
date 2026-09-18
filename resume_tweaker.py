#!/usr/bin/env python3
"""
Resume Tweaker (Spec 002) — group analyzed jobs, suggest tweaks, rewrite resume via Groq free tier.

Models (defaults, see specs/002-resume-tweaker/spec.md §5):
  REWRITE = openai/gpt-oss-120b (fallback llama-3.3-70b-versatile)
  GROUP   = llama-3.1-8b-instant (fallback openai/gpt-oss-20b)

Usage:
  python resume_tweaker.py group | suggest | review | rewrite | run [--group ID ...]
"""
import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = "jobs.db"
RESUME_DIR = Path("resume")
BASE_RESUME = RESUME_DIR / "base_resume.md"
PROMPTS_DIR = Path("prompts")

try:  # .env next to this file; explicit path (bare load_dotenv can pick a stray global .env)
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

# Live Groq catalog verified 2026-09-19 (GET /openai/v1/models): Llama models
# are gone; GPT-OSS carries both roles. Override via env if catalog changes.
REWRITE_MODEL = os.getenv("GROQ_REWRITE_MODEL", "openai/gpt-oss-120b")
GROUP_MODEL = os.getenv("GROQ_GROUP_MODEL", "openai/gpt-oss-20b")
REWRITE_FALLBACK = "openai/gpt-oss-20b"
GROUP_FALLBACK = "qwen/qwen3.8-27b"


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS job_groups (
            group_id TEXT PRIMARY KEY, label TEXT NOT NULL,
            job_urls TEXT NOT NULL, top_skills TEXT NOT NULL,
            top_gaps TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tweak_suggestions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id TEXT NOT NULL REFERENCES job_groups(group_id),
            suggestion_json TEXT NOT NULL, user_decision TEXT,
            user_text TEXT, decided_at TEXT, UNIQUE(group_id)
        );
        CREATE TABLE IF NOT EXISTS resume_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id TEXT NOT NULL REFERENCES job_groups(group_id),
            base_hash TEXT NOT NULL, variant_path TEXT NOT NULL,
            diff_json TEXT NOT NULL, model TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
    """)
    conn.commit()


def groq_json(model: str, system: str, user: str, max_tokens: int = 1500,
              fallback: str | None = None) -> dict:
    """Call Groq chat-completions in JSON mode with backoff + fallback. No key logging."""
    try:
        from groq import Groq
    except ImportError:
        sys.exit("Missing dependency: pip install groq>=0.9.0")
    if not os.getenv("GROQ_API_KEY"):
        sys.exit("GROQ_API_KEY not set. Copy .env.example to .env and add your key.")
    client = Groq()  # reads GROQ_API_KEY from env
    last_err = None
    attempt_tokens = max_tokens
    for attempt, wait in enumerate([0, 2, 4, 8]):
        if wait:
            time.sleep(wait)
        try:
            resp = client.chat.completions.create(
                model=model, temperature=0, max_tokens=attempt_tokens,
                response_format={"type": "json_object"},
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
            )
            return json.loads(resp.choices[0].message.content)
        except Exception as e:
            last_err = e
            msg = str(e).lower()
            if "413" in str(e) or "too large" in msg or "reduce your message" in msg:
                break  # input-size error: retrying the same payload is pointless
            if "output tokens" in msg or "otpm" in msg:
                # Output-per-minute budget exceeded: shrink and retry same model.
                attempt_tokens = max(256, attempt_tokens // 2)
                continue
            if "429" not in str(e) and "rate" not in msg:
                break
    if fallback and last_err is not None:
        msg = str(last_err).lower()
        if "413" in str(last_err) or "too large" in msg or "reduce your message" in msg:
            raise RuntimeError(
                f"Groq call failed (payload too large for {model}, "
                f"fallback would fail too): {last_err}")
        print(f"Primary {model} failed ({last_err}); falling back to {fallback}")
        return groq_json(fallback, system, user, attempt_tokens, fallback=None)
    raise RuntimeError(f"Groq call failed: {last_err}")


def base_hash() -> str:
    return hashlib.sha256(BASE_RESUME.read_bytes()).hexdigest() if BASE_RESUME.exists() else "missing"


GROUP_CHUNK = 20  # jobs per grouping call; keeps payloads under per-minute token limits


def _clip(s, n):
    return (s or "")[:n]


def cmd_group(conn, args) -> None:
    rows = conn.execute("""
        SELECT j.url, a.title, a.skills, a.requirements, a.experience
        FROM jobs j JOIN job_analysis a ON a.job_url = j.url
        LIMIT ?""", (args.limit,)).fetchall()
    if not rows:
        print("No analyzed jobs. Run analyze_jobs.py first.")
        return
    jobs_payload = [{"url": u, "title": _clip(t, 200), "skills": _clip(s, 800),
                     "requirements": _clip(r, 800), "experience": _clip(e, 100)}
                    for u, t, s, r, e in rows]
    system = load_prompt("group_v1.txt")
    merged = {}  # group_id -> {label, job_urls set, top_skills set}
    for i in range(0, len(jobs_payload), GROUP_CHUNK):
        chunk = jobs_payload[i:i + GROUP_CHUNK]
        user = f"MIN_SIZE={args.min_size}\nJOBS:\n" + json.dumps(chunk)
        data = groq_json(GROUP_MODEL, system, user, max_tokens=900,
                         fallback=GROUP_FALLBACK)
        for g in data.get("groups", []):
            gid = g.get("group_id")
            if not gid:
                continue
            m = merged.setdefault(gid, {"label": g.get("label", gid),
                                        "job_urls": set(), "top_skills": set()})
            m["job_urls"].update(g.get("job_urls", []))
            m["top_skills"].update(g.get("top_skills", []))
        print(f"  chunk {i // GROUP_CHUNK + 1}: {len(chunk)} jobs -> "
              f"{len(data.get('groups', []))} groups")
    now = datetime.now(timezone.utc).isoformat()
    for gid, m in merged.items():
        conn.execute("""INSERT OR REPLACE INTO job_groups
            (group_id, label, job_urls, top_skills, top_gaps, created_at)
            VALUES (?,?,?,?,?,?)""",
            (gid, m["label"], json.dumps(sorted(m["job_urls"])),
             json.dumps(sorted(m["top_skills"])), json.dumps([]), now))
    conn.commit()
    print(f"Grouped {len(rows)} jobs into {len(merged)} groups.")


def cmd_suggest(conn, args) -> None:
    groups = [args.group] if args.group else [r[0] for r in conn.execute("SELECT group_id FROM job_groups")]
    if not BASE_RESUME.exists():
        sys.exit(f"Missing {BASE_RESUME}. Create resume/base_resume.md first.")
    system = load_prompt("suggest_v1.txt")
    resume_md = BASE_RESUME.read_text(encoding="utf-8")[:20000]
    for gid in groups:
        row = conn.execute("SELECT label, job_urls, top_skills FROM job_groups WHERE group_id=?", (gid,)).fetchone()
        if not row:
            print(f"Unknown group {gid}; skipping"); continue
        user = f"GROUP={gid} ({row[0]})\nSKILLS={row[2]}\nJOBS={row[1][:4000]}\nBASE_RESUME:\n{resume_md}"
        data = groq_json(REWRITE_MODEL, system, user, max_tokens=1500, fallback=REWRITE_FALLBACK)
        conn.execute("""INSERT OR REPLACE INTO tweak_suggestions (group_id, suggestion_json, user_decision)
            VALUES (?,?,NULL)""", (gid, json.dumps(data, ensure_ascii=False)))
        conn.commit()
        print(f"[{gid}] suggestion stored ({len(data.get('add', []))} adds, {len(data.get('rephrase', []))} rephrases)")


def cmd_review(conn, args) -> None:
    gid = args.group or sys.exit("--group required for review")
    row = conn.execute("SELECT suggestion_json FROM tweak_suggestions WHERE group_id=?", (gid,)).fetchone()
    if not row:
        sys.exit(f"No suggestion for {gid}. Run suggest first.")
    print(json.dumps(json.loads(row[0]), indent=2, ensure_ascii=False))
    decision = args.decision or input("[accept/edit/custom/skip]? ").strip().lower()
    text = args.text or (input("Your tweak text (empty=none): ").strip() if decision in ("edit", "custom") else None)
    if decision not in ("accept", "edit", "custom", "skip"):
        sys.exit("Decision must be accept|edit|custom|skip")
    conn.execute("""UPDATE tweak_suggestions SET user_decision=?, user_text=?, decided_at=?
        WHERE group_id=?""", (decision, text, datetime.now(timezone.utc).isoformat(), gid))
    conn.commit()
    print(f"[{gid}] decision={decision} stored.")


def cmd_rewrite(conn, args) -> None:
    groups = [args.group] if args.group else [r[0] for r in conn.execute(
        "SELECT group_id FROM tweak_suggestions WHERE user_decision IN ('accept','edit','custom')")]
    system = load_prompt("rewrite_v1.txt")
    resume_md = BASE_RESUME.read_text(encoding="utf-8")[:20000]
    RESUME_DIR.mkdir(exist_ok=True)
    for gid in groups:
        row = conn.execute("SELECT suggestion_json, user_decision, user_text FROM tweak_suggestions WHERE group_id=?",
                           (gid,)).fetchone()
        if not row or row[1] == "skip":
            continue
        bh = base_hash()
        if not args.force and conn.execute(
                "SELECT 1 FROM resume_versions WHERE group_id=? AND base_hash=?", (gid, bh)).fetchone():
            print(f"[{gid}] up-to-date; use --force to regenerate"); continue
        user = (f"GROUP={gid}\nDECISION={row[1]}\nUSER_TEXT={row[2] or ''}\n"
                f"SUGGESTION={row[0][:8000]}\nBASE_RESUME:\n{resume_md}")
        data = groq_json(REWRITE_MODEL, system, user, max_tokens=4000, fallback=REWRITE_FALLBACK)
        out = RESUME_DIR / f"variant_{gid}.md"
        out.write_text(data.get("resume_markdown", ""), encoding="utf-8")
        conn.execute("""INSERT INTO resume_versions (group_id, base_hash, variant_path, diff_json, model, created_at)
            VALUES (?,?,?,?,?,?)""", (gid, bh, str(out),
            json.dumps(data.get("diff", []), ensure_ascii=False),
            REWRITE_MODEL, datetime.now(timezone.utc).isoformat()))
        conn.commit()
        print(f"[{gid}] wrote {out}")


def main() -> None:
    p = argparse.ArgumentParser(description="Resume Tweaker (Spec 002, Groq free tier)")
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("group"); g.add_argument("--min-size", type=int, default=3); g.add_argument("--limit", type=int, default=100)
    s = sub.add_parser("suggest"); s.add_argument("--group", default=None)
    r = sub.add_parser("review"); r.add_argument("--group", required=True)
    r.add_argument("--decision", choices=["accept", "edit", "custom", "skip"], default=None)
    r.add_argument("--text", default=None)
    w = sub.add_parser("rewrite"); w.add_argument("--group", default=None); w.add_argument("--force", action="store_true")
    u = sub.add_parser("run"); u.add_argument("--min-size", type=int, default=3); u.add_argument("--limit", type=int, default=100)
    args = p.parse_args()
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    if args.cmd == "group":
        cmd_group(conn, args)
    elif args.cmd == "suggest":
        cmd_suggest(conn, args)
    elif args.cmd == "review":
        cmd_review(conn, args)
    elif args.cmd == "rewrite":
        cmd_rewrite(conn, args)
    elif args.cmd == "run":
        cmd_group(conn, args)
        cmd_suggest(conn, type("A", (), {"group": None})())
        print("Review each group: python resume_tweaker.py review --group <id>")
    conn.close()


if __name__ == "__main__":
    main()
