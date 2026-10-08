"""全链路可追溯日志。

竞赛要求"完整记录文件访问、工具调用、计算过程和结果生成"。
本模块把每一步动作以 JSONL 追加落盘，使运行过程可核验、可复现、可回放。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any


class Trace:
    """运行轨迹记录器。每行一条 JSON 事件，seq 单调递增，可直接重放。"""

    def __init__(self, out_dir: str = "output/traces", run_id: str | None = None) -> None:
        self.run_id = run_id or datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
        os.makedirs(out_dir, exist_ok=True)
        self.path = os.path.join(out_dir, f"{self.run_id}.jsonl")
        self._seq = 0
        self._buffer: list = []
        self._fh = open(self.path, "a", encoding="utf-8")
        self.record("run_start", version=_version())

    def record(self, event, **payload):
        self._seq += 1
        item = {
            "seq": self._seq,
            "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds"),
            "run_id": self.run_id,
            "event": event,
            **payload,
        }
        self._buffer.append(item)
        self._fh.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
        self._fh.flush()
        return item

    def file_access(self, path, mode="read", note=""):
        return self.record("file_access", path=os.path.abspath(path), mode=mode, note=note)

    def tool_call(self, tool, inputs=None, outputs=None, ok=True, duration_ms=None):
        return self.record("tool_call", tool=tool, inputs=inputs,
                           outputs=outputs, ok=ok, duration_ms=duration_ms)

    def compute(self, name, formula, inputs=None, output=None):
        return self.record("compute", name=name, formula=formula, inputs=inputs, output=output)

    def finding(self, kind, **kw):
        return self.record("finding", kind=kind, **kw)

    def result(self, **kw):
        return self.record("result", **kw)

    @property
    def events(self):
        return list(self._buffer)

    def step(self, name, **kw):
        return _Step(self, name, **kw)

    def close(self):
        self.record("run_end", total_events=self._seq)
        self._fh.close()
        return self.path


class _Step:
    """计时上下文：with trace.step("parse"): ..."""

    def __init__(self, trace, name, **kw):
        self.trace, self.name, self.kw = trace, name, kw

    def __enter__(self):
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb):
        ms = (time.perf_counter() - self.t0) * 1000
        self.trace.record("step", name=self.name, duration_ms=round(ms, 2),
                          ok=exc_type is None, **self.kw)
        return False


def _version():
    from finagent import __version__
    return __version__
