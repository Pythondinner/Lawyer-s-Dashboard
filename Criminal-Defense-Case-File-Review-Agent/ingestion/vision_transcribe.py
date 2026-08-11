"""拍照/扫描页的视觉识别。整理自 probe_vision.py 里验证过的 prompt 和分层规则,
改成输出结构化 JSON,方便管道直接落到证据台账里,不用再解析自由文本。"""

import base64
import json
import os

from openai import OpenAI

_client = None

# 粗略估算用,不是账单真实单价——qwen-vl-plus 国际站公开价换算,你这个独享工作空间的实际单价
# 可能不一样,这里只是给你一个"大概花了多少"的实时参考,不能当作最终账单。
_ESTIMATED_PRICE_PER_M_INPUT_CNY = 1.5
_ESTIMATED_PRICE_PER_M_OUTPUT_CNY = 4.5

usage_totals = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}


def print_usage_summary() -> None:
    p, c = usage_totals["prompt_tokens"], usage_totals["completion_tokens"]
    est_cost = p / 1_000_000 * _ESTIMATED_PRICE_PER_M_INPUT_CNY + c / 1_000_000 * _ESTIMATED_PRICE_PER_M_OUTPUT_CNY
    print(
        f"[用量汇总] 调用{usage_totals['calls']}次, "
        f"输入{p}tokens, 输出{c}tokens, 估算花费约{est_cost:.3f}元(粗略估算,不是真实账单)"
    )

SYSTEM_PROMPT = """你在阅读一份刑事案件卷宗的拍照页面。请按以下规则处理这一页,输出 JSON。

分层处理规则:
1. 正式文书/表格/正文(笔录、鉴定意见、行政文书等):完整转录文字,保留表格结构,content_type="formal_document"。
2. 账单/流水表格:逐行精确转录每一个数字,不要省略中间行,金额/日期/流水号必须跟原文一模一样,content_type="transaction_table"。
3. 多张手机截图拼在一页(2张或4张):先数清楚总共几张,严格按"左上→右上→左下→右下"顺序,每张单独作为一个 segment 输出,不能因为篇幅遗漏任何一张。每个 segment 的 content_type="chat_screenshot",text 里包含时间、聊天对象,以及**逐字转录的对话原文**(每一条消息都要原样打出来,不允许写"内容涉及XX""讨论了XX"这种概括性描述——概括会把具体措辞这种关键证据抹掉,必须是对方原话)。如果某条消息因为字迹小/模糊确实认不清,写"[看不清]"标注,不要因为看不清就跳过整条改成概括。
4. 签名/手印/公章/空白边角:不逐字转录,只用一句话描述状态(比如"此处有本人签名确认"),content_type="signature_seal"。
5. 如果整页(或某个 segment)是倒置/明显旋转的,在该 segment 的 flags 里加 "rotated"。
6. 如果内容明显跟其他材料不属于同一案子/同一批(出现不相关人名、办案机关),在该 segment 的 flags 里加 "possibly_misfiled"。
7. 如果字迹模糊、图片质量差、印章盖住了文字、或者其他原因导致你对自己转录的准确性没有把握(不确
定是不是每个字都对),在该 segment 的 flags 里加 "low_confidence"——不要因为没把握就编一个看起来
合理的内容,宁可转录得不确定,也不要编。这不是失败,是诚实的标注,后面会提示律师去核实原图,不
会因为你标了这个而扣分。

只输出如下 JSON,不要输出其他文字:
{"segments": [{"content_type": "...", "text": "...", "flags": []}, ...]}"""


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=os.environ["VISION_API_KEY"], base_url=os.environ["VISION_API_BASE"], timeout=180.0)
    return _client


def _repair_json(content: str) -> dict:
    """这个模型偶尔会在输出完整 JSON 后,多打一串空白行,却漏掉最后收尾的 '}'。
    内容本身是完整的,补一个收尾符号就能解析,不需要重新调用模型。"""
    content = content.strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    if not content.endswith("}"):
        try:
            return json.loads(content + "}")
        except json.JSONDecodeError:
            pass
    raise ValueError("无法修复的 JSON: " + content[:200])


_BASE_MAX_TOKENS = 6000
_MAX_TOKENS_CAP = 16000


def transcribe_page(image_bytes: bytes, _retries: int = 2) -> list[dict]:
    """返回这一页拆出来的 segment 列表,每条是 {"content_type", "text", "flags"}。

    JSON 解析失败分两种不同的原因,不能用同一招应付:
    1. finish_reason == "length"(输出撞到 max_tokens 上限被截断,通常发生在账单流水表格、
       多张聊天截图这类要求"逐字转录、不许省略"的页面,内容本身就长)——这种情况原样重试
       没用,大概率还是撞在同一个上限上,必须真的调大预算才有意义,所以每次重试把上限翻倍
       (封顶 16000)。
    2. 其他情况(比如输出完整但漏了收尾的 "}")——不是长度问题,重试时不改变预算,交给
       _repair_json 去修,修不了才真的重试请求。"""
    client = _get_client()
    model = os.environ["VISION_MODEL"]
    b64 = base64.b64encode(image_bytes).decode("utf-8")

    content = ""
    finish_reason = None
    max_tokens = _BASE_MAX_TOKENS
    for attempt in range(_retries + 1):
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": SYSTEM_PROMPT},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    ],
                }
            ],
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
        usage_totals["prompt_tokens"] += resp.usage.prompt_tokens
        usage_totals["completion_tokens"] += resp.usage.completion_tokens
        usage_totals["calls"] += 1

        content = resp.choices[0].message.content
        finish_reason = resp.choices[0].finish_reason
        try:
            parsed = _repair_json(content)
            return parsed.get("segments", [])
        except ValueError:
            is_last = attempt >= _retries
            reason_note = f"finish_reason={finish_reason}"
            if finish_reason == "length" and max_tokens < _MAX_TOKENS_CAP:
                max_tokens = min(max_tokens * 2, _MAX_TOKENS_CAP)
                reason_note += f",很可能是被截断的,下一次预算调到{max_tokens}"
            print(f"    (第{attempt + 1}次JSON解析失败[{reason_note}],{'放弃,原样保存' if is_last else '重试'})")

    # 重试用完还是解析不了:不丢内容,把原始文本整段存下来,标记待人工核实,同时把最后一次的
    # finish_reason 也存进 flags——比"失败了"多一点线索,回头人工复核或者以后再调参数都用得上。
    return [{"content_type": "other", "text": content, "flags": ["json_parse_failed", f"finish_reason:{finish_reason}"]}]
