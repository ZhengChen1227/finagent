"""配置加载：内置默认值 <- config.yaml <- 环境变量。"""

from __future__ import annotations

import copy
import os
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "data": {
        "source": "eastmoney",
        "cache_dir": "data/raw",
        "corpus_dir": "data/corpus",
        "index_dir": "data/corpus_index",
        "refresh": False,
    },
    "output": {
        "report_dir": "output/reports",
        "table_dir": "output/tables",
        "trace_dir": "output/traces",
    },
    "llm": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-chat",
        "temperature": 0.2,
        "timeout": 120,
    },
    "agent": {
        "max_steps": 12,
    },
    "companies": [
        {"code": "002714.SZ", "name": "牧原股份", "industry": "生猪养殖"},
        {"code": "000725.SZ", "name": "京东方A", "industry": "显示面板"},
    ],
    "thresholds": {
        "impairment_yoy_pct": 300.0,
        "impairment_min_amount": 100000000.0,
        "nonrecurring_ratio_pct": 20.0,
        "adjusted_cash_conv_low": 0.5,
        "adjusted_cash_conv_high": 1.8,
        "qoq_swing_pct": 50.0,
        "tax_rate_high_pct": 25.0,
        "collect_ratio_low": 0.9,
        "articulation_tol_pct": 0.5,
        "depr_to_revenue_high_pct": 15.0,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


LOCAL_CONFIG_NAME = "config.local.yaml"


def local_config_path(path: str = "config.yaml") -> str:
    """本机私有配置的路径。

    团队协作时 config.yaml 是共享的，而密钥必须私有。
    因此把密钥等敏感项放在同目录的 config.local.yaml，
    该文件已在 .gitignore 中排除，不会进入版本库。
    """
    return os.path.join(os.path.dirname(os.path.abspath(path)), LOCAL_CONFIG_NAME)


def load_config(path: str = "config.yaml") -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            _deep_merge(cfg, yaml.safe_load(fh) or {})
    local = local_config_path(path)
    if os.path.exists(local):
        try:
            with open(local, "r", encoding="utf-8") as fh:
                _deep_merge(cfg, yaml.safe_load(fh) or {})
        except Exception:
            pass          # 私有配置损坏不应导致整个系统起不来
    cfg["llm"]["base_url"] = os.environ.get("FINAGENT_BASE_URL", cfg["llm"]["base_url"])
    cfg["llm"]["api_key"] = os.environ.get("FINAGENT_API_KEY", cfg["llm"]["api_key"])
    cfg["llm"]["model"] = os.environ.get("FINAGENT_MODEL", cfg["llm"]["model"])
    return cfg


def company_of(cfg: dict, code: str) -> dict:
    """在配置中查找公司。匹配忽略市场后缀，兼容 002714 与 002714.SZ 两种写法。

    配置中没有的公司返回占位条目，由调用方从数据源补齐名称与行业。
    """
    target = str(code).strip().upper()
    bare_target = target.split(".")[0]
    for item in cfg.get("companies", []):
        if item["code"].upper() == target or item["code"].split(".")[0] == bare_target:
            return dict(item)
    return {"code": code, "name": code, "industry": "未分类"}
