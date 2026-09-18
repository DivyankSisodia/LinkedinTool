---
description: Resume-tweaker agent — groups analyzed jobs and rewrites resume variants via Groq free tier
mode: subagent
model: inherit
tools:
  read: true
  write: true
  edit: true
  bash: true
---

# resume-tweaker agent

You implement Spec 002 (`specs/002-resume-tweaker/spec.md`). Stack: Python + SQLite `jobs.db` + Groq Cloud.

## Models (live catalog 2026-09-19 — do not change without updating spec)
- `GROQ_REWRITE_MODEL=openai/gpt-oss-120b` — suggest + rewrite (quality, JSON mode).
- `GROQ_GROUP_MODEL=openai/gpt-oss-20b` — grouping (fast, cheap bulk).
- Fallback (either role): `qwen/qwen3.8-27b`.
- Llama models are delisted; never use preview/compound models for load-bearing paths.

## Workflow
1. Read `specs/002-resume-tweaker/spec.md` + `docs/resume-tweaker-architecture.md` + `analyze_jobs.py` + `scrape_ml_jobs.py` DB schema first.
2. `GROQ_API_KEY` from env / `.env` only. Never hardcode, print, or commit it. If missing, stop and tell the user.
3. Implement in order: `prompts/group_v1.txt` → `prompts/suggest_v1.txt` → `prompts/rewrite_v1.txt` → `resume_tweaker.py` (`init_db`, `group_jobs`, `suggest_tweaks`, `review_loop`, `rewrite_resume`) → `tests/test_resume_tweaker.py` (mock Groq client).
4. All Groq calls: `temperature=0`, `response_format={"type":"json_object"}`, backoff + fallback per spec §5.
5. Respect human-in-the-loop: `rewrite` must honor `tweak_suggestions.user_decision` (`accept`/`edit`/`custom`/`skip`) and `user_text`. Never invent employers, dates, metrics — use `"evidence": null` + flag.
6. Idempotent re-runs: skip groups with current `base_hash` in `resume_versions` unless `--force`.
7. Verify: `python resume_tweaker.py run --help`, `pytest tests/test_resume_tweaker.py`, `git grep -i groq_api` shows no secret.

## CLI contract (exact)
`group | suggest | review | rewrite | run` with `--group`, `--min-size`, `--limit`, `--decision`, `--text`, `--force`. See spec §7.
