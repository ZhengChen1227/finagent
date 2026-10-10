"""PDF 文本抽取：与报告原文页码一一对应。

存在的意义：本系统的每一条结论都要能翻回财报具体页面。
因此抽取时必须保留页边界，否则"第几页写的是什么"就再也无法核实，
证据链会在第一步就断掉。

抽取模式选 layout 而不是默认的 plain：中国上市公司财报的三大表是
严格分列的宽表格，plain 模式会把列打散、把相邻列的数字挤到一行里；
layout 模式保留空格填充，数字按列右对齐，因此才能按列位置还原
"哪个数字属于本期、哪个属于上期"——这是同比计算是否正确的前提。
"""

from __future__ import annotations

import os

from pypdf import PdfReader

PAGE_MARK = "\n\n<<<PAGE:{n}>>>\n\n"


def extract_pages(pdf_path: str, trace=None) -> list:
    """逐页抽取 layout 文本，返回按页排列的字符串列表（索引 0 = 第 1 页）。"""
    reader = PdfReader(pdf_path)
    pages = []
    for page in reader.pages:
        try:
            text = page.extract_text(extraction_mode="layout") or ""
        except Exception:
            # 个别页面的内容流损坏是常态，不应让整份报告抽取失败。
            # 失败页留空，页码仍然占位，后续"抽不到"会被如实报告。
            text = ""
        pages.append(text)
    if trace:
        trace.file_access(pdf_path, "read", f"抽取正文 {len(pages)} 页")
    return pages


def join_pages(pages: list) -> str:
    """按索引文件约定拼接：页码标记 + 正文。"""
    return "".join(PAGE_MARK.format(n=i) + p for i, p in enumerate(pages, start=1))


def page_of(offset: int, marked_text: str) -> int:
    """给定拼接文本中的偏移，返回它所在的页码。用于把抽取结果锚回原文。"""
    head = marked_text[:offset]
    return head.count("<<<PAGE:")


def write_text(pages: list, txt_path: str, trace=None) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(txt_path)), exist_ok=True)
    with open(txt_path, "w", encoding="utf-8") as fh:
        fh.write(join_pages(pages))
    if trace:
        trace.file_access(txt_path, "write", f"落盘正文 {len(pages)} 页")
    return txt_path
