# harness/ledger.py
# 状态账本：记录Harness闭环中的所有状态

import json
from datetime import datetime
from typing import Dict, List, Any, Optional


class Ledger:
    """记录模块运行状态 —— 同时也是审计记录：结论可追溯到具体哪次调用、用的哪个模型、依据哪版语料"""

    def __init__(self, module_name: str, max_rounds: int = 3, model: str = None, corpus_version: str = None):
        self.module_name = module_name
        self.max_rounds = max_rounds
        self.round = 0
        self.history: List[Dict[str, Any]] = []
        self.best_result: Optional[Dict[str, Any]] = None
        self.start_time = datetime.now()
        self.model = model
        self.corpus_version = corpus_version

    def log_round(self, round_num: int, inputs: Dict, outputs: Dict, observation: Dict, decision: Dict):
        entry = {
            "round": round_num,
            "timestamp": datetime.now().isoformat(),
            "inputs": inputs,
            "outputs": outputs,
            "observation": observation,
            "decision": decision
        }
        self.history.append(entry)
        self.round = round_num
        if self._is_better(outputs):
            self.best_result = outputs

    def _is_better(self, outputs: Dict) -> bool:
        if self.best_result is None:
            return True
        current_count = len(outputs.get("risk_points", []))
        best_count = len(self.best_result.get("risk_points", []))
        return current_count > best_count

    def should_stop(self) -> bool:
        return self.round >= self.max_rounds

    def get_snapshot(self) -> Dict:
        return {
            "module_name": self.module_name,
            "round": self.round,
            "max_rounds": self.max_rounds,
            "history_count": len(self.history),
            "best_result": self.best_result,
            "elapsed_seconds": (datetime.now() - self.start_time).total_seconds()
        }

    def get_audit_record(self) -> Dict[str, Any]:
        """结论可追溯：记录用的哪个模型、哪次调用、依据哪版语料——不只是调试日志，是可查询的生成依据"""
        return {
            "module_name": self.module_name,
            "model": self.model,
            "corpus_version": self.corpus_version,
            "start_time": self.start_time.isoformat(),
            "end_time": datetime.now().isoformat(),
            "total_rounds": self.round,
            "round_timestamps": [h["timestamp"] for h in self.history],
        }

    def save_to_file(self, filepath: str):
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump({
                "module_name": self.module_name,
                "model": self.model,
                "corpus_version": self.corpus_version,
                "start_time": self.start_time.isoformat(),
                "history": self.history,
                "best_result": self.best_result,
                "audit_record": self.get_audit_record(),
            }, f, indent=2, ensure_ascii=False)
