# context_manager.py
# 会话上下文管理器 - 负责所有状态存储（历史、思考链、要件状态表）

import uuid
from datetime import datetime
from typing import Dict, Any, Optional, List

from intake.state import FactStore


class ContextManager:
    """会话上下文管理器"""

    def __init__(self, session_id: Optional[str] = None):
        self.session_id = session_id or str(uuid.uuid4())[:8]
        self.created_at = datetime.now().isoformat()
        self._reset()

    def _reset(self):
        self.stage = "idle"  # idle / collecting_facts / confirming / executing
        self.applicable_engines: List[str] = []  # 阶段0判定后确定的引擎列表，如 ["gdpr", "ai_act"]
        self.fact_store = FactStore()
        self.ask_counts: Dict[str, int] = {}  # 每个字段被主动追问过几次，用于避免死磕同一个问题
        self.turn_count = 0
        self.history = []
        self.cot_chain = []

    def set_stage(self, stage: str):
        self.stage = stage
        self.cot_chain.append(f"📌 阶段切换: {stage}")

    def abort_collection(self):
        """退出采集流程/引擎执行完毕后调用：重置阶段与要件状态，但不动对话历史"""
        self.stage = "idle"
        self.applicable_engines = []
        self.fact_store = FactStore()
        self.ask_counts = {}
        self.cot_chain.append("🔓 采集状态已重置")

    def get_stage(self) -> str:
        return self.stage

    def add_message(self, role: str, content: str):
        self.turn_count += 1
        self.fact_store.turn_count = self.turn_count
        self.history.append({"role": role, "content": content, "turn": self.turn_count})

    def get_last_user_message(self) -> Optional[str]:
        for msg in reversed(self.history):
            if msg["role"] == "user":
                return msg["content"]
        return None

    def get_recent_history(self, n: int = 10) -> list:
        return self.history[-n:] if self.history else []

    def get_full_history(self) -> list:
        return self.history.copy()

    def add_cot(self, step: str):
        self.cot_chain.append(f"[{self.turn_count}] {step}")

    def get_cot_chain(self) -> list:
        return self.cot_chain.copy()

    def to_checkpoint(self) -> dict:
        return {
            "session_id": self.session_id,
            "created_at": self.created_at,
            "stage": self.stage,
            "applicable_engines": self.applicable_engines,
            "ask_counts": self.ask_counts,
            "facts": self.fact_store.snapshot(),
            "turn_count": self.turn_count,
            "history": self.history[-10:],
            "cot_chain": self.cot_chain[-20:],
        }

    def from_checkpoint(self, data: dict):
        self.session_id = data.get("session_id", self.session_id)
        self.created_at = data.get("created_at", self.created_at)
        self.stage = data.get("stage", "idle")
        self.applicable_engines = data.get("applicable_engines", [])
        self.ask_counts = data.get("ask_counts", {})
        self.turn_count = data.get("turn_count", 0)
        self.history = data.get("history", [])
        self.cot_chain = data.get("cot_chain", [])

        self.fact_store = FactStore()
        self.fact_store.turn_count = self.turn_count
        for key, fact in data.get("facts", {}).items():
            self.fact_store.facts[key] = fact

    def reset(self):
        self._reset()
        self.cot_chain.append("🔄 会话已重置")
