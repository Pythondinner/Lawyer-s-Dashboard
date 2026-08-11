# intake/state.py
# 要件状态表 —— 对话驱动的核心数据结构
# 取代旧版 orchestrator 里"固定字段列表 + phase下标"的线性走法。
# 设计依据：REBUILD_DESIGN.md 第11.1节

from typing import Dict, Any, List, Optional


class FactStore:
    """
    维护一组要件（facts）的当前状态。
    每个要件: {"value": ..., "status": "unknown"/"known"/"inferred", "confidence": float, "source_turn": int}
    """

    def __init__(self):
        self.facts: Dict[str, Dict[str, Any]] = {}
        self.turn_count = 0

    def ensure_field(self, key: str):
        if key not in self.facts:
            self.facts[key] = {"value": None, "status": "unknown", "confidence": 0.0, "source_turn": None}

    def set(self, key: str, value: Any, confidence: float = 1.0, status: str = "known"):
        self.ensure_field(key)
        self.facts[key] = {
            "value": value,
            "status": status,
            "confidence": confidence,
            "source_turn": self.turn_count,
        }

    def invalidate(self, key: str):
        """把某个要件重新标记为未知（用于用户回溯修正之前的答案时，触发依赖它的枢纽重新计算）"""
        if key in self.facts:
            self.facts[key] = {"value": None, "status": "unknown", "confidence": 0.0, "source_turn": None}

    def get_value(self, key: str, default=None):
        return self.facts.get(key, {}).get("value", default)

    def is_known(self, key: str) -> bool:
        return self.facts.get(key, {}).get("status") in ("known", "inferred")

    def open_fields(self, field_defs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """返回field_defs里还没有取值的字段定义，保持field_defs给定的优先级顺序"""
        return [f for f in field_defs if not self.is_known(f["key"])]

    def next_question_field(self, field_defs: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        open_ones = self.open_fields(field_defs)
        return open_ones[0] if open_ones else None

    def values_dict(self) -> Dict[str, Any]:
        """返回 {key: value} 形式，方便传给 gate.evaluate_gate 这类纯函数"""
        return {k: v.get("value") for k, v in self.facts.items()}

    def snapshot(self) -> Dict[str, Any]:
        return {k: v.copy() for k, v in self.facts.items()}
