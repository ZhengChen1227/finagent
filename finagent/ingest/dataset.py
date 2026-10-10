"""上传集合：把用户拖进来的财报 PDF 变成可复现、可追溯的分析输入。

存在的意义：原来的系统靠联网取数，输入是"股票代码"；现在输入是"用户上传的
文件"，于是必须有一层来回答三个问题：

1. **这批材料放在哪里、是不是同一批？** 每批上传（一次分析请求）生成一个
   run_id 目录，PDF 原样留存，正文另存为带页码锚点的 txt。
   PDF 原文必须留着——报告里的每一个数字都要能翻回它的第几页。
2. **同一份文件重复分析会不会得到不同结果？** 抽取结果按文件内容的 SHA256
   落盘缓存，内容没变就直接复用，因此"同一输入重复运行结果一致"是可验证的，
   而不是一句口号。缓存的键是内容哈希而非文件名，改名也会命中同一份缓存。
3. **多份材料怎么拼成一条时间轴？** 一份财报自带比较列（上年同期／上年年末），
   所以单份材料就能算同比；把同一主体的多份材料叠加，才能算环比。
   合并时**本期口径优先**：一季报的"上年同期"里那一行 3-31 数据，
   与上年一季报的"本期"是同一个数，但后者才是一手来源，前者只是比较列。

`manifest.json` 沿用原来的语料清单格式（code/date/kind/title/path/size），
因此 `corpus/index.py` 可以直接对这个目录建全文索引，检索功能一行都不用改。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time

import pandas as pd

from finagent.datasource import schema
from finagent.ingest import assemble, docmeta, highlights, pdf_text, statements, verify

REPORTS = ("income", "cashflow", "balance")
DOC_EXT = ".pdf"


def _code_fingerprint() -> str:
    """抽取层源码指纹：抽取代码一改，缓存自动失效。

    缓存按"文件内容哈希"命中，这对"同一份财报重复分析结果一致"是必需的，
    但只按内容哈希还不够：升级抽取代码后，同一份财报会直接复用**旧代码**
    的结果——界面上"可复现"于是变成了"复现了上一次的错误"。
    把源码指纹一并算进缓存键，才能既快又不会读到过期结论。
    """
    base = os.path.dirname(os.path.abspath(__file__))
    parts = []
    for name in sorted(os.listdir(base)):
        if name.endswith(".py"):
            with open(os.path.join(base, name), "rb") as fh:
                parts.append(hashlib.sha256(fh.read()).hexdigest())
    peer = os.path.join(os.path.dirname(base), "datasource", "schema.py")
    if os.path.isfile(peer):
        with open(peer, "rb") as fh:
            parts.append(hashlib.sha256(fh.read()).hexdigest())
    return hashlib.sha256("".join(parts).encode("utf-8")).hexdigest()[:12]


# 当前抽取层的指纹。已在缓存条目里的指纹与它不一致时，缓存作废、重新抽取。
CODE_VERSION = _code_fingerprint()


def new_run_id() -> str:
    """运行编号：时间戳 + 毫秒，便于人工排序与定位。"""
    return time.strftime("%Y%m%d-%H%M%S") + "-%03d" % (int(time.time() * 1000) % 1000)


def sha256_of(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


class UploadError(RuntimeError):
    """上传的文件无法作为财报处理。必须显式抛出，不能退化成"没有数据"。"""


class UploadSet:
    """一次分析所用的全部上传材料。"""

    def __init__(self, run_id: str | None = None, root: str = "data/uploads",
                 trace=None) -> None:
        self.run_id = run_id or new_run_id()
        self.root = os.path.abspath(root)
        self.dir = os.path.join(self.root, self.run_id)
        os.makedirs(self.dir, exist_ok=True)
        self.trace = trace
        self._entries: list = []
        self._load_existing()

    # ---------------------------------------------------------------- 读取

    def _load_existing(self) -> None:
        """复用同一 run_id 目录里已经分析过的结果（进程重启后仍可续跑）。"""
        for name in sorted(os.listdir(self.dir)):
            if not name.endswith(".analysis.json"):
                continue
            path = os.path.join(self.dir, name)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    entry = json.load(fh)
            except Exception:
                continue
            stored = entry.get("stored_path", "")
            if not (entry.get("sha256") and os.path.exists(stored)):
                continue
            if entry.get("version") != CODE_VERSION:
                # 抽取层代码已经变过：旧结果一律作废重算，而不是沿用它。
                try:
                    entry = self._analyze(stored, entry.get("filename") or name,
                                          entry["sha256"])
                except Exception:
                    continue
            self._entries.append(entry)

    # ---------------------------------------------------------------- 写入

    def add(self, src_path: str, filename: str | None = None) -> dict:
        """收下一份财报，完成"落盘 → 抽取 → 装配 → 校验"，返回条目。"""
        if not os.path.exists(src_path):
            raise UploadError(f"文件不存在：{src_path}")
        name = _safe_name(filename or os.path.basename(src_path))
        digest = sha256_of(src_path)
        cached = self._find_cached(digest)
        if cached and cached.get("version") == CODE_VERSION:
            if self.trace:
                self.trace.record("upload_cache_hit", file=name, sha256=digest[:12])
            return cached

        stored = os.path.join(self.dir, name)
        if os.path.abspath(src_path) != os.path.abspath(stored):
            shutil.copy2(src_path, stored)
        if self.trace:
            self.trace.file_access(stored, "write", f"上传财报 {name}")

        entry = self._analyze(stored, name, digest)
        self._entries.append(entry)
        self._write_manifest()
        return entry

    def _find_cached(self, digest: str):
        for entry in self._entries:
            if entry.get("sha256") == digest:
                return entry
        return None

    def _analyze(self, stored: str, name: str, digest: str) -> dict:
        pages = pdf_text.extract_pages(stored, trace=self.trace)
        if not any(p.strip() for p in pages):
            raise UploadError(f"{name}：未能抽取到任何文本。"
                              f"它可能是扫描件（图片型 PDF），本系统不内置 OCR，"
                              f"请改用可复制文字的财报版本。")
        txt_path = os.path.splitext(stored)[0] + ".txt"
        pdf_text.write_text(pages, txt_path, trace=self.trace)

        doc = docmeta.parse(pages, name)
        st = statements.extract(pages, trace=self.trace)
        st["missing"] = statements.missing(pages, st)
        hl = highlights.extract(pages, trace=self.trace)
        built = assemble.build(doc, st, hl, stored)
        verification = verify.run(built)
        built["verification"] = verification
        built["table"] = None

        from finagent.metrics import indicators as I
        table, flags = I.compute_all(built["frames"], self.trace)
        built["table"] = table
        built["flags"] = flags
        if self.trace:
            self.trace.record("upload_analyzed", file=name,
                              subject=built["subject"].get("display"),
                              report_date=built["subject"].get("report_date"),
                              fields={k: len(v) for k, v in built["contributions"].items()},
                              checks_passed=verification["passed"],
                              checks_failed=verification["failed"],
                              suspect=sorted(verification["suspect"]))

        entry = {
            # 抽取层指纹随条目落盘：它既是缓存有效性的判据，
            # 也是报告里"这份结论由哪一版抽取代码产出"的凭据。
            "version": CODE_VERSION,
            "upload_id": hashlib.sha1(digest.encode("utf-8")).hexdigest()[:12],
            "filename": name, "stored_path": os.path.abspath(stored),
            "txt_path": os.path.abspath(txt_path), "size": os.path.getsize(stored),
            "sha256": digest, "pages": len(pages),
            "doc": {k: v for k, v in doc.items()},
            "subject": built["subject"],
            "counts": {k: len(v) for k, v in built["contributions"].items()},
            "missing": built["missing"],
            "warnings": list(doc.get("warnings") or []),
            "verification": verification,
            # 单位（元／千元／万元／百万元）随条目落盘：合并多份材料时
            # 要靠它判断口径是否一致，单位不同的表格绝不能直接相加。
            "built_units": dict(built.get("units") or {}),
            "regions": dict(built.get("regions") or {}),
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._store_analysis(entry, built)
        return entry

    def _store_analysis(self, entry: dict, built: dict) -> None:
        stem = os.path.splitext(entry["stored_path"])[0]
        path = stem + ".analysis.json"
        payload = _jsonable({**entry, "built": built})
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        entry["analysis_path"] = os.path.abspath(path)

    # ---------------------------------------------------------------- 产物

    @property
    def entries(self) -> list:
        return list(self._entries)

    def manifest(self) -> list:
        """语料清单（沿用原格式），使 corpus/index.py 可直接建索引。"""
        out = []
        for entry in self._entries:
            subject = entry["subject"]
            out.append({
                "code": subject.get("code") or "UNKNOWN",
                "date": subject.get("report_date") or "",
                "kind": subject.get("report_kind") or "unknown",
                "title": f"{subject.get('display') or entry['filename']} "
                         f"{subject.get('report_kind_label') or ''}".strip(),
                "url": "", "path": entry["stored_path"],
                "size": entry["size"], "pages": entry["pages"],
                "sha256": entry["sha256"],
            })
        return out

    def _write_manifest(self) -> None:
        payload = self.manifest()
        with open(os.path.join(self.dir, "manifest.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        # 按主体分目录再写一份，供 CorpusIndex(corpus_dir=本目录) 直接使用
        for item in payload:
            sub = os.path.join(self.dir, item["code"])
            os.makedirs(sub, exist_ok=True)
            path = os.path.join(sub, "manifest.json")
            existing = []
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as fh:
                        existing = json.load(fh)
                except Exception:
                    existing = []
            existing = [e for e in existing if e.get("path") != item["path"]]
            existing.append(item)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(sorted(existing, key=lambda e: e["date"]), fh,
                          ensure_ascii=False, indent=2)

    # ---------------------------------------------------------------- 归组

    def subject_key(self, entry: dict) -> str:
        """同一主体的归并键：优先用股票代码，其次用公司全称。

        代码解析失败时退回名称，是为了让"同一家公司的两份材料"仍能被认成
        同一个主体；若连名称都没有，则退回到文件名，各自成组、绝不硬凑。
        """
        subject = entry.get("subject") or {}
        return (subject.get("code") or subject.get("full_name")
                or subject.get("short_name") or entry.get("filename") or "UNKNOWN")

    def groups(self) -> dict:
        """按主体归组，组内按期排序。"""
        out: dict = {}
        for entry in self._entries:
            out.setdefault(self.subject_key(entry), []).append(entry)
        for key in out:
            out[key] = sorted(out[key], key=lambda e: (
                (e.get("subject") or {}).get("report_date") or "",
                (e.get("subject") or {}).get("report_kind") or ""))
        return out

    def load_built(self, entry: dict) -> dict:
        """取回某条上传的完整装配结果（含 frames，从缓存读）。"""
        path = entry.get("analysis_path")
        if not path or not os.path.exists(path):
            stem = os.path.splitext(entry["stored_path"])[0]
            path = stem + ".analysis.json"
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        built = payload["built"]
        built["frames"] = {k: _frame_from_json(v) for k, v in built["frames"].items()}
        built["table"] = _frame_from_json(built.get("table"))
        return built

    def merged(self, key: str) -> dict:
        """把同一主体的多份材料合并成一份 frames。

        合并规则：**本期口径优先于比较列**，同一报告期只保留一条。
        这样"一季报的上年同期"不会覆盖"上年一季报的本期"——
        两者数值相同，但后者才是一手来源，页码也能指到原始那张表。
        """
        members = self.groups().get(key) or []
        if not members:
            raise UploadError(f"没有找到主体 {key} 的材料")
        notes, excluded = [], []
        # 单位不一致时只保留多数派，并明确告知被排除的材料：
        # 把以万元列示的报表与以元列示的报表相加，会得到一个差一万倍的数，
        # 而且不会有任何报错，因此这里必须拒绝合并。
        kept = list(members)
        for report in REPORTS:
            values = []
            for entry in members:
                values.append(_unit_scale(entry, report))
            if len(set(values)) > 1:
                majority = max(set(values), key=values.count)
                keep_ok, bad = [], []
                for entry, value in zip(members, values):
                    (keep_ok if value == majority else bad).append(entry)
                if bad:
                    for entry in bad:
                        excluded.append(entry)
                        notes.append(
                            f"{entry['filename']} 的{_report_label(report)}以"
                            f"{_unit_label(values[members.index(entry)])}列示，"
                            f"与同组其它材料不一致，已从本表合并中排除")
                    kept = keep_ok
        loaded = [(entry, self.load_built(entry)) for entry in kept]
        frames = {}
        for report in REPORTS:
            rows = []
            for entry, built in loaded:
                df = built["frames"].get(report)
                if df is None or df.empty:
                    continue
                primary = pd.Timestamp((built.get("subject") or {})["report_date"])
                for _, row in df.iterrows():
                    rows.append((1 if row["report_date"] == primary else 0, row))
            if not rows:
                frames[report] = pd.DataFrame(columns=["report_date"])
                continue
            rows.sort(key=lambda item: (-item[0], item[1]["report_date"]))
            seen, ordered = set(), []
            for _, row in rows:
                ts = row["report_date"]
                if ts in seen:
                    continue
                seen.add(ts)
                ordered.append(row)
            df = pd.DataFrame(ordered).sort_values("report_date").reset_index(drop=True)
            frames[report] = df
        from finagent.metrics import indicators as I
        table, flags = I.compute_all(frames)
        # 单季还原需要"同一主体既能看到年报分季度表、又能看到累计数"，
        # 因此只在合并后的层面做——这也正是环比能不能算的判据。
        extra = verify.single_quarter_check([b for _, b in loaded])
        return {"frames": frames, "table": table, "flags": flags,
                "members": kept, "excluded": excluded, "notes": notes,
                "subject_key": key,
                "verification": _merge_verification(kept, extra)}


def _unit_scale(entry: dict, report: str) -> float:
    built_units = entry.get("built_units") or {}
    if built_units:
        return round(float(built_units.get(report, 1.0)), 6)
    # 条目的 units 存在 analysis.json 的 built 里，未回填时按"元"处理
    return 1.0


_UNIT_LABEL = {1.0: "元", 1e3: "千元", 1e4: "万元", 1e6: "百万元"}


def _unit_label(scale: float) -> str:
    return _UNIT_LABEL.get(round(float(scale), 6), f"×{scale:g}")


def _report_label(report: str) -> str:
    return assemble.REPORT_LABEL.get(report, report)


def _merge_verification(entries: list, extra: list = ()) -> dict:
    """把各份材料的校验结果汇总，供报告与界面呈现。

    extra 是跨材料才能做的校验（单季还原）。它同样要计入 passed/failed，
    否则界面上"校验全通过"会掩盖一条真实的未通过项。
    """
    checks, suspect = [], {}
    for entry in entries:
        result = entry.get("verification") or {}
        for check in result.get("checks") or []:
            check = dict(check)
            check["file"] = entry.get("filename")
            checks.append(check)
        for field, reason in (result.get("suspect") or {}).items():
            suspect.setdefault(field, reason)
    for check in extra:
        check = dict(check)
        check.setdefault("file", None)
        checks.append(check)
        if check.get("level") == "fail":
            for field in check.get("fields") or []:
                suspect.setdefault(field, check.get("detail") or "单季还原校验未通过")
    return {"checks": checks, "suspect": suspect,
            "failed": sum(1 for c in checks if c.get("level") == "fail"),
            "passed": sum(1 for c in checks if c.get("level") == "pass"),
            "files": [e.get("filename") for e in entries]}


def _frame_from_json(payload) -> pd.DataFrame:
    """把落盘的表格还原成 DataFrame（report_date 还原为 Timestamp）。"""
    if not payload:
        return pd.DataFrame(columns=["report_date"])
    df = pd.DataFrame(payload)
    if "report_date" not in df.columns:
        return pd.DataFrame(columns=["report_date"])
    df["report_date"] = pd.to_datetime(df["report_date"])
    ordered = ["report_date"] + [c for c in df.columns if c != "report_date"]
    return df[ordered].sort_values("report_date").reset_index(drop=True)


def _jsonable(value):
    """递归转成可 JSON 化的结构（DataFrame / Timestamp / numpy 标量）。"""
    if isinstance(value, pd.DataFrame):
        return _jsonable(value.to_dict(orient="records"))
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            return str(value)
    if isinstance(value, float) and value != value:
        return None
    return value


_UNSAFE = re.compile(r"[^\w\u4e00-\u9fa5.\-（）()]+")


def _safe_name(name: str) -> str:
    """把上传文件名收敛成安全的本地文件名，同时保留可读性。"""
    name = os.path.basename(name or "upload.pdf")
    name = _UNSAFE.sub("_", name).strip("._") or "upload.pdf"
    if not name.lower().endswith(DOC_EXT):
        name += DOC_EXT
    return name[:120]
