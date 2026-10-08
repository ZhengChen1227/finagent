"""本地语料索引：公告原文的检索基础。

这是"封闭数据环境"的实现核心。索引一旦建成，系统运行期只读取本机文件，
不再访问任何外部服务——检索、抽取、引用全部可离线完成。

检索算法采用字符二元组 + BM25。之所以不引入分词库：
中文财经文本中"非经常性损益""递延所得税资产""生产性生物资产"等术语
既长又固定，字符二元组能稳定命中这类术语，且不引入额外依赖，
保证评审环境零配置即可复现。
"""

from __future__ import annotations

import json
import math
import os
import re
from collections import defaultdict
from typing import NamedTuple

from pypdf import PdfReader

_PAGE_MARK = "\n\n<<<PAGE:{n}>>>\n\n"


class Chunk(NamedTuple):
    doc_id: str
    code: str
    title: str
    date: str
    kind: str
    page: int
    text: str

    def citation(self) -> str:
        return f"{self.title}（{self.date}）第 {self.page} 页"


def tokens(text: str) -> list:
    """字符二元组 + 英文数字词。"""
    compact = re.sub(r"\s+", "", text)
    grams = [compact[i:i + 2] for i in range(len(compact) - 1)]
    words = re.findall(r"[A-Za-z][A-Za-z0-9\.\-]*|\d+(?:\.\d+)?%?", compact)
    return grams + words


class CorpusIndex:
    """公告全文索引。"""

    def __init__(self, corpus_dir: str = "data/corpus",
                 index_dir: str = "data/corpus_index") -> None:
        self.corpus_dir = corpus_dir
        self.index_dir = index_dir
        self.chunks: list = []
        self._postings: dict = {}
        self._df: dict = {}
        self._len: list = []
        self._avgdl: float = 0.0
        self._loaded = False

    # ---------------- 建索引 ----------------

    def build(self, codes=None, force: bool = False, trace=None) -> int:
        manifest_all = []
        for code in (codes or self._codes()):
            path = os.path.join(self.corpus_dir, code, "manifest.json")
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as fh:
                    manifest_all.extend(json.load(fh))

        os.makedirs(self.index_dir, exist_ok=True)
        text_dir = os.path.join(self.index_dir, "_text")
        os.makedirs(text_dir, exist_ok=True)

        rows = []
        for entry in manifest_all:
            doc_id = f"{entry['code']}_{entry['date']}_{entry['kind']}"
            txt_path = os.path.join(text_dir, doc_id + ".txt")
            if not os.path.exists(txt_path) or force:
                self._extract(entry["path"], txt_path, trace)
            rows.extend(self._chunk(doc_id, entry, txt_path))

        chunks_path = os.path.join(self.index_dir, "chunks.jsonl")
        with open(chunks_path, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        if trace:
            trace.tool_call("build_index", {"documents": len(manifest_all)},
                            {"chunks": len(rows), "path": chunks_path})
        return len(rows)

    def _codes(self) -> list:
        if not os.path.isdir(self.corpus_dir):
            return []
        return [d for d in os.listdir(self.corpus_dir)
                if os.path.isdir(os.path.join(self.corpus_dir, d))]

    @staticmethod
    def _extract(pdf_path: str, txt_path: str, trace=None) -> None:
        reader = PdfReader(pdf_path)
        parts = []
        for i, page in enumerate(reader.pages, start=1):
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            parts.append(_PAGE_MARK.format(n=i) + text)
        with open(txt_path, "w", encoding="utf-8") as fh:
            fh.write("".join(parts))
        if trace:
            trace.file_access(txt_path, "write", f"抽取文本 {len(reader.pages)} 页")

    @staticmethod
    def _chunk(doc_id: str, entry: dict, txt_path: str) -> list:
        with open(txt_path, "r", encoding="utf-8") as fh:
            raw = fh.read()
        rows = []
        for part in raw.split("<<<PAGE:"):
            if ">>>" not in part:
                continue
            head, _, body = part.partition(">>>")
            try:
                page = int(head.strip())
            except ValueError:
                continue
            body = body.strip()
            if len(body) < 40:      # 跳过空白页与纯页眉页脚
                continue
            rows.append({
                "doc_id": doc_id, "code": entry["code"], "title": entry["title"],
                "date": entry["date"], "kind": entry["kind"],
                "page": page, "text": body,
            })
        return rows

    # ---------------- 加载与检索 ----------------

    def load(self) -> "CorpusIndex":
        if self._loaded:
            return self
        chunks_path = os.path.join(self.index_dir, "chunks.jsonl")
        if not os.path.exists(chunks_path):
            self.build()
        with open(chunks_path, "r", encoding="utf-8") as fh:
            self.chunks = [json.loads(line) for line in fh if line.strip()]

        postings = defaultdict(list)
        df = defaultdict(int)
        lengths = []
        for i, ch in enumerate(self.chunks):
            tf = defaultdict(int)
            for tok in tokens(ch["text"]):
                tf[tok] += 1
            lengths.append(sum(tf.values()) or 1)
            for tok, n in tf.items():
                postings[tok].append((i, n))
                df[tok] += 1
        self._postings = dict(postings)
        self._df = dict(df)
        self._len = lengths
        self._avgdl = (sum(lengths) / len(lengths)) if lengths else 1.0
        self._loaded = True
        return self

    def search(self, query: str, code: str | None = None, kind: str | None = None,
               topk: int = 8, k1: float = 1.5, b: float = 0.75) -> list:
        """BM25 检索，返回带出处的文本块。"""
        self.load()
        if not self.chunks:
            return []
        n_docs = len(self.chunks)
        scores = defaultdict(float)
        for tok in set(tokens(query)):
            posting = self._postings.get(tok)
            if not posting:
                continue
            idf = math.log(1 + (n_docs - self._df[tok] + 0.5) / (self._df[tok] + 0.5))
            for idx, freq in posting:
                dl = self._len[idx]
                denom = freq + k1 * (1 - b + b * dl / self._avgdl)
                scores[idx] += idf * freq * (k1 + 1) / denom

        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        out = []
        for idx, score in ranked:
            ch = self.chunks[idx]
            if code and ch["code"] != code:
                continue
            if kind and ch["kind"] != kind:
                continue
            chunk = Chunk(ch["doc_id"], ch["code"], ch["title"], ch["date"],
                          ch["kind"], ch["page"], ch["text"])
            out.append(_Scored(chunk, round(score, 2)))
            if len(out) >= topk:
                break
        return out


class _Scored(NamedTuple):
    chunk: Chunk
    score: float

    def __getattr__(self, item):
        return getattr(self.chunk, item)
