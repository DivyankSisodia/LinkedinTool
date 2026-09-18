"""Resume Tweaker UI — Streamlit front-end for Spec 002.

Run:  .\\.venv\\Scripts\\python.exe -m streamlit run app.py
Needs: GROQ_API_KEY in .env, jobs.db with job_analysis rows,
       resume/base_resume.md (generated from your PDF).
"""
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import streamlit as st
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")  # explicit: bare lookup can hit a stray global .env
sys.path.insert(0, str(Path(__file__).parent))
import resume_tweaker as rt

DB_PATH = "jobs.db"
st.set_page_config(page_title="Resume Tweaker", layout="wide")


def conn():
    c = sqlite3.connect(DB_PATH)
    rt.init_db(c)
    return c


def stats():
    c = conn()
    out = {
        "jobs": c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
        "analyzed": c.execute("SELECT COUNT(*) FROM job_analysis").fetchone()[0],
        "groups": c.execute("SELECT COUNT(*) FROM job_groups").fetchone()[0],
        "pending": c.execute(
            "SELECT COUNT(*) FROM tweak_suggestions WHERE user_decision IS NULL").fetchone()[0],
        "variants": c.execute("SELECT COUNT(*) FROM resume_versions").fetchone()[0],
    }
    c.close()
    return out


s = stats()
st.sidebar.title("Resume Tweaker")
st.sidebar.metric("Jobs", s["jobs"])
st.sidebar.metric("Analyzed", s["analyzed"])
st.sidebar.metric("Groups", s["groups"])
st.sidebar.metric("Pending reviews", s["pending"])
st.sidebar.metric("Variants", s["variants"])
st.sidebar.caption(f"Rewrite: `{rt.REWRITE_MODEL}`\n\nGroup: `{rt.GROUP_MODEL}`")
if not Path("resume/base_resume.md").exists():
    st.sidebar.error("Missing resume/base_resume.md")
if not __import__("os").getenv("GROQ_API_KEY"):
    st.sidebar.error("GROQ_API_KEY not set (check .env)")

jobs_tab, groups_tab, review_tab, variants_tab, resume_tab = st.tabs(
    ["Jobs", "Groups", "Review & Rewrite", "Variants", "Base Resume"])

with jobs_tab:
    q = st.text_input("Filter (title/company/skill)", "")
    c = conn()
    rows = c.execute("""
        SELECT a.title, j.company, a.location, a.experience, a.skills
        FROM job_analysis a JOIN jobs j ON j.url = a.job_url
        ORDER BY j.scraped_at DESC LIMIT 300""").fetchall()
    c.close()
    if q:
        ql = q.lower()
        rows = [r for r in rows if ql in f"{r[0]} {r[1]} {r[4]}".lower()]
    st.write(f"{len(rows)} analyzed jobs")
    st.dataframe(
        [{"Title": r[0], "Company": r[1], "Location": r[2],
          "Exp": r[3], "Skills": ", ".join(json.loads(r[4] or "[]")[:8])}
         for r in rows],
        use_container_width=True)

with groups_tab:
    col1, col2, col3 = st.columns(3)
    with col1:
        limit = st.number_input("Job limit", 10, 500, 100)
    with col2:
        min_size = st.number_input("Min group size", 2, 20, 3)
    with col3:
        st.write("")
        if st.button("Run grouping", type="primary"):
            with st.spinner("Grouping with llama-3.1-8b-instant..."):
                c = conn()
                rt.cmd_group(c, SimpleNamespace(limit=limit, min_size=min_size))
                c.close()
            st.rerun()
    c = conn()
    groups = c.execute(
        "SELECT group_id, label, job_urls, top_skills FROM job_groups").fetchall()
    c.close()
    if not groups:
        st.info("No groups yet — click **Run grouping**.")
    for gid, label, urls_json, skills_json in groups:
        urls = json.loads(urls_json or "[]")
        with st.expander(f"**{label}** (`{gid}`) — {len(urls)} jobs"):
            st.write("Top skills:", ", ".join(json.loads(skills_json or "[]")[:15]))
            st.caption(f"{len(urls)} job URLs linked")

with review_tab:
    c = conn()
    groups = c.execute("SELECT group_id, label FROM job_groups").fetchall()
    sugg = {r[0]: r[1] for r in c.execute(
        "SELECT group_id, suggestion_json FROM tweak_suggestions")}
    decis = {r[0]: (r[1], r[2]) for r in c.execute(
        "SELECT group_id, user_decision, user_text FROM tweak_suggestions")}
    c.close()
    if not groups:
        st.info("Run grouping first.")
    else:
        gid = st.selectbox("Group", [g[0] for g in groups],
                           format_func=lambda g: f"{dict(groups)[g]} ({g})")
        if gid not in sugg:
            if st.button("Generate suggestion", type="primary"):
                with st.spinner("Suggesting with gpt-oss-120b..."):
                    c = conn()
                    rt.cmd_suggest(c, SimpleNamespace(group=gid))
                    c.close()
                st.rerun()
        else:
            data = json.loads(sugg[gid])
            st.subheader("Suggested tweaks")
            st.write("**Add**")
            for a in data.get("add", []):
                st.markdown(f"- **{a.get('skill')}**: {a.get('bullet')} "
                            f"_(evidence: {a.get('evidence') or 'GAP — no evidence'})_")
            st.write("**Rephrase**")
            for r_ in data.get("rephrase", []):
                st.markdown(f"- ~~{r_.get('original')}~~ → **{r_.get('revised')}** "
                            f"({r_.get('reason')})")
            if data.get("remove"):
                st.write("**Remove:**", ", ".join(data["remove"]))
            if data.get("ats_keywords"):
                st.write("**ATS keywords:**", ", ".join(data["ats_keywords"]))

            st.divider()
            st.subheader("Your review")
            cur_dec, cur_text = decis.get(gid, (None, None)) or (None, None)
            decision = st.radio("Decision", ["accept", "edit", "custom", "skip"],
                                index=["accept", "edit", "custom", "skip"].index(cur_dec)
                                if cur_dec in ("accept", "edit", "custom", "skip") else 0,
                                horizontal=True)
            user_text = st.text_area("Your tweak (used for edit/custom)",
                                     value=cur_text or "", height=120)
            c1, c2 = st.columns(2)
            with c1:
                if st.button("Save review"):
                    c = conn()
                    import datetime
                    c.execute("""UPDATE tweak_suggestions SET user_decision=?, user_text=?,
                                 decided_at=? WHERE group_id=?""",
                              (decision, user_text or None,
                               datetime.datetime.now(datetime.timezone.utc).isoformat(), gid))
                    c.commit()
                    c.close()
                    st.success(f"Saved: {decision}")
                    st.rerun()
            with c2:
                force = st.checkbox("Force regenerate", value=False)
                if st.button("Rewrite resume variant", type="primary",
                             disabled=decision == "skip"):
                    with st.spinner("Rewriting with gpt-oss-120b..."):
                        c = conn()
                        # persist latest decision first
                        import datetime
                        c.execute("""UPDATE tweak_suggestions SET user_decision=?, user_text=?,
                                     decided_at=? WHERE group_id=?""",
                                  (decision, user_text or None,
                                   datetime.datetime.now(datetime.timezone.utc).isoformat(), gid))
                        c.commit()
                        rt.cmd_rewrite(c, SimpleNamespace(group=gid, force=force))
                        c.close()
                    st.success("Variant written — see Variants tab.")
                    st.rerun()

with variants_tab:
    c = conn()
    rows = c.execute("""SELECT group_id, variant_path, diff_json, model, created_at
                        FROM resume_versions ORDER BY created_at DESC""").fetchall()
    c.close()
    if not rows:
        st.info("No variants yet — review a group and rewrite.")
    for gid, path, diff_json, model, created in rows:
        with st.expander(f"`{gid}` — {path} ({created[:16]})"):
            st.caption(f"model: {model}")
            st.write("**Changes**")
            for d in json.loads(diff_json or "[]"):
                st.markdown(f"- **{d.get('section')}**: {d.get('reason')}")
                with st.expander("before → after"):
                    st.markdown(f"~~{d.get('before')}~~")
                    st.markdown(d.get("after") or "")
            if Path(path).exists():
                md = Path(path).read_text(encoding="utf-8")
                st.download_button("Download markdown", md, file_name=Path(path).name,
                                   key=f"dl-{gid}-{created}")
                with st.expander("Full resume markdown"):
                    st.markdown(md)

with resume_tab:
    p = Path("resume/base_resume.md")
    if p.exists():
        content = st.text_area("Base resume (source of truth for all rewrites)",
                               value=p.read_text(encoding="utf-8"), height=500)
        if st.button("Save base resume"):
            p.write_text(content, encoding="utf-8")
            st.success("Saved. New rewrites will use the updated hash.")
    else:
        st.error("resume/base_resume.md missing.")
