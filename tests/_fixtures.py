"""测试夹具：把本机语料变成可复用的上传集合。

设计取舍：所有需要真实 PDF 的测试都从这里取材料，并共用同一个上传目录
（`data/uploads/_tests`）。`UploadSet` 按文件内容的 SHA256 缓存抽取结果，
因此第二次之后的测试不必重复解析上百页 PDF，整套测试能在几十秒内跑完。

本机没有 `data/corpus/` 时（例如评审刚克隆仓库），依赖真实材料的测试会
**跳过并明确提示原因**，而不是伪装通过——伪装通过会让"复现"变成一句空话。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CORPUS = ROOT / "data" / "corpus"
TEST_RUN = "_tests"

# 验收基准：牧原 2026 半年报的三个关键数字（与巨潮原文逐字核对过）。
MUYUAN_2026H1 = {
    "revenue": 59410306384.59,
    "parent_net_profit": -6077653278.94,
    "deduct_parent_net_profit": -5895303071.49,
    "netcash_operate": -2223668382.68,
}


def load_cfg():
    """本机配置（含 output 路径）。测试只读它，不改任何生产配置。"""
    from finagent.config import load_config

    return load_config(str(ROOT / "config.yaml"))


def corpus_dir(code: str = "002714"):
    """某公司的语料目录。不存在时返回 None。"""
    path = CORPUS / code
    return path if path.is_dir() else None


def pick(code: str = "002714", kinds=("semi", "annual")) -> list:
    """按报告类型取最新的若干份 PDF，返回绝对路径列表（新的在前）。"""
    folder = corpus_dir(code)
    if folder is None:
        return []
    out = []
    for kind in kinds:
        hits = sorted([p for p in folder.glob(f"*_{kind}_*.PDF")] +
                      [p for p in folder.glob(f"*_{kind}_*.pdf")])
        if hits:
            out.append(str(hits[-1]))
    return out


def missing_message(code: str = "002714") -> str:
    return (f"跳过：本机没有 data/corpus/{code}/ 下的财报原文。"
            f"该测试需要真实 PDF 才能验证抽取准确性，"
            f"请按 docs/复现清单.md 下载对应公告（含下载链接与 SHA256）后重跑。")


def uploads(code: str = "002714", kinds=("semi", "annual")):
    """构建（或复用）一个上传集合。返回 (UploadSet, key) 或 (None, None)。"""
    from finagent.config import uploads_dir
    from finagent.ingest.dataset import UploadSet

    files = pick(code, kinds)
    if not files:
        return None, None
    cfg = load_cfg()
    us = UploadSet(run_id=TEST_RUN, root=uploads_dir(cfg))
    for path in files:
        us.add(path)
    groups = us.groups()
    # 同一个测试上传目录会攒下多家公司的材料，必须按代码挑出本次要分析的主体，
    # 否则"分析 000725"会静默地分析成 002714——数字看着都对，主体却是错的。
    keys = [k for k in groups if str(k) == code]
    if not keys:
        keys = [k for k in groups
                if code in {(m.get("subject") or {}).get("code") for m in groups[k]}]
    if not keys:
        keys = list(groups)
    if not keys:
        return None, None
    return us, keys[0]


def merged(code: str = "002714", kinds=("semi", "annual")):
    """直接取合并后的 frames / 指标宽表。"""
    us, key = uploads(code, kinds)
    if us is None:
        return None
    return us, us.merged(key)


def report(title: str, ok: bool, detail: str = "") -> bool:
    """统一的结果打印：通过/失败一眼可辨。"""
    mark = "通过" if ok else "失败"
    print(f"  [{mark}] {title}" + (f"  {detail}" if detail else ""))
    return ok
