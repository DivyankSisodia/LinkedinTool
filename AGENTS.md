# AGENTS.md — LinkedinTool + Resume Tweaker

> Generic agent file respected by opencode, Cursor, Gemini, Codex, Copilot. Tool-specific configs mirror this: `.opencode/agent/resume-tweaker.md`, `.cursor/rules/resume-tweaker.mdc`, `GEMINI.md`.

## Project
Async LinkedIn scraper (Playwright) → SQLite `jobs.db` → local LLM analysis (`analyze_jobs.py`, Ollama `qwen2.5:7b`) → **Resume Tweaker (Spec 002, Groq Cloud)**.

## Source of truth
1. `specs/002-resume-tweaker/spec.md` — CLI contract, DB migration, prompts, acceptance criteria.
2. `docs/resume-tweaker-architecture.md` — mermaid flow + state machine + fallback routing.
3. `analyze_jobs.py` / `scrape_ml_jobs.py` — existing `jobs` / `job_analysis` schema. Do not break.

## Groq free-tier policy (live catalog 2026-09-19)
- `GROQ_REWRITE_MODEL=openai/gpt-oss-120b` — suggest + rewrite. 131k ctx, ~500 tok/s.
- `GROQ_GROUP_MODEL=openai/gpt-oss-20b` — grouping. 131k ctx, ~1000 tok/s.
- Fallback (either role): `qwen/qwen3.8-27b`. Llama models are delisted — do not use.
- Call pattern: `temperature=0`, `response_format={"type":"json_object"}`, 429 → backoff 2s/4s/8s → fallback.
- Live catalog: `GET https://api.groq.com/openai/v1/models`.

## Rules for agents
- `GROQ_API_KEY` from env/`.env` only. Never hardcode, log, or commit. `git grep -i groq_api` must show placeholders only.
- Prompts versioned in `prompts/`. Code in `resume_tweaker.py`. Tests in `tests/test_resume_tweaker.py` (mocked, offline).
- Human-in-the-loop: `review` decisions (`accept`/`edit`/`custom`/`skip` + `user_text`) are authoritative for `rewrite`.
- No fake employers, dates, metrics. Gaps flagged explicitly.
- Verify with: `pytest tests/test_resume_tweaker.py` and `python resume_tweaker.py --help`.

## Commands
```powershell
winget install Ollama.Ollama
ollama pull qwen2.5:7b
pip install -r requirements.txt   # includes groq>=0.9.0, python-dotenv
Copy-Item .env.example .env       # then set GROQ_API_KEY + LINKEDIN_*
python analyze_jobs.py
python resume_tweaker.py run
```
