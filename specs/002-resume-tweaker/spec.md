# Spec 002 — Resume Tweaker (Groq-powered, post-job-analysis)

## 0. Status
- State: `draft` — next feature after `analyze_jobs.py` (job analysis)
- Owner: LinkedinTool repo
- Stack: Python 3.8+, SQLite (`jobs.db`), Groq Cloud API (free tier), no local GPU required

## 1. Goal
Go through `job_analysis` rows, **group jobs by resume-tweak needed**, propose
per-group resume edits, let the user **accept / edit / supply their own tweak**,
then **generate an updated resume version** per group.

Non-goals (v1):
- Auto-applying to jobs on LinkedIn.
- Cover-letter generation (future spec).
- PDF layout perfection (v1 = markdown + JSON diff; PDF export best-effort).

## 2. Context / Inputs

### 2.1 Existing tables (do not break)
`jobs` (from `scrape_ml_jobs.py:43-60`):
```
url TEXT PK, title, company, location, posted_date, applicants,
employment_type, description, keyword, scraped_at
```

`job_analysis` (from `analyze_jobs.py:50-64`):
```
job_url TEXT PK/FK -> jobs(url), title, location, salary,
skills TEXT (JSON array), requirements TEXT (JSON array),
experience, analyzed_at
```

### 2.2 New inputs
- `resume/base_resume.md` — single source of truth, user-maintained markdown
  (sections: Summary, Skills, Experience, Projects, Education).
- Optional `resume/base_resume.json` — structured mirror for reliable diffing.
- Env: `GROQ_API_KEY` (required), `GROQ_GROUP_MODEL`, `GROQ_REWRITE_MODEL`
  (optional overrides, see §5).

## 3. User stories
1. As a job seeker, I run one command and see job groups like
   `genai-llm-rag (12 jobs)`, `mlops (7 jobs)` with the top missing skills per group,
   so I know which resume variant to make.
2. As a user, I see a suggested bullet-level tweak per group
   (`ADD skill X with evidence Y`, `REPHRASE bullet Z`), I can type
   `accept`, `edit <my text>`, or `skip`, and the system respects my choice.
3. As a user, after I answer, I get `resume/variant_<group>.md` + a JSON diff,
   and the decision is stored so re-runs don't re-ask me.

## 4. Architecture (see `docs/resume-tweaker-architecture.md` for graph)
```
jobs + job_analysis
  -> group_jobs()      [Groq bulk model, embeddings-free clustering]
  -> suggest_tweaks()  [Groq quality model, JSON mode]
  -> review_loop()     [CLI: accept / edit / custom]
  -> rewrite_resume()  [Groq quality model, constrained rewrite]
  -> resume_versions + variant_*.md
```

## 5. Groq model choice (free tier, verified Sep 2026)

> Free-tier limits are per-org, not per-key. Daily token ceiling binds first.
> Check live catalog: `GET https://api.groq.com/openai/v1/models`.

| Role | Model ID | Why | Speed |
|------|----------|-----|-------|
| **Rewrite / suggest (PRIMARY)** | `openai/gpt-oss-120b` | Best quality open-weight on Groq, 131k ctx, reliable JSON mode | ~500 tok/s |
| **Grouping (FAST)** | `openai/gpt-oss-20b` | Same family, fastest, 131k ctx, cheap bulk clustering | ~1000 tok/s |
| **Fallback (either role)** | `qwen/qwen3.8-27b` | Mid-size quality backup, 131k ctx | ~500 tok/s |

> Live catalog verified 2026-09-19 via `GET /openai/v1/models` (13 models).
> `llama-3.1-8b-instant` / `llama-3.3-70b-versatile` are delisted — do not use.
> Free-tier limits change; authoritative source is console.groq.com/settings/limits.

Rules:
- Default `GROQ_REWRITE_MODEL=openai/gpt-oss-120b`, `GROQ_GROUP_MODEL=openai/gpt-oss-20b`.
- Never use preview models (`qwen/qwen3.6-27b`, `minimax-*`, `compound-*`) for load-bearing paths — quarterly churn.
- All calls: `temperature=0`, `response_format={"type":"json_object"}`, max 1500 output tokens for suggest, 4000 for rewrite.
- Budget: grouping ~1-2k tokens/job → 100 jobs ≈ 150k tokens on the fast 20b model. Rewrite ~3-5k tokens/variant → 5 variants ≈ 25k tokens on 120b. Watch daily usage at console.groq.com/settings/limits.
- On `429`: exponential backoff (2s, 4s, 8s), then auto-fallback to `qwen/qwen3.8-27b`.

## 6. DB changes (new migration in `resume_tweaker.py:init_db`)
```sql
CREATE TABLE IF NOT EXISTS job_groups (
  group_id TEXT PRIMARY KEY,      -- slug, e.g. genai-llm-rag
  label TEXT NOT NULL,
  job_urls TEXT NOT NULL,         -- JSON array of jobs.url
  top_skills TEXT NOT NULL,       -- JSON array
  top_gaps TEXT NOT NULL,         -- JSON array (skills in jobs, missing in resume)
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tweak_suggestions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  group_id TEXT NOT NULL REFERENCES job_groups(group_id),
  suggestion_json TEXT NOT NULL,  -- {add[], rephrase[], remove[], ats_keywords[]}
  user_decision TEXT,             -- accept | edit | custom | skip | NULL=pending
  user_text TEXT,                 -- user-supplied tweak when edit/custom
  decided_at TEXT,
  UNIQUE(group_id)
);
CREATE TABLE IF NOT EXISTS resume_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  group_id TEXT NOT NULL REFERENCES job_groups(group_id),
  base_hash TEXT NOT NULL,        -- sha256 of base_resume.md
  variant_path TEXT NOT NULL,
  diff_json TEXT NOT NULL,
  model TEXT NOT NULL,
  created_at TEXT NOT NULL
);
```

## 7. CLI contract
```
python resume_tweaker.py --help
python resume_tweaker.py group    --min-size 3 --limit 100
python resume_tweaker.py suggest  --group genai-llm-rag
python resume_tweaker.py review   --group genai-llm-rag   # interactive accept/edit/custom/skip
python resume_tweaker.py rewrite  --group genai-llm-rag   # respects stored decision
python resume_tweaker.py run      # group -> suggest -> review -> rewrite (full pipeline)
```

- `group`: reads ungrouped `job_analysis` rows, calls BULK model, upserts `job_groups`.
- `suggest`: reads `base_resume.md` + group jobs, calls PRIMARY model, upserts `tweak_suggestions` with `user_decision=NULL`.
- `review`: prints suggestion, prompts `[accept/edit/custom/skip]`; stores decision. Must support non-interactive `--decision accept --text "..."`.
- `rewrite`: builds final prompt = base resume + suggestion + user_text, calls PRIMARY model, writes `resume/variant_<group>.md`, inserts `resume_versions`.
- Idempotent: re-run skips groups with a `resume_versions` row for current `base_hash` unless `--force`.

## 8. Prompt contracts
- Prompts live in `prompts/` as versioned files: `group_v1.txt`, `suggest_v1.txt`, `rewrite_v1.txt`.
- `suggest` output JSON schema (strict):
```json
{
  "add": [{"skill": "str", "bullet": "str", "evidence": "str"}],
  "rephrase": [{"original": "str", "revised": "str", "reason": "str"}],
  "remove": ["str"],
  "ats_keywords": ["str"]
}
```
- `rewrite` output: `{ "resume_markdown": "str", "diff": [{"section": "str", "before": "str", "after": "str", "reason": "str"}] }`.
- Never invent employers, dates, or metrics. If evidence missing, emit `"evidence": null` and flag in `diff[].reason`.

## 9. API sketch
```python
from groq import Groq
client = Groq()  # reads GROQ_API_KEY
def groq_json(model, system, user, max_tokens=1500) -> dict:
    resp = client.chat.completions.create(
        model=model, temperature=0, max_tokens=max_tokens,
        response_format={"type": "json_object"},
        messages=[{"role":"system","content":system},{"role":"user","content":user}],
    )
    return json.loads(resp.choices[0].message.content)
```

## 10. Acceptance criteria
- [ ] `python resume_tweaker.py run` on 50 analyzed jobs → 3-6 groups, each with suggestion + variant md.
- [ ] `review` accept/edit/custom/skip all persist correctly; `rewrite` honors `user_text`.
- [ ] No hallucinated jobs/degrees; all added bullets carry `evidence` or explicit gap flag.
- [ ] 429 rate-limit → backoff + fallback works; daily usage logged to stdout.
- [ ] `pytest tests/test_resume_tweaker.py` passes (mocked Groq client, no network).
- [ ] No secrets in repo (`git grep -i groq_api` returns only `GROQ_API_KEY` placeholder + env reads).

## 11. Security
- `GROQ_API_KEY` via `.env` / env only. Never hardcode, never log.
- The key pasted in chat must be **rotated immediately** at console.groq.com → new key → `.env`.
- `.env` already gitignored. Add `resume/variant_*.md` to gitignore if resumes are private (opt-in).

## 12. Rollout
1. Land this spec + agent/config files (no behavior change).
2. Implement `resume_tweaker.py` + `prompts/*` + tests.
3. Manual run on real `jobs.db`, tune `min-size` + prompts.
4. Optional: PDF export, Streamlit review UI.
