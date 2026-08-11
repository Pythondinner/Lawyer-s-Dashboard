"""笔杆子(Writer):记者→笔杆子流水线的第二阶段。每个主题独立、并行调用一次——
复用 Multi-Agent 已经验证过的并行调度模式(ThreadPoolExecutor),只是这次并行的是"写作",
不是"检索"。只负责把记者已经核实、分好类的一个主题写成报告里的一个板块,不负责判断主题划分本身。
"""

import json
import os

import openai
from openai import OpenAI

from context import today_str
from events import emit
from retry import with_retry

WRITER_SYSTEM_PROMPT = """你是一位经验丰富的调查记者/撰稿人,负责把编辑部核实好的素材写成报告里的一个板块,
要写得像样的深度报道内容,不是简单罗列干巴巴的条目。

规则:
- 先判断这批事实最适合用哪种呈现方式:
  - comparison_table: 适合多个来源/多个指标的数值对比
  - timeline: 适合随时间演变的信息(有具体日期的事件)
  - steps: 适合有明确先后顺序的操作流程(比如菜谱步骤、教程、操作指南)——如果事实本质是
    "第一步做什么、第二步做什么"这种可以照着执行的流程,优先用这个,不要拆成几段说明文字
    让读者自己拼凑步骤
  - item_list: 适合资讯类、非对比类、彼此并列没有顺序关系的信息罗列
  - narrative: 适合写成一段连贯的叙述文字,不必强行拆成条目
- 尽量把给你的这个主题下所有相关事实都体现出来,不要只挑一两条敷衍了事。
- 每条事实性表述,必须标注它依据的 source_id(必须用给你的事实列表里已有的 source_id,不要编造新的;
  一条事实如果有多个 source_ids,都要标上)。source_id 只能放进对应字段(values/events/items 里的
  source_id 字段,或者 narrative 的 citations 数组),正文文字里绝对不能出现"s123"这种原始编号或者
  "据s123报道"这种写法——narrative 类型尤其要注意,引用一律放进 citations 数组,不要写进 text 正文里。
- 每条事实都带了 corroboration 标记("single"=只有一个来源,"multiple"=多个来源印证)。这个标记
  不要原样出现在你写的文字里,要转化成行文措辞体现:multiple 用"据多方报道"/"多个信源证实"这类措辞;
  single 用"据XX报道"/"消息人士透露,尚未进一步证实"这类措辞,不要把单一来源的信息写得跟确凿事实一样
  斩钉截铁。

只输出如下 JSON,不要输出其他文字,title 直接用给你的主题名。根据你选的 type,字段结构分别是:
- comparison_table: {"title": "...", "type": "comparison_table", "columns": [...], "rows": [{"label": "...", "values": [{"value": "...", "source_id": "..."}]}]}
- timeline: {"title": "...", "type": "timeline", "events": [{"date": "...", "description": "...", "source_id": "..."}]}
- steps: {"title": "...", "type": "steps", "steps": [{"description": "...", "source_id": "..."}]}(steps 数组顺序就是操作顺序,不需要单独的序号字段)
- item_list: {"title": "...", "type": "item_list", "items": [{"headline": "...", "detail": "...", "source_id": "..."}]}
- narrative: {"title": "...", "type": "narrative", "text": "...", "citations": ["source_id", ...]}
"""


@with_retry(max_retries=2, base_delay=1.5, exceptions=(openai.APIError,))
def _call_llm(question: str, theme: dict, correction_note: str | None) -> dict:
    client = OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url="https://api.deepseek.com")
    user_content = (
        f"今天的日期是{today_str()}——写'近期'/'最新'这类措辞时以这个日期为准。\n\n"
        f"研究问题: {question}\n\n主题: {theme.get('title')}\n\n"
        f"事实列表:\n{json.dumps(theme.get('facts', []), ensure_ascii=False, indent=2)}"
    )
    if correction_note:
        user_content += f"\n\n{correction_note}"

    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": WRITER_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        response_format={"type": "json_object"},
        temperature=0,
        max_tokens=8000,
    )
    return json.loads(response.choices[0].message.content)


def write_section(question: str, theme: dict, correction_note: str | None = None, on_event=None) -> dict:
    section = _call_llm(question, theme, correction_note)
    section.setdefault("title", theme.get("title"))
    emit(f"[笔杆子]《{section.get('title')}》完稿", on_event)
    return section
