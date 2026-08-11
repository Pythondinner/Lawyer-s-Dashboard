"""原生文字页的提取 + 清洗。清洗目标是简报里提到的那类归档水印噪声——
同一页里反复出现的短字符串(比如"隆林县院_徐美美14501201511174335"),
不是正文内容,是扫描/归档流程叠加上去的,需要过滤掉。"""

from collections import Counter


def extract_and_clean(page) -> str:
    raw = page.get_text() or ""
    lines = [line.strip() for line in raw.splitlines()]
    lines = [line for line in lines if line]

    # 同一页里出现3次以上的短行(<=40字符),大概率是水印/条码噪声,不是正文
    counts = Counter(line for line in lines if len(line) <= 40)
    noise = {line for line, n in counts.items() if n >= 3}

    cleaned = [line for line in lines if line not in noise]
    return "\n".join(cleaned)
