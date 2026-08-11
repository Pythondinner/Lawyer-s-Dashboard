"""Agent-2 深挖对话的核心处理逻辑。从最初只写在 cli/deep_dive.py 里的终端版本抽出来单独
成一个模块,原因是网页版(web/app.py)要跑一样的对话+工具调用逻辑,不能维护两份。

数据面接的是 cli/run_case.py 产出的 manifest.json:同一份台账(ledger)、同一份报告
(report.txt)两边共用,不重复存一份。新增两份专属于"深挖"这个动作的持久化文件:
- deep_dive_log.jsonl:完整对话历史,按案子累积(不是按 session),下次打开同一个案子会先把
  之前的对话读回去,律师能接着上次问的地方继续,而不是每次都从零开始。
- verification_log.json:律师明确确认/推翻某条发现时,追加写入的事件记录,不覆盖原 ledger。
  按(卷/页)定位,同一条目可以被多次确认/推翻,取最新一条为当前状态,历史记录不丢——这样
  "律师后来反悔了"这种情况,回看的时候能看到判断本身是怎么变化的,不是只能回到最初状态。

记录律师判断这一步,不让模型自己判断"律师是不是在明确表态"——这是从起诉书对照表的备注功能
学来的教训,同一条"硬接口优于自然语言理解"的原则这里也要贯彻到底。模型只能调用
`propose_finding` **提议**记一笔,这一步不会真的写进 verification_log.json;真正落盘只能
靠调用方(网页版的确认/推翻按钮、终端版输入 y/n 这种固定信号)显式触发的 confirm_finding()。
模型负责判断"这个值得提出来问一句",人负责判断"是不是真的要记"，两件事分开,不再让模型
一个人说了算。

历史记录以磁盘上的 deep_dive_log.jsonl 为唯一真相来源,每次调用都从文件重新加载,不在内存
里长期持有状态——网页版每个 HTTP 请求本来就是无状态的,这样两边(终端/网页)行为一致,
服务重启、多个浏览器标签页同时开着,都不会有状态对不上的问题。

第三份文件 interaction_debug_log.jsonl 是给"测试的时候回头复盘"用的,记的东西比
deep_dive_log.jsonl 更细:每一轮完整的工具调用参数和结果(不只是"核实了N条引用"这种
人类可读摘要)、这一轮花了多少秒、以及一个用关键词粗判的"疑似漏判确认"标记——如果律师这句话
里出现了"确认/对的/没错/推翻/不对"这类词,但模型这一轮没有调用 propose_finding,就标一下。
这个标记只是给人回头看用的线索,不代表真的漏判了(也可能律师只是在陈述、不是在表态),更不会
拿去驱动任何自动化动作——真正会不会调用工具,还是模型自己判断,这里不越权替它做决定。"""

import json
import os
import time
from datetime import datetime, timezone

from dotenv import load_dotenv
from openai import OpenAI

from analysis.deep_read import build_corpus
from analysis.deep_read_agentic import VERIFY_TOOL, _execute_verify_citations
from verification.quote_check import build_ledger_index, build_pages_by_volume

load_dotenv()

_client = None

PROPOSE_FINDING_TOOL = {
    "type": "function",
    "function": {
        "name": "propose_finding",
        "description": (
            "当你觉得某条发现值得让律师明确确认或者推翻的时候,调用这个工具**提议**记一笔——"
            "这不会直接写进正式记录,只是把候选提交给律师看,由律师自己点击确认或推翻,你不用、"
            "也不能替律师做这个判断。不确定要不要提议的时候,倾向于提议(交给律师筛选成本很低),"
            "但不要对律师只是随口讨论、还完全没有形成明确意见的内容也提议。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "volume": {"type": "string", "description": "卷宗标签,比如'卷2'"},
                "page": {"type": "integer", "description": "页码"},
                "finding": {"type": "string", "description": "简要描述这条发现是什么,方便律师确认时看清楚在说什么"},
                "note": {"type": "string", "description": "补充说明,没有就留空字符串"},
            },
            "required": ["volume", "page", "finding"],
        },
    },
}

SYSTEM_PROMPT_TEMPLATE = """你是刑事辩护律师的阅卷深挖助手。律师已经看过下面这份初步分析报告
(由另一个自动化流程生成),现在想针对报告里的具体发现、或者卷宗里报告没提到的内容,继续追问。

## 任务边界(跟生成报告时完全一样,没有放宽)

你的任务还是"事实还原",不是"主观定性判断"。回答问题时只依据卷宗原文和已有报告,不要:
- 引用具体法条(比如"《XX法》第X条")或者下法律结论(比如"这违反了XX规定")——法律判断
  完全交给律师;
- 对动机、主观明知程度、共同犯罪中的角色(主犯/从犯)、罪名定性下结论。

如果律师问的问题,答案不在卷宗内容里、需要查询外部资料(法条、类案、资质名录等)才能回答,
明确告诉律师"这个问题不在卷宗内部能找到答案,需要你自己核实",不要编。

## 引用规则

如果你的回答里提到卷宗具体内容,必须标注【卷宗标签 第Y页】,并且用 verify_citations 工具
核实(即使律师没要求,你自己主动核实,发现核实结果是 not_found 或者页码不对,要如实告诉律师,
不要悄悄改成好像一直是对的)。

## 提议记录发现

聊到某条发现,如果你觉得这个值得留痕(不管是律师看起来认可、还是有异议、还是你自己核实后
觉得重要),调用 propose_finding 工具**提议**一下。这一步不是"记录律师的判断"——你不用去
判断律师是不是在明确表态,那是律师自己的事:你提议之后,系统会在界面上给律师一个明确的
确认/推翻的入口,由律师自己点,不是你替他判断。你的任务只是"覆盖率要高"(该提的都提),
不是"准确判断律师的语气"。

## 下面是卷宗原文(带【卷+页码】标注):

{corpus}

## 下面是已有的初步分析报告:

{report}
"""


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url="https://api.deepseek.com", timeout=600.0)
    return _client


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_jsonl(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _append_jsonl(path: str, obj: dict) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _append_verification(path: str, event: dict) -> None:
    events = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            events = json.load(f)
    events.append(event)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(events, f, ensure_ascii=False, indent=2)


def confirm_finding(case: "DeepDiveCase", proposal: dict, status: str) -> dict:
    """把一条候选正式落盘成 verification_log.json 里的一条记录——这是整个"提议/确认"分离
    设计里,唯一真正写入正式记录的入口,只能由调用方(网页按钮点击、终端固定指令)显式调用,
    不是模型自己触发的。status 只能是 "confirmed" 或 "overturned",由律师决定,不是模型。"""
    event = {
        "volume": proposal.get("volume"),
        "page": proposal.get("page"),
        "finding": proposal.get("finding"),
        "status": status,
        "note": proposal.get("note", ""),
        "timestamp": _now(),
    }
    _append_verification(case.verification_log_path, event)
    return event


class DeepDiveCase:
    """一个案子的深挖上下文——语料、报告、system prompt、ledger 索引都不随对话轮次变化,
    加载一次、多轮复用。终端版和网页版都用这个类,不重复读文件、不重复拼 system prompt。"""

    def __init__(self, case_dir: str):
        self.case_dir = case_dir
        with open(os.path.join(case_dir, "manifest.json"), "r", encoding="utf-8") as f:
            self.manifest = json.load(f)
        with open(self.manifest["report_path"], "r", encoding="utf-8") as f:
            self.report = f.read()

        # 时间线是老案子(改动之前跑的)可能没有的可选产出,manifest 里没有就是 None。
        self.timeline_path = self.manifest.get("timeline_path")

        corpus = build_corpus(self.manifest["ledger_paths"])
        self.system_prompt = SYSTEM_PROMPT_TEMPLATE.format(corpus=corpus, report=self.report)
        self.ledger_index = build_ledger_index(self.manifest["ledger_paths"])
        self.pages_by_volume = build_pages_by_volume(self.ledger_index)

        self.dialogue_log_path = os.path.join(case_dir, "deep_dive_log.jsonl")
        self.verification_log_path = os.path.join(case_dir, "verification_log.json")
        self.debug_log_path = os.path.join(case_dir, "interaction_debug_log.jsonl")

        # 起诉书/起诉意见书核对是可选的、而且可能同时有两种——manifest 里的 indictments 是个
        # 列表,每项对应一份已经律师确认过的文书(type/comparison_path/comparison_docx_path)。
        # 老格式(单一 indictment_comparison_path 字段)在这里兼容一下,不强迫已经跑过的老案子
        # 重新处理一遍才能继续用——只在读的时候兼容,新产出的案子一律走列表格式。
        self.indictments = self.manifest.get("indictments")
        if self.indictments is None:
            legacy_path = self.manifest.get("indictment_comparison_path")
            self.indictments = (
                [
                    {
                        "type": "起诉书",
                        "comparison_path": legacy_path,
                        "comparison_docx_path": self.manifest.get("indictment_comparison_docx_path"),
                        "healthy": self.manifest.get("indictment_comparison_healthy"),
                    }
                ]
                if legacy_path
                else []
            )
        self.indictment_candidates = self.manifest.get("indictment_candidates", [])
        self._legacy_single_indictment = self.manifest.get("indictments") is None

    def indictment_annotations_path(self, doc_type: str) -> str:
        """备注文件按文书类型分开存,起诉书和起诉意见书各自的备注不会串。老格式的案子
        (只可能有一份、且一定是"起诉书")沿用原来的文件名,不然已经写过的备注会读不到。"""
        if self._legacy_single_indictment and doc_type == "起诉书":
            return os.path.join(self.case_dir, "indictment_annotations.json")
        return os.path.join(self.case_dir, f"indictment_annotations_{doc_type}.json")

    def load_history(self) -> list[dict]:
        """从磁盘重新读——不缓存在内存里,见模块开头的说明。"""
        turns = _load_jsonl(self.dialogue_log_path)
        return [{"role": t["role"], "content": t["content"]} for t in turns if t["role"] in ("user", "assistant")]


# 粗判"这句话听起来像在表态"的关键词——只用来给调试日志打一个"这轮可能有该确认的候选"的
# 线索标记,不驱动任何实际动作。宁可多标、不要漏标,回头人工看的时候筛掉误报比漏掉真正的
# 漏判成本低。
_CONFIRMATION_CUE_WORDS = ("确认", "对的", "没错", "是对的", "推翻", "不对", "错了", "记一下", "记下", "撤销", "反悔")


def _looks_like_confirmation(text: str) -> bool:
    return any(w in text for w in _CONFIRMATION_CUE_WORDS)


def process_turn(case: DeepDiveCase, history: list[dict], user_input: str) -> tuple[str, list[str], list[dict]]:
    """处理一轮对话:追加用户输入、跑工具调用循环直到模型给出最终文字回复,期间产生的
    verify_citations 调用会真正执行并落盘;propose_finding 调用**不会**落盘,只会收集进
    返回值里的候选列表,真正写入 verification_log.json 要调用方(终端/网页)显式调用
    confirm_finding() 才会发生——这是"提议/确认"分离设计的核心,模型判断"值不值得问",
    人判断"是不是真的确认",两件事不再混在一起由模型一个人说了算。

    返回 (助手回复文本, 工具事件摘要列表, 待确认候选列表)。候选列表里每一项是
    {volume, page, finding, note},调用方展示给律师、律师确认或推翻时把整条候选原样传给
    confirm_finding()。

    user_input 和最终回复都会追加写入 case.dialogue_log_path,调用方不需要自己再存一次。
    另外会把这一轮的完整细节写进 case.debug_log_path,供测试完之后回头复盘用——出了任何
    异常也会记一条错误日志再重新抛出,不会让一次报错就悄悄丢掉这一轮发生了什么。"""
    client = _get_client()
    tools = [VERIFY_TOOL[0], PROPOSE_FINDING_TOOL]

    messages = [{"role": "system", "content": case.system_prompt}] + history + [{"role": "user", "content": user_input}]
    _append_jsonl(case.dialogue_log_path, {"role": "user", "content": user_input, "timestamp": _now()})

    tool_events: list[str] = []
    debug_tool_calls: list[dict] = []
    pending_proposals: list[dict] = []
    t0 = time.time()

    try:
        # 一轮对话里模型可能连续发起多次工具调用(先核实引用,再提议记一笔),
        # 循环处理直到模型不再要求调用工具为止。
        for _ in range(5):  # 安全上限,防止模型陷入死循环式的工具调用
            resp = client.chat.completions.create(
                model="deepseek-chat", messages=messages, tools=tools, tool_choice="auto", max_tokens=4000
            )
            msg = resp.choices[0].message

            if not msg.tool_calls:
                reply = msg.content or ""
                _append_jsonl(case.dialogue_log_path, {"role": "assistant", "content": reply, "timestamp": _now()})
                called_propose_finding = any(c["name"] == "propose_finding" for c in debug_tool_calls)
                _append_jsonl(
                    case.debug_log_path,
                    {
                        "timestamp": _now(),
                        "user_input": user_input,
                        "assistant_reply": reply,
                        "tool_calls": debug_tool_calls,
                        "latency_seconds": round(time.time() - t0, 2),
                        "possible_missed_confirmation": _looks_like_confirmation(user_input) and not called_propose_finding,
                    },
                )
                return reply, tool_events, pending_proposals

            messages.append(
                {
                    "role": "assistant",
                    "content": msg.content,
                    "tool_calls": [
                        {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                        for tc in msg.tool_calls
                    ],
                }
            )

            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}

                if tc.function.name == "verify_citations":
                    items = args.get("items", [])
                    result = _execute_verify_citations(items, case.ledger_index, case.pages_by_volume)
                    tool_events.append(f"核实了{len(items)}条引用")
                elif tc.function.name == "propose_finding":
                    proposal = {
                        "volume": args.get("volume"),
                        "page": args.get("page"),
                        "finding": args.get("finding"),
                        "note": args.get("note", ""),
                    }
                    pending_proposals.append(proposal)
                    # 工具本身"成功"了(候选已经被收集、会展示给律师),但没有真的写入正式记录,
                    # 用一句明确的话告诉模型这一点,避免它误以为记录已经生效。
                    result = {"ok": True, "note": "已作为候选提交给律师,等待律师在界面上确认或推翻,还没有写入正式记录"}
                    tool_events.append(f"提议记录候选: {proposal['finding']}")
                else:
                    result = {"error": f"未知工具 {tc.function.name}"}

                debug_tool_calls.append({"name": tc.function.name, "arguments": args, "result": result})
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result, ensure_ascii=False)})

        tool_events.append("连续工具调用次数过多,已跳过这一轮")
        fallback_reply = "抱歉,这一轮处理时连续多次调用工具没有收敛,请重新提问或者换个问法。"
        _append_jsonl(case.dialogue_log_path, {"role": "assistant", "content": fallback_reply, "timestamp": _now()})
        _append_jsonl(
            case.debug_log_path,
            {
                "timestamp": _now(),
                "user_input": user_input,
                "assistant_reply": fallback_reply,
                "tool_calls": debug_tool_calls,
                "latency_seconds": round(time.time() - t0, 2),
                "possible_missed_confirmation": False,
                "note": "连续工具调用次数过多",
            },
        )
        return fallback_reply, tool_events, pending_proposals
    except Exception as e:
        # 出错也要留痕——这一轮到底问了什么、跑了多久才报错,不能因为报错就什么都没留下,
        # 调用方(终端/网页)该怎么处理这个异常是它们的事,这里只负责别让信息悄悄丢掉。
        _append_jsonl(
            case.debug_log_path,
            {
                "timestamp": _now(),
                "user_input": user_input,
                "error": f"{type(e).__name__}: {e}",
                "tool_calls": debug_tool_calls,
                "latency_seconds": round(time.time() - t0, 2),
            },
        )
        raise
