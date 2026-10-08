"""工具层测试：模拟智能体将要发起的调用序列。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from finagent.agent.tools import build_tools
from finagent.config import load_config


def show(title, payload, limit=520):
    print("-" * 72)
    print(">>", title)
    text = json.dumps(payload, ensure_ascii=False, indent=1)
    print(text[:limit] + ("..." if len(text) > limit else ""))


def main() -> int:
    cfg = load_config()
    reg = build_tools(cfg)

    show("list_corpus(code=002714)",
         {k: v for k, v in reg["list_corpus"].handler("002714").items() if k != "items"})
    show("get_indicator 资产减值损失 2026Q2",
         reg["get_indicator"].handler("002714", "asset_impairment", "2026Q2"))
    show("compute_growth 归母净利润 2026Q2",
         reg["compute_growth"].handler("002714", "parent_net_profit", "2026Q2"))
    show("compare_companies",
         reg["compare_companies"].handler(["002714", "000725"]))
    res = reg["search_disclosure"].handler("存货跌价准备", "002714", None, 1)
    show("search_disclosure 存货跌价准备", res)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
