"""证券代码归一化与上市地识别。

覆盖范围：沪市主板、科创板、沪市B股、深市主板、创业板、深市B股、北交所。
本模块是"任意上市公司都能分析"的第一道关卡——只要代码能被正确归一化，
后续取数就有确定的落点；归一化错了，系统会静默取到空数据而不报错。

两个容易踩的坑：
1. 北交所存在两段代码：8xxxxx / 43xxxx（原精选层平移）与 920xxx（新代码段）。
   东方财富对二者的 SECUCODE 后缀并不统一，8xx/43x 实际使用 .NQ。
   因此这里返回"候选后缀"列表，由取数层逐个试探，命中即缓存。
2. 9 开头不能一律当作沪市：900xxx 是沪市B股，而 920xxx 属于北交所。
"""

from __future__ import annotations

import re

# 显式后缀 -> 规范后缀
KNOWN_SUFFIXES = ("SH", "SZ", "BJ", "NQ", "HK")

_CODE_RE = re.compile(r"^\d{5,6}$")


def is_bare_code(text: str) -> bool:
    """判断是否是不带后缀的纯数字代码。"""
    return bool(_CODE_RE.match((text or "").strip()))


def bare(code: str) -> str:
    """去掉后缀与空白，返回纯数字代码（不足 6 位左侧补零）。"""
    text = str(code or "").strip().upper()
    if "." in text:
        text = text.split(".")[0]
    text = text.strip()
    return text.zfill(6) if text.isdigit() else text


def suffix_of(code: str) -> str:
    """按代码段推断规范上市地后缀。"""
    text = bare(code)
    if text.startswith("900"):       # 沪市B股
        return "SH"
    if text.startswith("92"):        # 北交所新代码段
        return "BJ"
    if text.startswith(("6", "9")):  # 沪市主板、科创板、沪市其余
        return "SH"
    if text.startswith(("0", "2", "3")):  # 深市主板、深市B股、创业板
        return "SZ"
    if text.startswith(("4", "8")):  # 北交所（原精选层）
        return "BJ"
    return "SZ"


def explicit_suffix(code: str) -> str | None:
    """读取用户显式写出的后缀，如 835185.BJ -> BJ。"""
    text = str(code or "").strip().upper()
    if "." not in text:
        return None
    tail = text.rsplit(".", 1)[1].strip()
    return tail if tail in KNOWN_SUFFIXES else None


def secucode_candidates(code: str) -> tuple:
    """返回该代码在数据源上可能的 SECUCODE 写法，按尝试顺序排列。

    用户显式写出的后缀优先；其次按代码段推断的规范后缀；
    北交所再补一个 .NQ 备选，因为早期平移股票在东财使用该后缀。
    """
    text = bare(code)
    if not text.isdigit():
        return (str(code).strip().upper(),)

    explicit = explicit_suffix(code)
    primary = explicit or suffix_of(text)
    order = [primary]
    if primary in ("BJ", "NQ"):
        for extra in ("BJ", "NQ"):
            if extra not in order:
                order.append(extra)
    return tuple(f"{text}.{s}" for s in order)


def normalize_code(code: str) -> str:
    """归一化为规范写法，如 002714 -> 002714.SZ，835185 -> 835185.BJ。"""
    text = bare(code)
    if not text.isdigit():
        return str(code).strip().upper()
    return f"{text}.{explicit_suffix(code) or suffix_of(text)}"


def market_of(code: str) -> str:
    """返回 sz / sh / bj，用于交易所口径查询（如巨潮公告接口）。"""
    return {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(suffix_of(code), "sz")
