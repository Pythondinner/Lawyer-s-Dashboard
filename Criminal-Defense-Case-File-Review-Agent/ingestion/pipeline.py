"""摄取层入口:逐页判断格式,分别走原生文字/视觉识别路径,汇总成证据台账。

视觉识别这部分用线程池并发跑——每页是独立的网络请求,互相不依赖,没必要排队等。
原生文字页本地解析很快,不需要并发。"""

import argparse
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import fitz
from dotenv import load_dotenv

from ingestion.consistency_check import check_volume_consistency
from ingestion.format_detect import page_format
from ingestion.ledger import LedgerEntry, save_ledger
from ingestion.native_extract import extract_and_clean
from ingestion.vision_transcribe import print_usage_summary, transcribe_page

load_dotenv()

MAX_WORKERS = 8
_print_lock = threading.Lock()

# 原生文字页清洗完之后,如果剩下的字符数低于这个阈值,大概率是"整页基本是空的/只剩水印",
# 不代表这一页真的没内容,可能是这页本来就模糊、或者原文本来就很短——不管哪种,都不该当成
# 一条正常记录悄悄放进台账,要标出来让律师自己确认要不要去看原图。
NATIVE_TEXT_SUSPICIOUSLY_SHORT = 40


class EncryptedPDFError(Exception):
    """PDF 加密且没能打开(没给密码,或者密码不对)。之前遇到过一次(珍惜动物案第十二卷),
    是手动解密替换文件绕过去的,不在正常流程里——现在改成显式抛出这个专门的异常,让
    批量处理多份卷宗的调用方可以专门捕获它,把这一份跳过、清楚记下跳过原因,不用因为
    一份文件加密就让整个批次的其他卷宗也处理不了。"""


def _process_vision_page(
    source_file: str, volume_label: str, page_no: int, total: int, image_bytes: bytes, is_fallback: bool = False
) -> list[LedgerEntry]:
    tag = "vision(补救)" if is_fallback else "vision"
    try:
        segments = transcribe_page(image_bytes)
    except Exception as e:
        with _print_lock:
            print(f"[{source_file}] 第{page_no}/{total}页 -> {tag}  识别失败: {e}")
        return [
            LedgerEntry(
                volume=volume_label,
                source_file=source_file,
                page=page_no,
                segment=1,
                extraction_method="vision",
                content_type="error",
                text="",
                flags=["extraction_failed"] + (["native_fallback_also_failed"] if is_fallback else []),
            )
        ]

    with _print_lock:
        print(f"[{source_file}] 第{page_no}/{total}页 -> {tag}  完成,{len(segments)}个片段")

    fallback_flag = ["native_extraction_failed_used_vision_fallback"] if is_fallback else []
    return [
        LedgerEntry(
            volume=volume_label,
            source_file=source_file,
            page=page_no,
            segment=i,
            extraction_method="vision",
            content_type=seg.get("content_type", "other"),
            text=seg.get("text", ""),
            flags=seg.get("flags", []) + fallback_flag,
        )
        for i, seg in enumerate(segments, start=1)
    ]


def process_pdf(
    path: str,
    volume_label: str,
    start_page: int = 1,
    end_page: int | None = None,
    password: str | None = None,
    check_consistency: bool = True,
) -> list[LedgerEntry]:
    doc = fitz.open(path)
    if doc.is_encrypted:
        if not password or not doc.authenticate(password):
            doc.close()
            reason = "未提供密码" if not password else "密码错误"
            raise EncryptedPDFError(f"{path} 是加密PDF,{reason},无法处理")
    source_file = os.path.basename(path)
    total = doc.page_count
    end_page = end_page or total

    results_by_page: dict[int, list[LedgerEntry]] = {}
    vision_jobs = []  # (page_no, image_bytes, is_fallback)

    for page_no in range(start_page, min(end_page, total) + 1):
        page = doc[page_no - 1]
        fmt = page_format(page)

        if fmt == "native":
            text = extract_and_clean(page)
            if len(text) < NATIVE_TEXT_SUSPICIOUSLY_SHORT:
                # 原生文字读出来的东西太少,大概率是这一页的文字层本身就有问题(不是真的没内容)——
                # 不直接认命标个"低置信度"就完事,先补一次视觉识别,拿页面图片再试一次,
                # 试完了如果还是不行,那才是真的没办法,交给律师。
                print(f"[{source_file}] 第{page_no}/{total}页 -> native  内容异常短({len(text)}字符),转视觉识别补救")
                image_bytes = page.get_pixmap(dpi=150).tobytes("jpeg")
                vision_jobs.append((page_no, image_bytes, True))
                continue

            results_by_page[page_no] = [
                LedgerEntry(
                    volume=volume_label,
                    source_file=source_file,
                    page=page_no,
                    segment=1,
                    extraction_method="native",
                    content_type="native_text",
                    text=text,
                )
            ]
            print(f"[{source_file}] 第{page_no}/{total}页 -> native  完成")
            continue

        # 渲染整页而不是抠内嵌图片对象——在大文件上验证过,抠内嵌图片对象偶尔会抠错,
        # 渲染出来的画面跟人眼在阅读器里看到的一致,更可靠。
        image_bytes = page.get_pixmap(dpi=150).tobytes("jpeg")
        vision_jobs.append((page_no, image_bytes, False))

    if vision_jobs:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {
                pool.submit(_process_vision_page, source_file, volume_label, page_no, total, image_bytes, is_fallback): page_no
                for page_no, image_bytes, is_fallback in vision_jobs
            }
            for future in as_completed(futures):
                page_no = futures[future]
                results_by_page[page_no] = future.result()

    entries: list[LedgerEntry] = []
    for page_no in sorted(results_by_page):
        entries.extend(results_by_page[page_no])

    if check_consistency and entries:
        print(f"[{source_file}] 整卷一致性核对中...")
        flagged = check_volume_consistency(entries)
        if flagged:
            print(f"[{source_file}] 发现{len(flagged)}页疑似混入其他卷宗: {flagged}")
            for e in entries:
                if e.page in flagged:
                    if "possibly_misfiled" not in e.flags:
                        e.flags.append("possibly_misfiled")
                    e.flags.append(f"possibly_misfiled_reason:{flagged[e.page]}")

    return entries


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf_path")
    parser.add_argument("volume_label")
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--end", type=int, default=None)
    parser.add_argument("--out", default=None)
    parser.add_argument("--password", default=None, help="PDF 加密时需要提供")
    args = parser.parse_args()

    result = process_pdf(args.pdf_path, args.volume_label, args.start, args.end, args.password)
    out_path = args.out or f"data/ledger/{args.volume_label}.json"
    save_ledger(result, out_path)
    print(f"完成,{len(result)}条记录写入 {out_path}")
    print_usage_summary()
