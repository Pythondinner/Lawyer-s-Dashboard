"""轻量网页壳,纯展示层——不改动底层逻辑,Planner/Multi-Agent/记者/笔杆子全部原样复用。

跟终端版(run_research.py 的 run())区别有两处:
1. 反问环节:终端版走真实 input(),网页版走网页表单(input() 在网页里没法用)。
2. 进度展示:终端版是滚动的 print 日志;网页版把耗时的后端调用丢进后台线程,
   主线程轮询一个消息队列,实时把里程碑事件写进一个 st.status 面板,而不是干等一个转圈。
   详细的原始日志(每条来源的筛选过程)依然被完整捕获,收在一个可展开的区域里,不会丢失,
   只是不作为默认呈现——两种粒度都在,自己选看哪层。

引用不直接显示 source_id,渲染成脚注编号(①②③...),文末统一列参考来源;
不确定性(caveats)也不分散成好几个警告框,收进一个单独的"编辑说明"板块,样式收敛但依然完整可见。
"""

import contextlib
import html
import io
import queue
import time
from concurrent.futures import ThreadPoolExecutor

import streamlit as st
from dotenv import load_dotenv

load_dotenv()

from planner import plan
from run_research import DEMO_QUESTION, MAX_CLARIFY_ROUNDS, run_with_subqueries
from storage import init_db

st.set_page_config(page_title="Deep Research Agent", page_icon="🗞️", layout="wide")
init_db()

# ---------- 样式 ----------

st.markdown(
    """
<style>
:root {
  --ink: #f2f0e8;
  --muted: #a39c8a;
  --paper: #121212;
  --card: #1c1c1c;
  --border: #3a3324;
  --accent: #e0a94f;
  --note-bg: #24211a;
}
.stApp { background: var(--paper); }
.stApp, .stApp p, .stApp span, .stApp label, .stApp div { color: var(--ink); }
a { color: var(--accent) !important; }
.report-page { max-width: 880px; margin: 0 auto; color: var(--ink); }
.eyebrow { font-family: ui-monospace, monospace; font-size: 12px; letter-spacing: .08em;
  text-transform: uppercase; color: var(--accent); margin-bottom: 6px; }
.headline { font-family: Georgia, "Songti SC", serif; font-size: 34px; font-weight: 700;
  line-height: 1.25; margin: 0 0 14px; }
.lede { font-size: 17px; line-height: 1.7; color: var(--muted); font-style: italic;
  border-left: 3px solid var(--accent); padding-left: 14px; margin-bottom: 28px; }
.section-card { background: var(--card); border: 1px solid var(--border); border-radius: 10px;
  padding: 20px 22px; margin-bottom: 18px; }
.section-title { font-family: Georgia, "Songti SC", serif; font-size: 19px; font-weight: 700;
  margin: 0 0 12px; }
.section-body { font-size: 15px; line-height: 1.75; }
.report-table { width: 100%; border-collapse: collapse; font-size: 14px; }
.report-table th, .report-table td { border-bottom: 1px solid var(--border); padding: 7px 10px;
  text-align: left; }
.report-table th { color: var(--muted); font-weight: 600; font-size: 12.5px; }
sup { color: var(--accent); font-size: 11px; }
.steps-list { margin: 0; padding-left: 22px; }
.steps-list li { padding: 6px 0; }
.timeline-item, .list-item { padding: 8px 0; border-bottom: 1px dashed var(--border); }
.timeline-item:last-child, .list-item:last-child { border-bottom: none; }
.timeline-date { font-family: ui-monospace, monospace; font-size: 12.5px; color: var(--accent); }
.list-headline { font-weight: 700; }
.editor-note { background: var(--note-bg); border-radius: 10px; padding: 18px 22px; margin: 24px 0; }
.editor-note h4 { margin: 0 0 10px; font-size: 14px; letter-spacing: .02em; }
.editor-note .item { font-size: 13.5px; line-height: 1.6; margin-bottom: 8px; color: var(--muted); }
.editor-note .tag { font-family: ui-monospace, monospace; font-size: 10.5px; color: var(--accent);
  border: 1px solid var(--accent); border-radius: 4px; padding: 1px 5px; margin-right: 6px; }
.references { font-size: 12.5px; color: var(--muted); margin-top: 20px; }
.references ol { padding-left: 20px; }
.references li { margin-bottom: 4px; }
</style>
""",
    unsafe_allow_html=True,
)


# ---------- 渲染辅助 ----------


def _footnote_map(report: dict) -> dict[str, int]:
    """按第一次出现的顺序,给每个 source_id 分配一个脚注编号,渲染成 ①②③ 而不是原始 s123。"""
    order: list[str] = []
    seen: set[str] = set()

    def note(sid):
        if sid and sid not in seen:
            seen.add(sid)
            order.append(sid)

    for section in report.get("sections", []):
        t = section.get("type")
        if t == "comparison_table":
            for row in section.get("rows", []):
                for v in row.get("values", []):
                    note(v.get("source_id"))
        elif t == "timeline":
            for ev in section.get("events", []):
                note(ev.get("source_id"))
        elif t == "steps":
            for step in section.get("steps", []):
                note(step.get("source_id"))
        elif t == "item_list":
            for item in section.get("items", []):
                note(item.get("source_id"))
        elif t == "narrative":
            for sid in section.get("citations", []):
                note(sid)
    for cav in report.get("caveats", []):
        note(cav.get("source_id"))
    return {sid: i + 1 for i, sid in enumerate(order)}


def _sup(sid: str | None, fmap: dict[str, int]) -> str:
    if not sid or sid not in fmap:
        return ""
    return f"<sup>[{fmap[sid]}]</sup>"


def _esc(text) -> str:
    return html.escape(str(text)) if text is not None else ""


def render_section_html(section: dict, fmap: dict[str, int]) -> str:
    t = section.get("type")
    title = _esc(section.get("title", ""))
    body = ""

    if t == "comparison_table":
        columns = section.get("columns", [])
        header = "".join(f"<th>{_esc(c)}</th>" for c in columns)
        rows = ""
        for row in section.get("rows", []):
            cells = f"<td>{_esc(row.get('label',''))}</td>"
            for v in row.get("values", []):
                cells += f"<td>{_esc(v.get('value',''))}{_sup(v.get('source_id'), fmap)}</td>"
            rows += f"<tr>{cells}</tr>"
        body = f"<table class='report-table'><thead><tr>{header}</tr></thead><tbody>{rows}</tbody></table>"

    elif t == "timeline":
        events = sorted(section.get("events", []), key=lambda e: e.get("date") or "")
        for ev in events:
            body += (
                "<div class='timeline-item'>"
                f"<span class='timeline-date'>{_esc(ev.get('date',''))}</span> — "
                f"{_esc(ev.get('description',''))}{_sup(ev.get('source_id'), fmap)}"
                "</div>"
            )

    elif t == "steps":
        items_html = "".join(
            f"<li>{_esc(step.get('description',''))}{_sup(step.get('source_id'), fmap)}</li>"
            for step in section.get("steps", [])
        )
        body = f"<ol class='steps-list'>{items_html}</ol>"

    elif t == "item_list":
        for item in section.get("items", []):
            body += (
                "<div class='list-item'>"
                f"<div class='list-headline'>{_esc(item.get('headline',''))}</div>"
                f"<div>{_esc(item.get('detail',''))}{_sup(item.get('source_id'), fmap)}</div>"
                "</div>"
            )

    elif t == "narrative":
        sups = "".join(_sup(sid, fmap) for sid in section.get("citations", []))
        body = f"<p>{_esc(section.get('text',''))}{sups}</p>"

    return f"<div class='section-card'><div class='section-title'>{title}</div><div class='section-body'>{body}</div></div>"


def render_editor_note_html(caveats: list[dict], fmap: dict[str, int]) -> str:
    if not caveats:
        return ""
    items = ""
    for c in caveats:
        tag = _esc(c.get("severity", ""))
        items += (
            f"<div class='item'><span class='tag'>{tag}</span>"
            f"{_esc(c.get('issue',''))}{_sup(c.get('source_id'), fmap)}</div>"
        )
    return f"<div class='editor-note'><h4>编辑说明</h4>{items}</div>"


def render_references_html(sources: list[dict], fmap: dict[str, int]) -> str:
    by_num = {num: sid for sid, num in fmap.items()}
    by_id = {s["id"]: s for s in sources}
    items = ""
    for num in sorted(by_num):
        s = by_id.get(by_num[num])
        if not s:
            continue
        items += f"<li>{_esc(s.get('name',''))} — <a href='{s.get('url','')}' target='_blank'>{s.get('url','')}</a></li>"
    return f"<div class='references'><b>参考来源</b><ol>{items}</ol></div>"


# ---------- 页面 ----------

st.markdown("<div class='eyebrow'>Deep Research Agent</div>", unsafe_allow_html=True)
st.caption("Planner → 并行 Researcher(Multi-Agent) → 记者核实分类 → 并行笔杆子撰写。过程实时可见,不是黑盒。")

question = st.text_input("研究问题", value=DEMO_QUESTION, key="question_input")
start_clicked = st.button("开始研究", type="primary")

if start_clicked and question.strip():
    for key in ("stage", "clarification", "sub_queries", "result", "trace", "clarify_rounds"):
        st.session_state.pop(key, None)
    st.session_state.original_question = question.strip()
    with st.spinner("Planner 判断问题是否够清楚..."):
        plan_result = plan(question.strip())

    if plan_result["status"] == "needs_clarification":
        st.session_state.stage = "clarifying"
        st.session_state.clarification = plan_result
    else:
        st.session_state.stage = "ready_to_run"
        st.session_state.sub_queries = plan_result["sub_queries"]

# 用显式占位符管理每个阶段的区域——不管 Streamlit 内部怎么处理跨阶段的元素清理时机,
# 每次运行都强制"要么渲染、要么清空",不会出现上一阶段的反问框残留在新内容下面的情况。
clarify_slot = st.empty()
progress_slot = st.empty()

stage = st.session_state.get("stage")

if stage == "clarifying":
    with clarify_slot.container():
        c = st.session_state.clarification
        st.warning(f"**Planner 反问**:{c['clarification_question']}")
        if c.get("clarification_options"):
            st.caption("参考选项(不必照抄):")
            for opt in c["clarification_options"]:
                st.caption(f"　- {opt}")
        answer = st.text_input("你的回答", key="clarification_answer")
        if st.button("提交回答") and answer.strip():
            with st.spinner("重新规划中..."):
                plan_result2 = plan(st.session_state.original_question, clarification_answer=answer.strip())

            st.session_state.clarify_rounds = st.session_state.get("clarify_rounds", 0) + 1
            still_unclear = plan_result2.get("status") == "needs_clarification"

            if still_unclear and st.session_state.clarify_rounds < MAX_CLARIFY_ROUNDS:
                # 回答完还是不够清楚:Planner 完全可能再问一轮,不能假设"问一次就够",
                # 继续停在 clarifying 阶段,换成这一轮的新问题
                st.session_state.clarification = plan_result2
            else:
                # 要么已经清楚了,要么问够 MAX_CLARIFY_ROUNDS 轮还是不清楚——
                # 后一种情况就不再追问,用原始问题兜底,不能无限循环
                st.session_state.sub_queries = plan_result2.get("sub_queries") or [st.session_state.original_question]
                st.session_state.stage = "ready_to_run"

            clarify_slot.empty()
            st.rerun()
else:
    clarify_slot.empty()

if stage == "ready_to_run":
    with progress_slot.container():
        sub_queries = st.session_state.sub_queries
        st.info(f"[Planner] 拆解出 {len(sub_queries)} 个子问题: {sub_queries}")

        event_q: queue.Queue = queue.Queue()
        trace_buffer = io.StringIO()
        original_question = st.session_state.original_question  # 后台线程不能读 session_state,先取成普通变量

        def _worker():
            with contextlib.redirect_stdout(trace_buffer):
                return run_with_subqueries(original_question, sub_queries, on_event=event_q.put)

        pool = ThreadPoolExecutor(max_workers=1)
        future = pool.submit(_worker)

        status = st.status("研究进行中...", expanded=True)
        while not future.done():
            try:
                while True:
                    status.write(event_q.get_nowait())
            except queue.Empty:
                pass
            time.sleep(0.3)
        try:
            while True:
                status.write(event_q.get_nowait())
        except queue.Empty:
            pass

        result = future.result()
        pool.shutdown(wait=False)
        status.update(label="研究完成", state="complete", expanded=False)

        st.session_state.result = result
        st.session_state.trace = trace_buffer.getvalue()
        st.session_state.stage = "done"
        progress_slot.empty()
        st.rerun()
else:
    progress_slot.empty()

if st.session_state.get("stage") == "done":
    result = st.session_state.result
    report = result["report"]
    fmap = _footnote_map(report)

    st.success(
        f"{len(st.session_state.sub_queries)} 个 Researcher 并行跑完,"
        f"实际耗时 {result['wall_clock_seconds']} 秒"
        f"(顺序执行预计 {result['sequential_estimate_seconds']} 秒),"
        f"共 {result['evidence_count']} 条证据"
    )

    with st.expander("🧠 展开查看完整过程日志(每条来源的筛选细节)"):
        st.code(st.session_state.trace, language=None)

    page_html = "<div class='report-page'>"
    page_html += f"<div class='headline'>{_esc(report.get('headline',''))}</div>"
    page_html += f"<div class='lede'>{_esc(report.get('lede',''))}</div>"
    for section in report.get("sections", []):
        page_html += render_section_html(section, fmap)
    page_html += render_editor_note_html(report.get("caveats", []), fmap)
    page_html += render_references_html(report.get("sources", []), fmap)
    page_html += "</div>"

    st.markdown(page_html, unsafe_allow_html=True)

    if result["unresolved_problems"]:
        st.error(
            "引用校验未完全通过,以下问题未解决:\n"
            + "\n".join(f"- {p}" for p in result["unresolved_problems"])
        )
    else:
        st.caption(f"✅ 引用校验通过(共尝试 {result['attempts']} 轮)")

    if st.button("🔄 重新开始"):
        for key in ("stage", "clarification", "sub_queries", "result", "trace", "original_question", "clarify_rounds"):
            st.session_state.pop(key, None)
        st.rerun()
