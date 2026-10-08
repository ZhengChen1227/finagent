"""技能库：可复用的分析方法论。

竞赛要求提交"Skill"核心模块。技能与工具的分工不同：

    工具（Tool）回答"能做什么"——一个原子能力，如取数、检索、校验
    技能（Skill）回答"该怎么做"——一套沉淀下来的分析程序与判读标准

技能以 Markdown + YAML front-matter 外置存放，带来三个好处：
可利用版本控制追溯方法论演进、可人工直接阅读审阅、
可由智能体按任务关键词自动匹配加载。

技能内容本身就是 Prompt 的一部分——它把资深分析师的经验
（尤其是"常见误判"）固化成可复用的检查清单。
"""

from __future__ import annotations

import hashlib
import os
import re

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
_cache: dict = {}


def _parse(text: str) -> tuple:
    """拆分 front-matter 与正文。"""
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.S)
    if not match:
        return {}, text
    meta = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            meta[key.strip()] = [v.strip() for v in value[1:-1].split(",") if v.strip()]
        else:
            meta[key.strip()] = value
    return meta, match.group(2)


def _load_file(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    meta, body = _parse(text)
    return {"meta": meta, "body": body, "raw": text,
            "digest": hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]}


def _all() -> dict:
    if _cache:
        return _cache
    for fname in sorted(os.listdir(SKILL_DIR)):
        if not fname.endswith(".md"):
            continue
        info = _load_file(os.path.join(SKILL_DIR, fname))
        name = info["meta"].get("name") or fname[:-3]
        _cache[name] = info
    return _cache


def catalog() -> list:
    """技能清单。"""
    out = []
    for name, info in _all().items():
        meta = info["meta"]
        out.append({
            "name": name,
            "title": meta.get("title", name),
            "priority": meta.get("priority", "中"),
            "triggers": meta.get("triggers", []),
            "tools": meta.get("tools", []),
            "digest": info["digest"],
        })
    return sorted(out, key=lambda x: ({"高": 0, "中": 1, "低": 2}.get(x["priority"], 3),
                                      x["name"]))


def load_skill(name: str) -> str:
    """加载技能全文。"""
    info = _all().get(name)
    if info is None:
        raise KeyError(f"不存在技能: {name}（可用: {sorted(_all().keys())}）")
    return info["body"]


def match(text: str, limit: int = 3) -> list:
    """按关键词匹配相关技能，供智能体按任务自动加载。"""
    if not text:
        return []
    scored = []
    for item in catalog():
        hits = [t for t in item["triggers"] if t and t in text]
        if hits:
            scored.append((len(hits), item))
    scored.sort(key=lambda kv: (-kv[0], {"高": 0, "中": 1, "低": 2}.get(kv[1]["priority"], 3)))
    return [item for _, item in scored[:limit]]


def compose(text: str, limit: int = 3) -> str:
    """把匹配到的技能拼装为可注入模型上下文的文本块。"""
    matched = match(text, limit)
    if not matched:
        return ""
    parts = ["## 适用技能（按任务自动匹配加载）", ""]
    for item in matched:
        parts.append(f"### 技能：{item['title']}（`{item['name']}`）")
        parts.append("")
        parts.append(load_skill(item["name"]).strip())
        parts.append("")
    return "\n".join(parts)


def manifest() -> dict:
    """技能版本清单（内容哈希），写入轨迹用于验证。"""
    return {name: info["digest"] for name, info in sorted(_all().items())}
