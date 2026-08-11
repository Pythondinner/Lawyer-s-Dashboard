# lawyer_shell.py
# 律师壳核心：状态管理 + 相关性判断 + 要件驱动的对话流程
# 设计依据：REBUILD_DESIGN.md 第11/12节

import os
import yaml
import json
import pickle
from typing import Dict, Any, Optional, List
import sentence_transformers
from openai import OpenAI

from harness import Executor
from context_manager import ContextManager
from intake.gate import GATE_FIELDS, evaluate_gate
from intake.profile_template import PROFILE_FIELDS
from intake.extractor import extract_facts


class LawyerShell:
    """
    律师壳 - 法律合规Agent的大脑
    要件驱动模式：每轮对话扫描所有"未知"要件，尽量一次性抽取，不走固定顺序的线性表单
    """

    def __init__(self, config_path: str = "config.yaml"):
        with open(config_path, 'r', encoding='utf-8') as f:
            self.config = yaml.safe_load(f)

        self.embedder = sentence_transformers.SentenceTransformer('all-MiniLM-L6-v2')
        self.tool_embeddings = {}
        for tool in self.config['tools']:
            self.tool_embeddings[tool['id']] = self.embedder.encode(tool['description'])

        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            raise ValueError("请设置环境变量 DEEPSEEK_API_KEY")
        self.client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")

        self.executor = Executor(config_path)  # 复用harness的Executor做要件抽取调用

        self.cancel_flag = False

        self.start_triggers = [
            "启动评估", "开始评估",
            "启动GDPR", "开启GDPR", "开始GDPR", "打开GDPR",
            "启动EU", "开启EU", "开始EU", "打开EU",
            "GDPR评估", "EU评估",
        ]
        self.exit_triggers = ["退出评估", "取消评估", "不做了", "退出"]

        self.trigger_hint = """
---
🔧 **启动开关**：如需启动正式合规评估，请直接回复 `启动评估`。

💡 您也可以随时提出具体的合规问题。"""

    # ============================================================
    # 触发词检测
    # ============================================================
    def _detect_start_trigger(self, user_input: str) -> bool:
        return any(t in user_input for t in self.start_triggers)

    def _detect_exit_trigger(self, user_input: str) -> bool:
        return any(t in user_input for t in self.exit_triggers)

    def _append_trigger_hint(self, ctx: ContextManager, response: str) -> str:
        if ctx.stage != "idle":
            return response
        if "启动评估" in response or "启动开关" in response:
            return response
        return response + self.trigger_hint

    def _get_welcome_message(self) -> str:
        return "你好！👋 我是你的GDPR与EU AI Act合规助手。" + self.trigger_hint

    # ============================================================
    # 双层漏斗：判断用户这句话是否值得进入合规评估流程
    # ============================================================
    def _double_funnel(self, user_input: str, ctx: ContextManager) -> Dict:
        reject_keywords = ["不想", "不要", "算了", "不必", "不需要", "停止", "取消", "不用"]
        for kw in reject_keywords:
            if kw in user_input:
                ctx.add_cot(f"🔍 用户表达拒绝意愿: {kw}, 判为无关")
                return {"relevant": False, "posture": "D", "reason": "用户明确拒绝"}

        input_emb = self.embedder.encode(user_input)
        max_sim = 0
        for tool_id, emb in self.tool_embeddings.items():
            sim = float(input_emb @ emb)
            if sim > max_sim:
                max_sim = sim

        ctx.add_cot(f"🔍 Embedding相似度: {max_sim:.2f} (阈值0.7/0.3)")

        if max_sim >= 0.7:
            posture = self._judge_posture(user_input)
            ctx.add_cot(f"📌 高置信度判定: 相关, 姿态: {posture}")
            return {"relevant": True, "posture": posture, "similarity": max_sim}

        elif max_sim >= 0.3:
            is_relevant = self._llm_fuzzy_judge(user_input)
            if is_relevant:
                posture = self._judge_posture(user_input)
                ctx.add_cot(f"📌 LLM裁决: 相关, 姿态: {posture}")
                return {"relevant": True, "posture": posture, "similarity": max_sim}
            else:
                ctx.add_cot("📌 LLM裁决: 无关")
                return {"relevant": False, "similarity": max_sim, "reason": "LLM判定为无关"}
        else:
            return {"relevant": False, "similarity": max_sim, "reason": "相似度过低"}

    def _llm_fuzzy_judge(self, user_input: str) -> bool:
        prompt = f"""用户说："{user_input}"
判断这句话是否表现出对 GDPR 或 EU AI Act 合规的**关注、疑问或委托意愿**。
只要话题涉及"数据合规"、"AI监管"、"用户权利"、"风险评估"、"跨境传输"等，都应判定为相关。
只回答：相关 或 无关。"""
        response = self.client.chat.completions.create(
            model="deepseek-v4-pro",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=5
        )
        return response.choices[0].message.content.strip() == "相关"

    def _judge_posture(self, user_input: str) -> str:
        prompt = f"""用户说："{user_input}"
判断用户当前姿态：
A. 坚定委托（明确要求立即启动评估）
B. 试探咨询（想了解信息，尚未决定委托）
C. 背景铺垫（提及相关话题但无委托意向）
D. 纯粹闲聊（无关话题）
只输出 A、B、C 或 D。"""
        response = self.client.chat.completions.create(
            model="deepseek-v4-pro",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=2
        )
        return response.choices[0].message.content.strip()

    # ============================================================
    # 闲聊 / 信息提供
    # ============================================================
    def _chat(self, user_input: str, ctx: ContextManager = None) -> str:
        messages = [{"role": "system", "content": "你是GDPR和EU AI Act合规助手，请友好地回复用户。"}]
        if ctx:
            for msg in ctx.get_recent_history(5):
                messages.append({"role": msg["role"], "content": msg["content"]})
        if not ctx or not ctx.history or ctx.history[-1].get("content") != user_input:
            messages.append({"role": "user", "content": user_input})

        response = self.client.chat.completions.create(
            model="deepseek-v4-pro",
            messages=messages,
            temperature=0.7
        )
        return response.choices[0].message.content

    def _provide_info(self, user_input: str) -> str:
        prompt = f"用户说：{user_input}。请提供简短的GDPR或EU AI Act相关信息。"
        response = self.client.chat.completions.create(
            model="deepseek-v4-pro",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.5
        )
        return response.choices[0].message.content

    # ============================================================
    # 检查点
    # ============================================================
    def save_checkpoint(self, ctx: ContextManager):
        os.makedirs("checkpoints", exist_ok=True)
        checkpoint = ctx.to_checkpoint()
        with open("checkpoints/latest.pkl", "wb") as f:
            pickle.dump(checkpoint, f)
        ctx.add_cot("💾 检查点已保存")

    def load_checkpoint(self, ctx: ContextManager) -> bool:
        if os.path.exists("checkpoints/latest.pkl"):
            with open("checkpoints/latest.pkl", "rb") as f:
                data = pickle.load(f)
            ctx.from_checkpoint(data)
            ctx.add_cot("♻️ 检查点已恢复")
            return True
        return False

    # ============================================================
    # 核心入口
    # ============================================================
    def respond(self, user_input: str, session_state: dict) -> str:
        ctx = session_state.ctx
        try:
            return self._respond_impl(user_input, ctx)
        finally:
            self.save_checkpoint(ctx)

    def _respond_impl(self, user_input: str, ctx: ContextManager) -> str:
        ctx.add_message("user", user_input)

        print(f"\n📨 respond: {user_input}")
        print(f"   stage={ctx.stage}")

        if len(ctx.history) <= 1:
            response = self._get_welcome_message()
            ctx.add_message("assistant", response)
            return response

        if self.cancel_flag:
            self.cancel_flag = False
            response = "⏸️ 已暂停。"
            ctx.add_message("assistant", response)
            return response

        if ctx.stage != "idle" and self._detect_exit_trigger(user_input):
            ctx.abort_collection()
            response = "好的，已退出评估。您可以随时输入「启动评估」重新开始。"
            ctx.add_message("assistant", response)
            return response

        if ctx.stage == "collecting_facts":
            response = self._continue_collecting(ctx, user_input)
            ctx.add_message("assistant", response)
            return response

        if ctx.stage == "confirming":
            response = self._handle_confirmation(ctx, user_input)
            ctx.add_message("assistant", response)
            return response

        # ---- stage == idle ----
        if self._detect_start_trigger(user_input):
            ctx.set_stage("collecting_facts")
            ctx.add_cot("🔒 用户触发启动，进入要件采集")
            response = self._continue_collecting(ctx, user_input)
            ctx.add_message("assistant", response)
            return response

        funnel_result = self._double_funnel(user_input, ctx)
        if not funnel_result.get("relevant"):
            response = self._chat(user_input, ctx)
            ctx.add_message("assistant", response)
            return self._append_trigger_hint(ctx, response)

        posture = funnel_result.get("posture")
        if posture == "A":
            ctx.set_stage("collecting_facts")
            ctx.add_cot("🔒 姿态判定为坚定委托(A)，直接进入要件采集")
            response = self._continue_collecting(ctx, user_input)
        else:
            ctx.add_cot(f"📌 姿态判定为{posture}，先征询是否启动")
            response = "听起来您可能想了解或启动一次GDPR/EU AI Act合规评估。要不要现在开始？回复「启动评估」即可开始采集信息。"

        ctx.add_message("assistant", response)
        return self._append_trigger_hint(ctx, response)

    # ============================================================
    # 要件采集：每轮扫描所有"未知"要件，尽量一次性抽取
    # ============================================================
    def _current_engines(self, gate_result: Dict[str, Any]) -> List[str]:
        engines = []
        if gate_result["gdpr_applicable"]:
            engines.append("gdpr")
        if gate_result["ai_act_applicable"]:
            engines.append("ai_act")
        return engines

    def _candidate_profile_fields(self, gate_result: Dict[str, Any]) -> List[Dict[str, Any]]:
        if gate_result["gdpr_applicable"] is None or gate_result["ai_act_applicable"] is None:
            # 阶段0还没问完：先把全部画像字段当作候选，允许用户提前透露信息，
            # 但"下一个主动问的问题"仍然由阶段0优先（见 _continue_collecting 里的字段顺序）
            return PROFILE_FIELDS
        engines = self._current_engines(gate_result)
        return [f for f in PROFILE_FIELDS if any(e in f.get("feeds", []) for e in engines)]

    def _continue_collecting(self, ctx: ContextManager, user_input: Optional[str]) -> str:
        fs = ctx.fact_store

        gate_vals = {k: fs.get_value(k) for k in
                     ["serves_eu_individuals", "processes_personal_data", "is_ai_system"]}
        gate_result = evaluate_gate(gate_vals)
        candidate_profile_fields = self._candidate_profile_fields(gate_result)
        open_fields = fs.open_fields(GATE_FIELDS) + fs.open_fields(candidate_profile_fields)

        ack_parts = []
        if user_input:
            extracted = extract_facts(user_input, open_fields, self.executor)
            for key, item in extracted.items():
                fs.set(key, item["value"], item["confidence"])
                ctx.add_cot(f"✅ 要件[{key}]已采集: {item['value']}")
                display_val = "、".join(item["value"]) if isinstance(item["value"], list) else item["value"]
                ack_parts.append(str(display_val))

        # 抽取完后重新判定阶段0，可能已经从"未知"变为"已判定"
        gate_vals = {k: fs.get_value(k) for k in
                     ["serves_eu_individuals", "processes_personal_data", "is_ai_system"]}
        gate_result = evaluate_gate(gate_vals)

        if gate_result["gdpr_applicable"] is False and gate_result["ai_act_applicable"] is False:
            reasons = "；".join(gate_result["reasons"]) or "不满足两部法律的适用前提"
            ctx.add_cot(f"🛑 阶段0判定两部法律均不适用: {reasons}")
            ctx.abort_collection()
            return f"根据你提供的信息，GDPR 和 EU AI Act 目前看都不适用于这个系统：{reasons}。如果情况有变，随时可以重新输入「启动评估」。"

        if gate_result["gdpr_applicable"] is not None and gate_result["ai_act_applicable"] is not None:
            ctx.applicable_engines = self._current_engines(gate_result)

        candidate_profile_fields = self._candidate_profile_fields(gate_result)
        remaining = fs.open_fields(GATE_FIELDS) + fs.open_fields(candidate_profile_fields)

        ack = f"好的，已记录：{'；'.join(ack_parts)}。\n\n" if ack_parts else ""

        if not remaining:
            ctx.applicable_engines = self._current_engines(gate_result)
            ctx.set_stage("confirming")
            summary = self._build_summary(ctx)
            return f"{ack}所有必要信息已收集完毕。请核对以下内容：\n\n{summary}\n\n请回复「确认」启动评估。"

        # 按"已经被追问过几次"升序排列：优先级最高的字段如果连续问不出来，
        # 就让位给其他还没问过的字段，避免死磕同一个问题、让对话显得卡住
        remaining_sorted = sorted(remaining, key=lambda f: ctx.ask_counts.get(f["key"], 0))
        next_field = remaining_sorted[0]
        ask_count = ctx.ask_counts.get(next_field["key"], 0) + 1
        ctx.ask_counts[next_field["key"]] = ask_count

        question = next_field["question"]
        if ask_count >= 3 and next_field["type"] == "bool":
            question += "（这一点我还没能从对话里确认到，麻烦直接回答「是」或「否」）"

        return f"{ack}{question}"

    # ============================================================
    # 确认阶段：确认即执行，否则视为修正/补充信息
    # ============================================================
    def _handle_confirmation(self, ctx: ContextManager, user_input: str) -> str:
        if "确认" in user_input:
            return self._execute_engines(ctx)

        fs = ctx.fact_store
        engines = ctx.applicable_engines or []
        candidate_profile_fields = [f for f in PROFILE_FIELDS if any(e in f.get("feeds", []) for e in engines)]
        all_fields = GATE_FIELDS + candidate_profile_fields  # 传全集而非open_fields：允许覆盖已知值

        extracted = extract_facts(user_input, all_fields, self.executor)
        if not extracted:
            summary = self._build_summary(ctx)
            return f"没有理解具体要修改哪个信息，请直接说明，或回复「确认」启动评估。\n\n当前信息：\n\n{summary}"

        for key, item in extracted.items():
            fs.set(key, item["value"], item["confidence"])
            ctx.add_cot(f"✏️ 要件[{key}]已修正: {item['value']}")

        summary = self._build_summary(ctx)
        return f"好的，已更新。请核对以下内容：\n\n{summary}\n\n请回复「确认」启动评估。"

    def _build_summary(self, ctx: ContextManager) -> str:
        fs = ctx.fact_store
        engines = ctx.applicable_engines or []
        lines = []
        if "gdpr" in engines:
            lines.append("**将运行：GDPR 数据保护影响评估**")
        if "ai_act" in engines:
            lines.append("**将运行：EU AI Act 义务全覆盖分析**")
        lines.append("")

        candidate_fields = GATE_FIELDS + [f for f in PROFILE_FIELDS if any(e in f.get("feeds", []) for e in engines)]
        for f in candidate_fields:
            if fs.is_known(f["key"]):
                value = fs.get_value(f["key"])
                display = "、".join(value) if isinstance(value, list) else value
                lines.append(f"- {f['question']} → {display}")
        return "\n".join(lines)

    # ============================================================
    # 执行引擎
    # ============================================================
    def _execute_engines(self, ctx: ContextManager) -> str:
        ctx.add_cot("⚙️ 开始执行引擎...")
        ctx.set_stage("executing")
        fs = ctx.fact_store
        engines = ctx.applicable_engines or []
        responses = []

        gdpr_ok = False
        try:
            if "gdpr" in engines:
                from modules.risk_identification import run_risk_harness
                from modules.necessity_justification import run_necessity_harness
                from modules.mitigation_design import run_mitigation_harness
                from adapters import build_gdpr_scenario_text
                scenario_text = build_gdpr_scenario_text(fs)
                known_facts = {"automated_decision_exists": fs.get_value("automated_decision_exists")}
                print("=" * 60)
                print("📊 传递给 GDPR 工具的场景文本：")
                print(scenario_text)
                print("=" * 60)
                risk_result = run_risk_harness(scenario_text, known_facts=known_facts)
                if risk_result:
                    necessity_result = run_necessity_harness()
                    mitigation_result = run_mitigation_harness()
                    gdpr_ok = True
                    responses.append(
                        "✅ GDPR评估已完成（风险识别 + 必要性论证"
                        + ("+ 缓解方案" if mitigation_result else "，缓解方案生成失败")
                        + "）！报告已生成至 outputs/。"
                    )
                    if not necessity_result:
                        responses.append("⚠️ 必要性论证未能生成，报告不完整。")
                else:
                    responses.append("❌ GDPR评估未能生成有效结果，请检查输入信息是否足够具体，或稍后重试。")

            if "ai_act" in engines:
                from modules.main import run_eu_pipeline
                from adapters import build_ai_act_human_input
                human_input = build_ai_act_human_input(fs)
                result = run_eu_pipeline(human_input)
                if result:
                    responses.append("✅ EU AI Act分析已完成！报告已生成至 outputs/eu_ai_act_report.md。")
                else:
                    responses.append("❌ EU AI Act分析未能生成有效结果，请检查输入信息是否足够具体，或稍后重试。")

            if not engines:
                responses.append("⚠️ 没有确定要运行的引擎，可能是阶段0判断异常，请重新开始。")

            if gdpr_ok:
                try:
                    from fusion.report_builder import build_fusion_report
                    fusion_path = build_fusion_report()
                    responses.append(f"📄 融合报告（法务视图+工程视图）已生成至 {fusion_path}。")
                except Exception as fe:
                    print(f"⚠️ 融合报告生成失败（不影响主流程）: {fe}")

            response = "\n\n".join(responses)
            ctx.add_cot("✅ 引擎执行完成")

        except Exception as e:
            import traceback
            traceback.print_exc()
            response = f"❌ 执行失败：{str(e)}"
            ctx.add_cot(f"❌ 执行异常: {str(e)}")

        ctx.abort_collection()
        return response
