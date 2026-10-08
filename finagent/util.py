"""通用工具。"""

from __future__ import annotations

import numpy as np


def json_default(obj):
    """把 numpy 标量与数组转换为原生 Python 类型，供 json 序列化使用。"""
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return None if np.isnan(obj) else float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    return str(obj)


def clean_nan(value):
    """NaN 转 None，避免写出非标准 JSON。"""
    if isinstance(value, float) and np.isnan(value):
        return None
    return value
