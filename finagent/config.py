"""配置加载：内置默认值 <- config.yaml <- config.local.yaml <- 环境变量。

本项目的输入只有一种：用户上传的财报 PDF。因此配置里不再有
"数据源""缓存目录""预设公司清单"这类联网取数时代的字段——
留着它们会让人以为系统还能联网取数。
"""

from __future__ import annotations

import copy
import os
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "data": {
        # 每一批上传落在 uploads_dir/<run_id>/ 下，PDF 原件与抽取结果同目录存放。
        # 该目录整体被 .gitignore 排除：用户上传的财报不进入版本库。
        "uploads_dir": "data/uploads",
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

# 唯一允许出网访问的域名。写在这里是为了让"运行期只连 DeepSeek"这件事
# 有一个可被测试断言的单一来源，而不是散落在各处的口头约定。
ALLOWED_HOST = "api.deepseek.com"


def local_config_path(path: str = "config.yaml") -> str:
    """本机私有配置的路径。

    config.yaml 是共享的，而密钥必须私有。
    因此把密钥放在同目录的 config.local.yaml，该文件已在 .gitignore 中排除。
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


def uploads_dir(cfg: dict) -> str:
    """上传根目录的绝对路径。"""
    return os.path.abspath(cfg["data"]["uploads_dir"])
