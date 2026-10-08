"""环境自检：核对依赖版本，便于评审在指定环境中确认可复现性。"""

from __future__ import annotations

import importlib
import importlib.metadata as md
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

EXPECTED = {
    "pandas": "2.3.3", "numpy": "2.4.1", "requests": "2.32.5",
    "PyYAML": "6.0.3", "httpx": "0.28.1", "pypdf": "6.11.0", "mcp": "1.27.1",
}
REQUIRED = ["pandas", "numpy", "requests", "yaml", "httpx", "pypdf", "mcp"]


def main() -> int:
    print(f"Python {sys.version.split()[0]}")
    print(f"可执行文件 {sys.executable}")
    print()
    ok = True
    for name, want in EXPECTED.items():
        try:
            got = md.version(name)
        except md.PackageNotFoundError:
            print(f"  缺失  {name}")
            ok = False
            continue
        mark = "一致" if got == want else "差异"
        if got != want:
            ok = False
        print(f"  {mark}  {name:<10} 期望 {want:<10} 实际 {got}")
    print()
    for mod in REQUIRED:
        try:
            importlib.import_module(mod)
        except Exception as exc:
            print(f"  导入失败 {mod}: {exc!r}")
            ok = False
    print("环境自检：", "通过" if ok else "存在差异（仍可能正常运行，请核对）")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
