"""全市场证券名录：代码、简称、上市地与巨潮 orgId 的权威索引。

作用有二：
1. 让"任意上市公司都能分析"成为可操作的事实——用户输入简称（如"茅台"）
   或代码（如"600519"）都能定位到唯一主体，不必事先知道交易所后缀。
2. 提供封闭数据环境的构建清单：名录里的每一只证券都能用 run.py fetch
   抓取公告原文，因此"能不能分析"不依赖于我们预先挑了哪几家公司。

名录来源为巨潮资讯网公开代码表（含沪深北及 B 股共六千余只），
一次性下载后落盘，运行期只读本地文件，不依赖外网。
"""

from __future__ import annotations

import json
import os

import requests

from finagent.datasource.codes import bare, market_of, normalize_code

STOCK_MAP_URL = "http://www.cninfo.com.cn/new/data/szse_stock.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def _cache_path(cache_dir: str) -> str:
    return os.path.join(cache_dir, "_universe.json")


def load_universe(cache_dir: str = "data/raw", refresh: bool = False,
                  timeout: int = 30) -> list:
    """读取全市场名录；本地无缓存时下载并落盘。"""
    path = _cache_path(cache_dir)
    if os.path.exists(path) and not refresh:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if data:
                return data
        except Exception:
            pass

    resp = requests.get(STOCK_MAP_URL, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    items = []
    for item in resp.json().get("stockList", []):
        code = str(item.get("code", "")).strip().zfill(6)
        if not code.isdigit():
            continue
        items.append({
            "code": code,
            "name": (item.get("zwjc") or "").strip(),
            "pinyin": (item.get("pinyin") or "").strip().lower(),
            "org_id": item.get("orgId"),
            "market": market_of(code),
            "secucode": normalize_code(code),
        })
    os.makedirs(cache_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(items, fh, ensure_ascii=False, indent=1)
    return items


def by_code(code: str, cache_dir: str = "data/raw") -> dict | None:
    target = bare(code)
    for item in load_universe(cache_dir):
        if item["code"] == target:
            return item
    return None


def search(keyword: str, limit: int = 20, cache_dir: str = "data/raw") -> list:
    """按代码或简称检索主体。

    排序刻意做成"精确优先"：完全同名的排最前，其次是代码前缀命中，
    最后才是简称包含。这样输入"600519"或"茅台"都能一次命中目标。
    """
    text = (keyword or "").strip()
    if not text:
        return []
    upper, lower = text.upper(), text.lower()
    exact, prefix, contains = [], [], []
    for item in load_universe(cache_dir):
        if item["code"] == bare(text) or item["name"] == text or item["secucode"] == upper:
            exact.append(item)
        elif item["code"].startswith(text) or item["name"].startswith(text):
            prefix.append(item)
        elif text in item["name"] or item["pinyin"].startswith(lower):
            contains.append(item)
    return (exact + prefix + contains)[:limit]


def resolve(text: str, cache_dir: str = "data/raw") -> dict | None:
    """把用户输入（代码或简称）解析为唯一主体；歧义或未命中时返回 None。"""
    hits = search(text, limit=2, cache_dir=cache_dir)
    return hits[0] if hits else None
