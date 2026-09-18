# GEMINI.md — LinkedinTool + Resume Tweaker

> Gemini / generic-agent mirror of `AGENTS.md`. If they conflict, `specs/002-resume-tweaker/spec.md` wins.

## Context
- Scraper: `scrape_ml_jobs.py` → `jobs` table in `jobs.db`.
- Analysis: `analyze_jobs.py` (Ollama `qwen2.5:7b`) → `job_analysis` table.
- Next feature: `resume_tweaker.py` (Groq Cloud, free tier) → group jobs → suggest tweaks → user review → rewrite resume variants.

## Instructions for Gemini
1. Read `specs/002-resume-tweaker/spec.md` and `docs/resume-tweaker-architecture.md` before coding.
2. Use Groq models: rewrite `openai/gpt-oss-120b`, group `openai/gpt-oss-20b`, fallback `qwen/qwen3.8-27b`. JSON mode, `temperature=0`.
3. Keep `jobs` / `job_analysis` schema intact; add `job_groups`, `tweak_suggestions`, `resume_versions` per spec §6.
4. Implement CLI `group | suggest | review | rewrite | run` exactly as spec §7.
5. Review decisions are authoritative; never overwrite `user_text`.
6. Never output or commit `GROQ_API_KEY`. Read from environment.
7. Validate offline with mocked tests; do not call live Groq in tests.
