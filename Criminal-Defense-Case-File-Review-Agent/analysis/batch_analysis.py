"""案子大到一次装不进上下文窗口的时候用这个:按卷分批(不打散单卷),
每批独立跑一遍 deep_read_agentic(读全部内容,不做相似度筛选,只是分批读),
最后合并成一份报告,并且专门做一次"找批与批之间关联"的整合,不是简单拼接。"""

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv
from openai import OpenAI

from analysis.deep_read import PRINCIPLE_CATEGORY_NAMES, SCOPE_LIMITATION_NOTICE
from analysis.deep_read_agentic import run_deep_read_agentic
from analysis.report_health import check_report, estimate_max_tokens, looks_corrupted

load_dotenv()

_client = None
DEFAULT_TOKEN_BUDGET = 150_000  # 留够余量给 prompt+多轮工具调用的开销,不要卡着上限走


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url="https://api.deepseek.com", timeout=600.0)
    return _client


def estimate_tokens(ledger_path: str) -> int:
    with open(ledger_path, "r", encoding="utf-8") as f:
        entries = json.load(f)
    chars = sum(len(e["text"]) for e in entries)
    return int(chars / 1.5)  # 中文粗估,跟之前几次估算corpus大小用的比例一致


def plan_batches(ledger_paths: list[str], token_budget: int = DEFAULT_TOKEN_BUDGET) -> list[list[str]]:
    """按卷贪心装箱,不拆单卷。单卷自己就超预算的话,单独成一批,没法再拆了就实话实说地超一点。"""
    sized = [(p, estimate_tokens(p)) for p in ledger_paths]

    batches: list[list[str]] = []
    current: list[str] = []
    current_size = 0
    for path, size in sized:
        if current and current_size + size > token_budget:
            batches.append(current)
            current, current_size = [], 0
        current.append(path)
        current_size += size
    if current:
        batches.append(current)
    return batches


CONSOLIDATE_ACROSS_BATCHES_PROMPT = """下面是同一个案子的卷宗材料,因为体量太大装不进一次分析,分成了{n}批分别独立分析,
每批各自产出了一份报告。这些批次之间,内容是不重叠的(每一卷只在一批里出现过一次),所以你的任务
跟"合并重复发现"不一样,重点是:

1. 逐条保留每一批报告里的发现,不要因为整合就删减内容,原有的页码和原文摘句都要保留。**引用格式
是硬性要求**:每一条引用必须保持【卷宗标签 第Y页】+ 描述 + 原文:"摘句" 这个固定格式,"原文:"
后面必须跟着英文或中文引号包起来的原文,不允许改写成加粗、不加引号或者其他格式——后面有工具
会用正则表达式去匹配"原文:"这个固定文字加引号,格式不对会导致核验失败。
2. **重点任务,按下面这个具体步骤做,不要只是笼统地"看看有没有关联"**:
   a. 先把{n}批报告里出现过的所有关键数量/金额类数字(比如鉴定量、破坏价值、炸药/雷管用量、
   产量估算、价格、面积、时间跨度等)逐一列一遍清单,不管它在哪一批出现的。
   b. 对清单里的每一个数字,回头检查其余{n}-1批的发现里,有没有别的数据、公式、物理量(比如
   用炸药量反推理论产量上限、用面积×体重反推资源量、用单价×数量反推总价)可以用来验证、
   约束或者对比这个数字——即使这两批报告表面上讨论的是不同的人物/情节/证据类型,只要数字
   或者物理量之间存在可计算的关联,就要检查。
   c. 一批里的证人证言,跟另一批里的书证时间线对得上或对不上,也按同样方式检查。
   找到这种"跨批关联",单独作为新的一条发现列出来,并标注引用来自哪一批、具体哪一页,不要凭空
   猜测,要有具体的页码和原文依据;确实没找到就不用编,但前面 a/b/c 这几步逐项检查过一遍这件事
   本身不能跳过。
3. 按原来的{n_categories}类问题分组结构组织({categories}),跨批关联的发现放进最贴切的
类别里,并且在开头标注"[跨批关联]"。

{n}批报告如下:

{reports}

请输出整合后的最终报告。"""


def _cache_key(paths: list[str]) -> str:
    """按文件名(不含目录)算一个稳定的短哈希,用来给缓存路径打上"这是哪些卷宗"的标识。"""
    key = "|".join(sorted(os.path.basename(p) for p in paths))
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


def run_batch_analysis(
    ledger_paths: list[str],
    token_budget: int = DEFAULT_TOKEN_BUDGET,
    max_tokens: int = 8000,
    cache_dir: str | None = "data/batch_cache",
) -> tuple[str, dict, list[list[str]], bool]:
    """返回 (最终报告, usage统计, 实际分批方案, 是否健康)。

    每一批的中间结果会存到 cache_dir(如果给了的话)——分批分析这一步比整合贵得多,
    如果只是最后整合步骤的输出长度不够、要重跑,不应该连前面贵的部分也重新花钱跑一遍。

    healthy 目前有个已知的不精确之处:命中缓存的批次(直接读磁盘上之前跑好的文本)拿不到
    它当初是不是健康的信息,只能假设是——缓存文件本身不记这个状态。如果这个假设需要收紧
    (比如缓存也应该连健康状态一起存),后续可以再改,现在先用这个简化版本。"""
    batches = plan_batches(ledger_paths, token_budget)
    print(f"案子分成了{len(batches)}批:")
    for i, b in enumerate(batches, 1):
        names = [os.path.basename(p) for p in b]
        print(f"  批{i}: {names}")

    # 缓存原来只按批次序号命名(batch_0.txt/batch_1.txt...),不区分是哪个案子——不同案子
    # 分批数量一样时,序号会撞上,读到的是另一个案子的旧缓存却不会报错(实测过,珍惜动物案的
    # 缓存被非法采矿案的重跑悄悄复用,整合出来的报告内容完全对不上)。改成缓存路径按"这个
    # 案子的全部卷宗文件名"分一层子目录,同一批具体由哪几份卷宗组成再算一层哈希当文件名——
    # 双重隔离,不同案子、同一案子换了分批方案(比如改了token_budget),都不会撞缓存。
    if cache_dir:
        cache_dir = os.path.join(cache_dir, _cache_key(ledger_paths))
        os.makedirs(cache_dir, exist_ok=True)

    total_usage = {"prompt_tokens": 0, "completion_tokens": 0}
    batch_reports: list[str | None] = [None] * len(batches)
    any_batch_unhealthy = False
    cache_paths = [os.path.join(cache_dir, f"batch_{_cache_key(b)}.txt") if cache_dir else None for b in batches]

    to_run = []
    for i, cache_path in enumerate(cache_paths):
        if cache_path and os.path.exists(cache_path):
            with open(cache_path, "r", encoding="utf-8") as f:
                batch_reports[i] = f.read()
            print(f"  批{i + 1}: 命中缓存,跳过重新分析")
        else:
            to_run.append(i)

    if to_run:
        print(f"\n开始并发跑{len(to_run)}批(其余命中缓存)...")
        with ThreadPoolExecutor(max_workers=min(len(to_run), 6)) as pool:
            futures = {pool.submit(run_deep_read_agentic, batches[i], max_tokens): i for i in to_run}
            for future in as_completed(futures):
                i = futures[future]
                report, usage, batch_healthy = future.result()
                cleaned = report.replace(SCOPE_LIMITATION_NOTICE, "").strip()
                batch_reports[i] = cleaned
                total_usage["prompt_tokens"] += usage["prompt_tokens"]
                total_usage["completion_tokens"] += usage["completion_tokens"]
                if not batch_healthy:
                    any_batch_unhealthy = True
                if cache_paths[i]:
                    with open(cache_paths[i], "w", encoding="utf-8") as f:
                        f.write(cleaned)
                print(f"  批{i + 1}完成(healthy={batch_healthy})")

    if len(batches) == 1:
        # 只有一批,不需要跨批整合这一步
        return batch_reports[0] + SCOPE_LIMITATION_NOTICE, total_usage, batches, not any_batch_unhealthy

    print("整合跨批发现...")
    reports_block = "\n\n".join(f"===== 第{i + 1}批报告 =====\n{r}" for i, r in enumerate(batch_reports))
    prompt = CONSOLIDATE_ACROSS_BATCHES_PROMPT.format(
        n=len(batches),
        reports=reports_block,
        n_categories=len(PRINCIPLE_CATEGORY_NAMES),
        categories="/".join(PRINCIPLE_CATEGORY_NAMES),
    )

    # 整合步骤要把N批报告的全部发现原样保留(严格格式,比之前的简写更占token)、还要新增跨批发现,
    # 按待整合内容的实际长度动态估算,不再靠"批数×固定系数"这种跟内容详略程度脱节的估算方式。
    consolidate_max_tokens = estimate_max_tokens(reports_block, floor=max_tokens)

    client = _get_client()
    resp = client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=consolidate_max_tokens,
    )
    total_usage["prompt_tokens"] += resp.usage.prompt_tokens
    total_usage["completion_tokens"] += resp.usage.completion_tokens

    final_report, consolidate_healthy = check_report(resp.choices[0].message.content, resp.choices[0].finish_reason)
    if not consolidate_healthy and looks_corrupted(final_report):
        # 损坏是模型采样偶发的噪声,换一次采样很可能就不再出现,不该一遇到就直接放弃整批
        # 跨批分析——重试一次(参数不变),还不行才真的退回未整合的拼接。
        print("  整合输出里混入了格式错乱的工具调用文本,重试一次(换一次采样)...")
        resp = client.chat.completions.create(
            model="deepseek-chat", messages=[{"role": "user", "content": prompt}], max_tokens=consolidate_max_tokens
        )
        total_usage["prompt_tokens"] += resp.usage.prompt_tokens
        total_usage["completion_tokens"] += resp.usage.completion_tokens
        final_report, consolidate_healthy = check_report(resp.choices[0].message.content, resp.choices[0].finish_reason)

    if not consolidate_healthy:
        # 同 consensus.py 的处理方式:整合这一步没有真实工具可调用,输出夹带工具调用格式痕迹
        # 就整段判废;没有单批报告可以退回,退而求其次直接拼接各批未整合的原始报告交付,
        # 保留全部真实发现,只是没有"跨批关联"这个增量分析。
        print("  整合输出里混入了格式错乱的工具调用文本,重试后依然如此,判定为损坏,返回未整合的分批报告拼接")
        final_report = (
            f"[系统提示:{len(batches)}批分析结果的跨批整合步骤失败,以下是未经跨批关联分析的原始拼接。]\n\n"
            + reports_block
        )

    return final_report + SCOPE_LIMITATION_NOTICE, total_usage, batches, consolidate_healthy and not any_batch_unhealthy


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("ledger_paths", nargs="+")
    parser.add_argument("--token-budget", type=int, default=DEFAULT_TOKEN_BUDGET)
    parser.add_argument("--out", default="analysis_batch_result.txt")
    parser.add_argument("--max-tokens", type=int, default=8000)
    args = parser.parse_args()

    text, usage, batches, healthy = run_batch_analysis(args.ledger_paths, args.token_budget, args.max_tokens)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"[tokens] prompt={usage['prompt_tokens']} completion={usage['completion_tokens']}")
    print(f"结果写入 {args.out}(healthy={healthy})")
