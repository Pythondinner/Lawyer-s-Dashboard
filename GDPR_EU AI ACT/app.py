# app.py
# Streamlit界面 - 法律合规Agent前端

import sys
import streamlit as st
import os

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from lawyer_shell import LawyerShell
from context_manager import ContextManager

st.set_page_config(page_title="法律合规Agent", page_icon="⚖️", layout="wide")

@st.cache_resource
def get_lawyer_shell():
    return LawyerShell()

lawyer = get_lawyer_shell()

if "ctx" not in st.session_state:
    st.session_state.ctx = ContextManager()

# ============================================================
# 侧边栏
# ============================================================
with st.sidebar:
    st.header("⚖️ 法律合规Agent")
    st.caption("GDPR · EU AI Act")
    st.divider()

    col1, col2 = st.columns(2)
    with col1:
        if st.button("⏸️ 暂停", use_container_width=True):
            lawyer.cancel_flag = True
            st.rerun()
    with col2:
        if st.button("🔄 继续", use_container_width=True):
            lawyer.cancel_flag = False
            st.rerun()

    if st.button("🗑️ 清空对话", use_container_width=True):
        st.session_state.ctx.reset()
        st.rerun()

    if st.button("♻️ 恢复上次会话", use_container_width=True):
        if lawyer.load_checkpoint(st.session_state.ctx):
            st.success("已恢复上次会话")
        else:
            st.warning("没有找到可恢复的会话记录")
        st.rerun()

    st.divider()
    st.subheader("🧠 律师思考链")
    ctx = st.session_state.ctx
    if ctx.cot_chain:
        for step in ctx.cot_chain[-20:]:
            st.caption(f"• {step}")
    else:
        st.caption("等待对话开始...")

    st.divider()
    st.subheader("📊 当前状态")
    st.caption(f"阶段: {ctx.stage}")
    st.caption(f"适用引擎: {', '.join(ctx.applicable_engines) if ctx.applicable_engines else '未确定'}")
    known_count = sum(1 for f in ctx.fact_store.facts.values() if f.get('status') in ('known', 'inferred'))
    st.caption(f"已知要件: {known_count}")
    st.caption(f"会话ID: {ctx.session_id}")

# ============================================================
# 主区域
# ============================================================
st.title("⚖️ 法律合规Agent")
st.caption("专业GDPR · EU AI Act 合规评估")

ctx = st.session_state.ctx
for msg in ctx.history:
    if msg["role"] == "user":
        with st.chat_message("user"):
            st.write(msg["content"])
    else:
        with st.chat_message("assistant"):
            st.write(msg["content"])

# ============================================================
# 输入框
# ============================================================
prompt = st.chat_input("请输入您的合规需求...")

if prompt:
    with st.chat_message("user"):
        st.write(prompt)

    ctx.add_message("user", prompt)

    with st.chat_message("assistant"):
        with st.spinner("律师正在思考..."):
            response = lawyer.respond(prompt, st.session_state)

        st.write(response)

    if not ctx.history or ctx.history[-1].get("content") != response:
        ctx.add_message("assistant", response)

    st.rerun()
