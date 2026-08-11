"""整卷内部一致性核对——修复 possibly_misfiled 标记原来的架构缺陷。

背景:这个标记原来写在 vision_transcribe.py 的单页识别 prompt 里,要求"如果内容明显跟
其他材料不属于同一案子,标 possibly_misfiled"。但 transcribe_page() 每次调用只处理一张
图片,模型根本没有机会看到"其他材料"长什么样,这个判断依据在单页孤立识别的架构下不可能
真正成立——直接构造过测试验证(把一份真实存在、完全无关的卷宗页面单独喂给识别模型),确认
不会触发。这不是运气问题,是设计问题。

修复:换个位置做这件事——不在"每页识别的时候"判断,而是在"一整卷所有页面都识别完之后",
把全卷内容一起给模型看一遍,让它自己找有没有明显不属于同一案子的页(不同的当事人姓名、
不同的办案机关、完全不相关的案由)。这时候模型才真正有"其他材料"可以对比,判断依据才成立。"""

import os
import re

from dotenv import load_dotenv
from openai import OpenAI

from ingestion.ledger import LedgerEntry

load_dotenv()

_client = None

CONSISTENCY_CHECK_PROMPT = """下面是同一份卷宗(卷宗标签:{volume})里,逐页转录出来的内容,每页前面标了页码。

请通读一遍,找出有没有哪一页(或哪几页)的内容,跟其他页明显不属于同一个案子/同一批材料——
比如出现了完全不同的当事人姓名、不同的办案机关、完全不相关的案由或事件。正常卷宗里同一批
材料反复提到的当事人/机关名应该是高度重复一致的,如果某一页突然出现了整卷唯一一次出现的、
跟其他页毫无关联的人名/机关名,这可能意味着这一页在扫描/整理时被错误地混进了这份卷宗。

只指出真正**明显**的异常,不要因为某页内容比较简短(比如签名页、空白页这种信息量本来就少
的页面)就误判——信息量少不等于跟其他材料不属于同一案子,不确定就不要标。

如果没有发现任何可疑页面,只回答"未发现异常页面"这几个字,不要写别的。
如果发现了,每一条单独一行,格式:
第X页:异常原因(简要说明,提到具体是哪些人名/机关名/案由跟其他页对不上)

卷宗内容:

{corpus}
"""

_PAGE_RE = re.compile(r"^第(\d+)页[:：]")


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url="https://api.deepseek.com", timeout=600.0)
    return _client


def check_volume_consistency(entries: list[LedgerEntry]) -> dict[int, str]:
    """输入一整卷的台账条目,返回 {页码: 异常原因} ——只包含被判定为可疑的页,不改动 entries
    本身,调用方自己决定怎么合并 flags(方便单独测试这个函数,不用每次都真的读写台账)。"""
    if not entries:
        return {}

    volume = entries[0].volume
    corpus = "\n\n".join(f"【第{e.page}页】\n{e.text}" for e in entries)
    prompt = CONSISTENCY_CHECK_PROMPT.format(volume=volume, corpus=corpus)

    client = _get_client()
    resp = client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=2000,
    )
    content = resp.choices[0].message.content or ""

    if "未发现异常页面" in content and len(content.strip()) < 20:
        return {}

    flagged: dict[int, str] = {}
    for line in content.splitlines():
        line = line.strip()
        m = _PAGE_RE.match(line)
        if m:
            page = int(m.group(1))
            reason = line[m.end() :].strip()
            flagged[page] = reason
    return flagged


def apply_consistency_check(entries: list[LedgerEntry]) -> list[LedgerEntry]:
    """跑一致性核对,把结果直接合并进 entries 的 flags 里(原地修改并返回同一个列表,
    方便 pipeline.py 里 `entries = apply_consistency_check(entries)` 这种写法)。"""
    flagged = check_volume_consistency(entries)
    if not flagged:
        return entries
    for e in entries:
        if e.page in flagged:
            if "possibly_misfiled" not in e.flags:
                e.flags.append("possibly_misfiled")
            e.flags.append(f"possibly_misfiled_reason:{flagged[e.page]}")
    return entries
