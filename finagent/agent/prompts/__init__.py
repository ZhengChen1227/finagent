"""Prompt 管理：提示词外置为版本化文件。

竞赛要求提交"Prompt"核心模块。把提示词写死在代码字符串里有两个问题：
无法独立审阅与版本追溯，也无法证明某次输出究竟由哪个版本的提示词产生。

本模块从 .md 文件加载提示词，并计算内容哈希写入执行轨迹——
于是"这份结论对应哪一版提示词"成为可验证的事实，而不是口头承诺。
"""

from __future__ import annotations

import hashlib
import os

PROMPT_DIR = os.path.dirname(os.path.abspath(__file__))
_cache: dict = {}


def load(name: str) -> str:
    """按名称加载提示词文件（不含扩展名）。"""
    if name in _cache:
        return _cache[name]
    path = os.path.join(PROMPT_DIR, f"{name}.md")
    if not os.path.exists(path):
        raise FileNotFoundError(f"提示词不存在: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    _cache[name] = text
    return text


def digest(name: str, length: int = 12) -> str:
    """提示词内容哈希。写入轨迹后可用于验证输出对应的提示词版本。"""
    return hashlib.sha256(load(name).encode("utf-8")).hexdigest()[:length]


def manifest() -> dict:
    """全部提示词的版本清单。"""
    out = {}
    for fname in sorted(os.listdir(PROMPT_DIR)):
        if fname.endswith(".md"):
            stem = fname[:-3]
            out[stem] = digest(stem)
    return out
