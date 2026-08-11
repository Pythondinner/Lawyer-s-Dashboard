"""证据台账,一张表两个角色:
- 本次研究内(同一个 session_id):Tool Use 循环攒证据的地方
- 跨 session:直接按 question 查历史记录,就是长期记忆

Multi-Agent 版本里,多个 Researcher 线程会同时写这张表——每次调用都开自己的新连接
(不跨线程共享连接对象),本身是线程安全的;但 SQLite 同一时刻只能有一个写操作,
所以加了 busy_timeout,让并发写入排队等一下再重试,而不是直接抛"database is locked"。
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "memory.db"


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db() -> None:
    conn = get_connection()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            question TEXT NOT NULL,
            for_query TEXT NOT NULL,
            url TEXT,
            name TEXT,
            retrieved_at TEXT NOT NULL,
            status TEXT NOT NULL,
            extracted_claim TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def insert_evidence(
    session_id: str,
    question: str,
    for_query: str,
    url: str | None,
    name: str | None,
    retrieved_at: str,
    status: str,
    extracted_claim: str | None,
) -> str:
    """写一条证据,返回它的 source_id(比如 's7'),给报告里的引用用。"""
    conn = get_connection()
    cur = conn.execute(
        """INSERT INTO evidence
           (session_id, question, for_query, url, name, retrieved_at, status, extracted_claim)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (session_id, question, for_query, url, name, retrieved_at, status, extracted_claim),
    )
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return f"s{row_id}"


def find_past_evidence(question: str, exclude_session_id: str, limit: int = 20) -> list[dict]:
    """长期记忆查询:这个问题之前(别的 session)研究过吗?"""
    conn = get_connection()
    rows = conn.execute(
        """SELECT * FROM evidence
           WHERE question = ? AND session_id != ?
           ORDER BY retrieved_at DESC LIMIT ?""",
        (question, exclude_session_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
