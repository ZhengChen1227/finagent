"""证券代码归一化：纯字符串处理，不访问任何外部服务。

存在的意义：报告里的代码可能写成 "002714"、"002714.SZ"、"SZ002714"，
也可能带全角字符或多余空白。归组、检索、证据编号都需要一个规范写法，
否则同一家公司会被当成两家，或者证据指向一个不存在的代码。

按代码段推断上市地时有两个容易踩的坑：
1. 9 开头不能一律当作沪市：900xxx 是沪市 B 股，而 920xxx 属于北交所。
2. 北交所存在两段代码：8xxxxx / 43xxxx（原精选层平移）与 920xxx（新代码段）。
"""

from __future__ import annotations

import re

KNOWN_SUFFIXES = ("SH", "SZ", "BJ", "HK")

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
    """按代码段推断上市地后缀。"""
    text = bare(code)
    if text.startswith("900"):            # 沪市 B 股
        return "SH"
    if text.startswith("92"):             # 北交所新代码段
        return "BJ"
    if text.startswith(("6", "9")):       # 沪市主板、科创板、沪市其余
        return "SH"
    if text.startswith(("0", "2", "3")):  # 深市主板、深市 B 股、创业板
        return "SZ"
    if text.startswith(("4", "8")):       # 北交所（原精选层）
        return "BJ"
    return "SZ"


def explicit_suffix(code: str) -> "str | None":
    """读取用户显式写出的后缀，如 835185.BJ -> BJ。"""
    text = str(code or "").strip().upper()
    if "." not in text:
        return None
    tail = text.rsplit(".", 1)[1].strip()
    return tail if tail in KNOWN_SUFFIXES else None


def normalize_code(code: str) -> str:
    """归一化为规范写法，如 002714 -> 002714.SZ，835185 -> 835185.BJ。"""
    text = bare(code)
    if not text.isdigit():
        return str(code).strip().upper()
    return f"{text}.{explicit_suffix(code) or suffix_of(text)}"
