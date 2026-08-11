"""v1: 写死顺序的最简链路 —— search 一次 -> 依次 extract -> 写入 SQLite,验证整条链路能不能跑通。
不接 Planner、不接反问机制,先证明地基是通的。

[历史版本,留作演进记录,不是当前系统入口] 当前系统请跑项目根目录的 run_research.py 或 app.py。
这个文件是 Tool Use 循环从"代码写死顺序"升级到"LLM自主决策"(见 run_v2.py)之前的最初版本。
"""

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.stdout.reconfigure(encoding="utf-8")
load_dotenv(PROJECT_ROOT / ".env")

from storage import find_past_evidence, init_db, insert_evidence
from tools.extract import extract_claim
from tools.search import bocha_search

DEMO_QUESTION = "今日国际原油价格是多少"


def run():
    init_db()
    session_id = uuid.uuid4().hex[:8]
    print(f"研究问题: {DEMO_QUESTION}\n")
    print(f"本次 session_id: {session_id}\n")

    # 长期记忆:查一下之前(别的 session)是不是研究过这个问题
    past = find_past_evidence(DEMO_QUESTION, exclude_session_id=session_id)
    if past:
        print(f"[长期记忆] 发现之前研究过这个问题,有 {len(past)} 条历史证据,例如:")
        print(f"    {past[0]['retrieved_at']}  {past[0]['name']} -> {past[0]['extracted_claim']}\n")
    else:
        print("[长期记忆] 没查到历史记录,这是第一次研究这个问题\n")

    results = bocha_search(DEMO_QUESTION, count=5)
    print(f"搜到 {len(results)} 条候选来源\n")

    evidence_records = []
    for page in results:
        extraction = extract_claim(DEMO_QUESTION, page)
        status = "success" if extraction.get("relevant") else "irrelevant"
        retrieved_at = datetime.now(timezone.utc).isoformat()

        source_id = insert_evidence(
            session_id=session_id,
            question=DEMO_QUESTION,
            for_query=DEMO_QUESTION,
            url=page.get("url"),
            name=page.get("name"),
            retrieved_at=retrieved_at,
            status=status,
            extracted_claim=extraction.get("claim"),
        )

        record = {
            "source_id": source_id,
            "url": page.get("url"),
            "name": page.get("name"),
            "for_query": DEMO_QUESTION,
            "retrieved_at": retrieved_at,
            "status": status,
            "extracted_claim": extraction.get("claim"),
        }
        evidence_records.append(record)

        print(f"[{source_id}] {record['name']} -> status={status}")
        if record["extracted_claim"]:
            print(f"    抽取: {record['extracted_claim']}")
        print()

    print("=== 证据记录汇总(已写入 memory.db,喂给 Synthesizer 的原材料) ===")
    for r in evidence_records:
        print(r)


if __name__ == "__main__":
    run()
