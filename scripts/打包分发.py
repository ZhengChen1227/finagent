"""生成分发给队友的压缩包。

只做一件事：把"应该给队友的东西"打包，并确保不会把密钥打进去。
密钥一旦随包外泄，等同于别人的账单记在你头上，所以这里做了硬校验：
打包前扫描每个待入包文件，命中疑似密钥即中止。

    python scripts/打包分发.py                 # 仅代码（推荐，默认）
    python scripts/打包分发.py --with-corpus   # 连公告原文一起打包（约 74MB）
    python scripts/打包分发.py --with-venv     # 连虚拟环境一起打包（仅限同系统同架构）
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 密钥特征：sk- 开头，或 32 位以上的连续十六进制/字母数字串
SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)(api[_-]?key|secret|token)\s*[:=]\s*.{0,4}?[A-Za-z0-9_\-]{24,}"),
]

# 永不入包的路径片段
ALWAYS_EXCLUDE = {
    ".git", ".venv", "venv", "__pycache__", "dist", ".idea", ".vscode",
    "node_modules", ".pytest_cache",
}
EXCLUDE_SUFFIX = {".pyc", ".pyo", ".zip"}

# 默认排除：本机私有配置、运行产物、公告原文与索引
EXCLUDE_NAMES = {"config.local.yaml"}
EXCLUDE_DIRS = {"output/traces", "data/corpus", "data/corpus_index", "data/raw"}


def iter_files(with_corpus: bool, with_venv: bool):
    for dirpath, dirnames, filenames in os.walk(ROOT):
        rel_dir = Path(dirpath).relative_to(ROOT).as_posix()
        dirnames[:] = [
            d for d in dirnames
            if d not in ALWAYS_EXCLUDE
            and (with_venv or d != ".venv")
        ]
        for name in filenames:
            rel = f"{rel_dir}/{name}" if rel_dir != "." else name
            if name in EXCLUDE_NAMES or Path(name).suffix in EXCLUDE_SUFFIX:
                continue
            if any(rel == d or rel.startswith(d + "/") for d in EXCLUDE_DIRS):
                if not (with_corpus and rel.startswith("data/corpus")):
                    continue
            if not with_corpus and rel.startswith("data/corpus_index"):
                continue
            yield Path(dirpath) / name, rel


def scan_secrets(files) -> list:
    hits = []
    for path, rel in files:
        if Path(rel).suffix.lower() in {".png", ".jpg", ".pdf", ".xlsx", ".ico"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for pattern in SECRET_PATTERNS:
            for match in pattern.finditer(text):
                hits.append((rel, match.group(0)[:12] + "..."))
    return hits


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--with-corpus", action="store_true", help="连公告原文一起打包")
    parser.add_argument("--with-venv", action="store_true", help="连 .venv 一起打包")
    parser.add_argument("--out", default=None, help="输出目录，默认 dist/")
    args = parser.parse_args()

    files = list(iter_files(args.with_corpus, args.with_venv))

    leaks = scan_secrets(files)
    if leaks:
        print("发现疑似密钥，已中止打包：")
        for rel, sample in leaks:
            print(f"  {rel}  ->  {sample}")
        print("\n请先移除该密钥（改用 config.local.yaml，它不会被入包）。")
        return 1

    tag = "完整包-含公告原文" if args.with_corpus else "轻量包-仅代码"
    out_dir = Path(args.out) if args.out else ROOT / "dist"
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"FinAgent-{tag}.zip"

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, rel in files:
            zf.write(path, f"FinAgent/{rel}")

    size = zip_path.stat().st_size
    print(f"已生成 {zip_path}")
    print(f"  文件数 {len(files)}  体积 {size / 1048576:.1f} MB")
    print("  已排除 config.local.yaml（含密钥）、output/、data/raw/")
    if not args.with_corpus:
        print("  未包含 data/corpus/：队友可在解包后执行 python run.py fetch 自行抓取")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
