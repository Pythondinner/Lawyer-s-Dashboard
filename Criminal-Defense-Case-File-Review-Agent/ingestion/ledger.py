"""证据台账的数据结构和存取——每条记录对应卷宗里可独立引用的一段内容
(通常是一页,拍照页里贴了多张截图时是其中一张)。"""

import json
from dataclasses import asdict, dataclass, field


@dataclass
class LedgerEntry:
    volume: str  # 卷宗标签,比如"卷2"
    source_file: str
    page: int
    segment: int  # 同一页内的第几个独立片段,普通页固定为1
    extraction_method: str  # "native" 或 "vision"
    content_type: str  # formal_document / chat_screenshot / transaction_table / signature_seal / other
    text: str
    flags: list[str] = field(default_factory=list)  # 比如 rotated / possibly_misfiled / low_confidence


def save_ledger(entries: list[LedgerEntry], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(e) for e in entries], f, ensure_ascii=False, indent=2)


def load_ledger(path: str) -> list[LedgerEntry]:
    with open(path, "r", encoding="utf-8") as f:
        rows = json.load(f)
    return [LedgerEntry(**row) for row in rows]
