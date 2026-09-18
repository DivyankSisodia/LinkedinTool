"""Mocked offline tests for resume_tweaker.py — no network, no real GROQ_API_KEY."""
import json
import sqlite3
from pathlib import Path

import resume_tweaker as rt


def _memdb():
    conn = sqlite3.connect(":memory:")
    rt.init_db(conn)
    return conn


def test_init_db_creates_tables():
    conn = _memdb()
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"job_groups", "tweak_suggestions", "resume_versions"} <= tables


def test_group_chunks_and_merges(monkeypatch):
    conn = _memdb()
    conn.executescript("""
        CREATE TABLE jobs (url TEXT PRIMARY KEY, title TEXT, scraped_at TEXT);
        CREATE TABLE job_analysis (job_url TEXT PRIMARY KEY, title TEXT, skills TEXT,
            requirements TEXT, experience TEXT);
    """)
    for i in range(45):
        conn.execute("INSERT INTO jobs VALUES (?,?,?)", (f"https://x/{i}", f"T{i}", "now"))
        conn.execute("INSERT INTO job_analysis VALUES (?,?,?,?,?)",
                     (f"https://x/{i}", f"T{i}", '["Python"]', '["5y exp"]', "5 years"))
    calls = []

    def fake_groq(model, system, user, max_tokens=1500, fallback=None):
        calls.append(user)
        n = len(json.loads(user.split("JOBS:\n", 1)[1]))
        assert n <= rt.GROUP_CHUNK  # no oversized payloads
        gid = f"g{n}"  # same slug every chunk -> must merge, not duplicate
        return {"groups": [{"group_id": gid, "label": "L",
                            "job_urls": [f"https://x/{i}" for i in range(n)],
                            "top_skills": ["Python"]}]}

    monkeypatch.setattr(rt, "groq_json", fake_groq)
    monkeypatch.setattr(rt, "load_prompt", lambda _: "sys")
    rt.cmd_group(conn, type("A", (), {"limit": 45, "min_size": 3})())
    assert len(calls) == 3  # 45 jobs / chunk 20
    row = conn.execute("SELECT job_urls FROM job_groups WHERE group_id='g20'").fetchone()
    assert row and len(json.loads(row[0])) == 20  # merged across chunks 1+2
    assert conn.execute("SELECT COUNT(*) FROM job_groups").fetchone()[0] == 2  # g20 + g5


def test_size_error_raises_without_fallback(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    seen = []

    class FakeCompletions:
        def create(self, **kw):
            seen.append(kw.get("model"))
            raise Exception("Error code: 413 - Request too large, reduce your message size")

    class FakeChat:
        completions = FakeCompletions()

    class FakeGroq:
        def __init__(self, *a, **k): pass
        chat = FakeChat()

    import groq
    monkeypatch.setattr(groq, "Groq", FakeGroq)
    try:
        rt.groq_json("m1", "sys", "user", fallback="m2")
        assert False, "should raise"
    except RuntimeError as e:
        assert "too large" in str(e).lower()
    assert seen == ["m1"]  # fallback never attempted with same oversized payload


def test_otpm_error_halves_tokens_and_retries(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    seen_tokens = []
    state = {"n": 0}

    class FakeCompletions:
        def create(self, **kw):
            seen_tokens.append(kw.get("max_tokens"))
            state["n"] += 1
            if state["n"] == 1:
                raise Exception("Error code: 429 - output tokens per minute (OTPM): "
                                "Limit 1000, Requested 1957, reduce max_tokens and try again")
            return type("R", (), {"choices": [type("C", (), {
                "message": type("M", (), {"content": '{"ok": true}'})})]})()

    class FakeChat:
        completions = FakeCompletions()

    class FakeGroq:
        def __init__(self, *a, **k): pass
        chat = FakeChat()

    import groq
    monkeypatch.setattr(groq, "Groq", FakeGroq)
    out = rt.groq_json("m1", "sys", "user", max_tokens=900, fallback="m2")
    assert out == {"ok": True}
    assert seen_tokens == [900, 450]  # halved once, same model, no fallback needed


def test_review_decisions_authoritative(tmp_path, monkeypatch):
    conn = _memdb()
    conn.execute(
        "INSERT INTO job_groups (group_id, label, job_urls, top_skills, top_gaps, created_at)"
        " VALUES (?,?,?,?,?,?)",
        ("genai-llm-rag", "GenAI", "[]", "[]", "[]", "now"),
    )
    conn.execute(
        "INSERT INTO tweak_suggestions (group_id, suggestion_json) VALUES (?,?)",
        ("genai-llm-rag", json.dumps({"add": [], "rephrase": [], "remove": [], "ats_keywords": []})),
    )
    monkeypatch.setattr("builtins.input", lambda _: "accept")
    args = type("A", (), {"group": "genai-llm-rag", "decision": "accept", "text": None})()
    rt.cmd_review(conn, args)
    row = conn.execute("SELECT user_decision FROM tweak_suggestions WHERE group_id='genai-llm-rag'").fetchone()
    assert row[0] == "accept"


def test_rewrite_honors_user_text_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "BASE_RESUME", tmp_path / "base_resume.md")
    monkeypatch.setattr(rt, "RESUME_DIR", tmp_path)
    (tmp_path / "base_resume.md").write_text("# Me\n## Skills\nPython", encoding="utf-8")
    monkeypatch.setattr(rt, "load_prompt", lambda _: "sys")
    calls = []

    def fake_groq(model, system, user, max_tokens=1500, fallback=None):
        calls.append(user)
        return {"resume_markdown": "# Me\n## Skills\nPython, RAG", "diff": []}

    monkeypatch.setattr(rt, "groq_json", fake_groq)
    conn = _memdb()
    conn.execute(
        "INSERT INTO tweak_suggestions (group_id, suggestion_json, user_decision, user_text)"
        " VALUES (?,?,?,?)",
        ("genai-llm-rag", json.dumps({"add": []}), "custom", "Emphasize RAG + evals"),
    )
    args = type("A", (), {"group": "genai-llm-rag", "force": False})()
    rt.cmd_rewrite(conn, args)
    assert "Emphasize RAG" in calls[0]  # user_text merged into rewrite prompt
    assert (tmp_path / "variant_genai-llm-rag.md").exists()
    n_before = len(calls)
    rt.cmd_rewrite(conn, args)  # second run skips (idempotent)
    assert len(calls) == n_before
