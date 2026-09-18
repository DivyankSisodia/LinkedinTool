# Runbook — Scraper + AI Features

End-to-end: LinkedIn scrape → local job analysis (Ollama) → resume tweaker (Groq).
Source of truth: `specs/002-resume-tweaker/spec.md`.

## 1. Prerequisites

```powershell
# Python 3.10+ + venv
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Playwright browser (required once)
.\.venv\Scripts\playwright.exe install chromium

# Ollama (local analysis only)
winget install Ollama.Ollama
ollama pull qwen2.5:7b

# Groq (resume tweaker only) — key at https://console.groq.com
Copy-Item .env.example .env
# edit .env: GROQ_API_KEY=gsk_...  +  LINKEDIN_EMAIL / LINKEDIN_PASSWORD
```

Verify:
```powershell
.\.venv\Scripts\python.exe resume_tweaker.py --help
ollama list
```

## 2. LinkedIn session (once)

LinkedIn needs auth. Create `linkedin_session.json` via manual login sample:

```powershell
.\.venv\Scripts\python.exe samples/create_session.py
# browser opens → log in manually → session saved
```

If login fails, delete `linkedin_session.json` and retry. Do not commit this file.

## 3. Run scraper

```powershell
.\.venv\Scripts\python.exe scrape_ml_jobs.py
```

* Config at top of `scrape_ml_jobs.py`: `KEYWORDS`, `LOCATION="India"`, `TIME_FILTER="r86400"` (24h), `SEARCH_LIMIT/MAX_TOTAL=100`, `MIN_EMPLOYEES=100`.
* Writes to `jobs.db` table `jobs`, flushes every 20 rows, 8–15s delay between pages (human-like).
* On `CHALLENGE` (security check): waits 60s and continues. If repeated, stop, re-login, retry later.

Check output:
```powershell
.\.venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect('jobs.db'); print(c.execute('select count(*) from jobs').fetchone())"
```

## 4. Run job analysis (Ollama, local, free)

```powershell
ollama serve   # in separate terminal if not running
.\.venv\Scripts\python.exe analyze_jobs.py
```

* Uses `MODEL=qwen2.5:7b` in `analyze_jobs.py` (RTX 4060 8GB sweet spot, ~50-70 tok/s on GPU).
* Extracts `title/location/salary/skills/requirements/experience` → `job_analysis` table.
* Skips already-analyzed URLs. Tune `MAX_DESC_CHARS=8000`, `num_ctx=4096` if VRAM-bound.
* If slow (~5 tok/s): Ollama is on CPU — see `ollama ps` → must say `100% GPU`. Restart `ollama serve` after driver update.

## 5. Run resume tweaker (Groq Cloud, free tier)

```powershell
# 1. Base resume (required)
mkdir resume -Force
# create resume/base_resume.md with Summary/Skills/Experience/Projects/Education

# 2. Full pipeline
.\.venv\Scripts\python.exe resume_tweaker.py run

# Or step-by-step:
.\.venv\Scripts\python.exe resume_tweaker.py group --limit 100 --min-size 3
.\.venv\Scripts\python.exe resume_tweaker.py suggest --group genai-llm-rag
.\.venv\Scripts\python.exe resume_tweaker.py review --group genai-llm-rag
# interactive: accept | edit | custom | skip
.\.venv\Scripts\python.exe resume_tweaker.py rewrite --group genai-llm-rag
```

* Models: rewrite `openai/gpt-oss-120b`, group `llama-3.1-8b-instant` (see `AGENTS.md`). Override via `GROQ_REWRITE_MODEL` / `GROQ_GROUP_MODEL` in `.env`.
* Output: `resume/variant_<group>.md` + rows in `job_groups`, `tweak_suggestions`, `resume_versions`.
* Re-runs are idempotent (skips current `base_hash` unless `--force`).
* Non-interactive review: `--decision accept --text "..."`.

## 6. Tests

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_resume_tweaker.py -q
```

Mocked Groq client — no network, no key needed.

## 7. Troubleshooting

| Symptom | Fix |
|---|---|
| `playwright … Executable doesn't exist` | `.\.venv\Scripts\playwright.exe install chromium` |
| `linkedin_session.json` expired / authwall | rerun `samples/create_session.py` |
| Ollama `5 tok/s`, `ollama ps` = CPU | quit tray → `ollama serve`, free VRAM, `nvidia-smi` must show ollama process |
| Groq `429` | backoff auto-retries then falls back (120b→70b, 8b→20b); wait, reduce `--limit` |
| `GROQ_API_KEY not set` | `.env` missing or not loaded; `Copy-Item .env.example .env` |
| `jobs.db` locked | close other python processes / DB viewers |

## 8. File map

* `scrape_ml_jobs.py` → `jobs`
* `analyze_jobs.py` → `job_analysis`
* `resume_tweaker.py` + `prompts/*` → `job_groups` → `tweak_suggestions` → `resume/variant_*.md`
* `specs/002-resume-tweaker/spec.md` — contract; `docs/resume-tweaker-architecture.md` — graphs
