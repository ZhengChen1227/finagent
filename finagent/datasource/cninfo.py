"""巨潮资讯网数据源：权威公告原文的获取与本地化。

竞赛要求作品"在封闭或半封闭数据环境中运行"。
本模块承担环境构建职责：把上市公司公告原文一次性抓取落盘，
此后系统的检索、抽取、引用全部基于本地文件，运行期不依赖外网。

同时，公告原文是报告中一切结论的最终权威出处——
第三方数据接口只用于交叉核验，原文才是证据链的终点。
"""

from __future__ import annotations

import json
import os
import time

import requests

QUERY_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
STATIC_BASE = "http://static.cninfo.com.cn/"
STOCK_MAP_URL = "http://www.cninfo.com.cn/new/data/szse_stock.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
    "X-Requested-With": "XMLHttpRequest",
}

# 定期报告类别代码（深交所口径，沪市同构）
CATEGORIES = {
    "annual": "category_ndbg_szsh",
    "semi": "category_bndbg_szsh",
    "q1": "category_yjdbg_szsh",
    "q3": "category_sjdbg_szsh",
}

# 巨潮查询接口要求 column/plate 与上市地匹配。
# 这里曾固定写成深交所口径（column=szse, plate=sz），
# 结果沪市公司（600/601/603/605/688）查询恒为空——抓取静默拿到 0 份公告。
# 现在改为按代码推断，并在查不到时依次回退其它市场。
COLUMN_OF = {"sz": "szse", "sh": "sse", "bj": "bj"}
MARKET_ORDER = ("sz", "sh", "bj")

# 上市地识别统一收敛到 codes 模块，避免两处各写一套前缀规则而慢慢分叉。
# 曾经踩过的坑：9 开头被一律当作沪市，导致北交所新代码段 920xxx 走错交易所口径。
from finagent.datasource.codes import market_of  # noqa: E402  (置于常量之后便于阅读)


def load_stock_map(timeout: int = 30) -> dict:
    """获取 股票代码 -> orgId 映射。巨潮查询接口必须同时提供二者。"""
    resp = requests.get(STOCK_MAP_URL, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    return {item["code"]: item for item in resp.json().get("stockList", [])}


def _query_market(code: str, org_id: str, market: str, timeout: int) -> list:
    """按指定市场口径查询一次定期报告。"""
    out = []
    for kind, category in CATEGORIES.items():
        payload = {
            "pageNum": "1", "pageSize": "15", "column": COLUMN_OF[market],
            "tabName": "fulltext", "plate": market,
            "stock": f"{code},{org_id}", "searchkey": "", "secid": "",
            "category": category, "trade": "", "seDate": "",
            "sortName": "", "sortType": "", "isHLtitle": "true",
        }
        resp = requests.post(QUERY_URL, data=payload, headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
        for item in (resp.json().get("announcements") or []):
            title = item.get("announcementTitle", "")
            # 只要正文，跳过摘要与英文版
            if "摘要" in title or "英文" in title:
                continue
            out.append({
                "code": code,
                "kind": kind,
                "title": title,
                "url": STATIC_BASE + item.get("adjunctUrl", ""),
                "date": time.strftime("%Y-%m-%d",
                                      time.localtime(item.get("announcementTime", 0) / 1000)),
                "announcement_id": item.get("announcementId"),
            })
    return out


def list_periodic_reports(code: str, org_id: str, timeout: int = 30) -> list:
    """列出某公司的定期报告公告（年报/半年报/一季报/三季报）。

    先按代码推断的上市地查询；若返回空，再依次回退其它市场口径，
    避免因 column/plate 与上市地不匹配而静默得到空清单。
    """
    primary = market_of(code)
    order = [primary] + [m for m in MARKET_ORDER if m != primary]
    for market in order:
        found = _query_market(code, org_id, market, timeout)
        if found:
            return sorted(found, key=lambda x: x["date"], reverse=True)
    return []


def download(url: str, dest: str, timeout: int = 120, retries: int = 3) -> str:
    """下载文件到本地。已存在则跳过，保证可重复执行。"""
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return dest
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    last = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=timeout)
            resp.raise_for_status()
            tmp = dest + ".part"
            with open(tmp, "wb") as fh:
                fh.write(resp.content)
            os.replace(tmp, dest)
            return dest
        except Exception as exc:
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"下载失败 {url}: {last}")


def fetch_corpus(code: str, org_id: str, corpus_dir: str = "data/corpus",
                 kinds: tuple = ("annual", "semi", "q1", "q3"), limit: int = 4) -> list:
    """抓取并落盘某公司近期定期报告原文，返回本地语料清单。"""
    reports = list_periodic_reports(code, org_id)
    manifest_path = os.path.join(corpus_dir, code, "manifest.json")
    manifest = []
    if os.path.exists(manifest_path):
        with open(manifest_path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
    known = {m["url"] for m in manifest}

    counts: dict = {}
    for rep in reports:
        counts.setdefault(rep["kind"], 0)
        if counts[rep["kind"]] >= limit:
            continue
        counts[rep["kind"]] += 1
        fname = f"{rep['date']}_{rep['kind']}_{os.path.basename(rep['url'])}"
        dest = os.path.join(corpus_dir, code, fname)
        download(rep["url"], dest)
        entry = dict(rep, path=os.path.abspath(dest),
                     size=os.path.getsize(dest))
        manifest = [m for m in manifest if m["url"] != rep["url"]]
        manifest.append(entry)
        known.add(rep["url"])

    os.makedirs(os.path.join(corpus_dir, code), exist_ok=True)
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    return sorted(manifest, key=lambda x: x["date"], reverse=True)


def load_manifest(code: str, corpus_dir: str = "data/corpus") -> list:
    """读取本地语料清单。系统运行期只走这里，不再访问外网。"""
    path = os.path.join(corpus_dir, code, "manifest.json")
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)
