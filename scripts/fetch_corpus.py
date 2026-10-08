"""一次性抓取公告原文，构建封闭数据环境。

运行后，系统的检索与引用全部基于 data/corpus/ 下的本地文件，不再访问外网。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from finagent.datasource.cninfo import fetch_corpus, load_stock_map

TARGETS = ["002714", "000725"]


def main() -> int:
    stock_map = load_stock_map()
    for code in TARGETS:
        info = stock_map.get(code)
        if not info:
            print(f"未找到 {code} 的 orgId，跳过")
            continue
        print(f"[抓取] {code} {info.get('zwjc', '')} orgId={info['orgId']} ...")
        manifest = fetch_corpus(code, info["orgId"], limit=3)
        print(f"  本地语料 {len(manifest)} 份：")
        for entry in manifest:
            print(f"    {entry['date']}  {entry['kind']:<7} "
                  f"{entry['size'] / 1e6:6.2f}MB  {entry['title']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
