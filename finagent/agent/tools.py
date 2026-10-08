"""工具注册表：把系统能力封装为大模型可调用的 Tool。

竞赛要求提交"Tool"核心模块。本模块的设计约束是：

1. 数值只来自确定性计算层。模型无论怎么调用工具，都拿不到"模型算出来的数"，
   这是整套系统可复现性的根基。
2. 每个返回值都携带证据 ID 或原文出处。模型据此写出的每条结论都可回溯。
3. 工具描述即 Prompt。description 字段直接进入模型的决策上下文，
   因此它同时也是引导模型正确使用工具的手段。
"""

from __future__ import annotations

import json
from typing import Callable, NamedTuple

import pandas as pd

from finagent.corpus.index import CorpusIndex
from finagent.datasource.cninfo import load_manifest
from finagent.datasource.schema import FIELD_MAP
from finagent.datasource.eastmoney import EastMoneySource, evidence_id
from finagent.metrics import indicators as I
from finagent.metrics.indicators import FORMULAS, LABELS
from finagent.metrics.periods import metric_table
from finagent import skills as skill_lib
from finagent.validate.anomaly import run_rules
from finagent.validate.articulation import run_articulation


class Tool(NamedTuple):
    name: str
    description: str
    parameters: dict
    handler: Callable

    def schema(self) -> dict:
        """OpenAI function-calling 格式的工具声明。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def normalize_code(code: str) -> str:
    code = str(code).strip().upper()
    if "." in code:
        return code
    if code.startswith("6"):
        return f"{code}.SH"
    return f"{code}.SZ"


def parse_period(period: str):
    """把 '2026Q2' / '2026-06-30' 统一解析为 (年, 年内序号)。"""
    p = str(period).strip().upper()
    if "Q" in p:
        year, q = p.split("Q")
        return int(year), int(q)
    ts = pd.Timestamp(p)
    return ts.year, {3: 1, 6: 2, 9: 3, 12: 4}[ts.month]


class FinanceTools:
    """工具实现集合。持有取数缓存与语料索引，供一次运行内复用。"""

    def __init__(self, cfg: dict, trace=None, focus_code: str | None = None) -> None:
        self.cfg = cfg
        self.trace = trace
        self._frames: dict = {}
        self._table: dict = {}
        self._index: CorpusIndex | None = None
        # 本次分析的主角。语料检索与公告检索默认只在这一家公司的材料内进行——
        # 否则"分析工商银行"时会检索出牧原、京东方乃至任何配置内公司的原文，
        # 结论看似有出处，证据链却指向了别的公司。这是最隐蔽也最严重的错误之一。
        self.focus_code = normalize_code(focus_code).split(".")[0] if focus_code else None

    # ---------------- 内部缓存 ----------------

    def frames(self, code: str) -> dict:
        code = normalize_code(code)
        if code not in self._frames:
            src = EastMoneySource(cache_dir=self.cfg["data"]["cache_dir"], trace=self.trace)
            self._frames[code] = src.frames(code)
        return self._frames[code]

    def table(self, code: str) -> pd.DataFrame:
        code = normalize_code(code)
        if code not in self._table:
            tbl, _ = I.compute_all(self.frames(code), self.trace)
            self._table[code] = tbl
        return self._table[code]

    def index(self) -> CorpusIndex:
        if self._index is None:
            self._index = CorpusIndex().load()
        return self._index

    # ---------------- 工具实现 ----------------

    def list_periods(self, code: str) -> dict:
        tbl = self.table(code)
        return {
            "code": normalize_code(code),
            "periods": [f"{y}Q{q}" for y, q in tbl.index][-12:],
            "note": "报告期为累计口径：Q1=一季报，Q2=中报，Q3=三季报，Q4=年报。",
        }

    def get_indicator(self, code: str, indicator: str, period: str) -> dict:
        code_n = normalize_code(code)
        tbl = self.table(code_n)
        key = indicator if indicator in tbl.columns else _resolve(indicator, tbl.columns)
        if key is None:
            return {"error": f"未知指标 {indicator}",
                    "available": sorted(tbl.columns.tolist())}
        y, q = parse_period(period)
        if (y, q) not in tbl.index:
            return {"error": f"无该报告期数据 {period}",
                    "available": [f"{a}Q{b}" for a, b in tbl.index][-12:]}
        value = tbl.loc[(y, q), key]
        value = None if pd.isna(value) else float(value)
        return {
            "code": code_n,
            "indicator": key,
            "label": _label(key),
            "formula": FORMULAS.get(key, "报表原始科目（累计值）"),
            "period": f"{y}Q{q}",
            "value": value,
            "unit": "元" if key in I.BASE_METRICS else "比率/倍数",
            "evidence_id": evidence_id(code_n, f"{y}Q{q}", key),
            "source": "定期报告（第三方接口核验）",
        }

    def compute_growth(self, code: str, indicator: str, period: str) -> dict:
        """计算同比/环比。环比基于还原后的单季度值，而非累计值。"""
        code_n = normalize_code(code)
        frames = self.frames(code_n)
        y, q = parse_period(period)
        df = frames.get("income") if indicator in frames.get("income", pd.DataFrame()).columns else None
        if df is None:
            for name in ("income", "cashflow", "balance"):
                cand = frames.get(name)
                if cand is not None and indicator in cand.columns:
                    df = cand
                    break
        if df is None:
            return {"error": f"指标 {indicator} 不在可用报表中"}
        t = metric_table(df, indicator, _label(indicator))
        row = t[(t["year"] == y) & (t["q"] == q)]
        if row.empty:
            return {"error": f"无该报告期数据 {period}"}
        r = row.iloc[0]
        return {
            "code": code_n,
            "indicator": indicator,
            "label": _label(indicator),
            "period": f"{y}Q{q}",
            "cumulative": _f(r["cumulative"]),
            "single_quarter": _f(r["single_quarter"]),
            "yoy_cum_pct": _f(r["yoy_cum_pct"]),
            "yoy_single_pct": _f(r["yoy_single_pct"]),
            "qoq_single_pct": _f(r["qoq_single_pct"]),
            "note": r.get("note") or "",
            "method": "单季度值 = 本期累计 - 上期累计；环比基于单季度值计算",
        }

    def run_anomaly_rules(self, code: str, since_year: int = 2025) -> dict:
        code_n = normalize_code(code)
        findings = run_rules(code_n, self.frames(code_n), self.table(code_n),
                             self.cfg, self.trace, since_year=int(since_year))
        return {
            "code": code_n,
            "count": len(findings),
            "findings": [{
                "rule": f.rule, "level": f.level, "category": f.category,
                "period": f.period, "title": f.title, "fact": f.statement,
                "evidence": [e.as_dict() for e in f.evidence],
            } for f in findings],
        }

    def check_articulation(self, code: str) -> dict:
        code_n = normalize_code(code)
        checks = run_articulation(self.frames(code_n), code_n, self.cfg, self.trace)
        balance = [c for c in checks if "资产=" in c["check"]]
        indirect = [c for c in checks if "间接法" in c["check"]]
        return {
            "code": code_n,
            "balance_identity": {
                "periods": len(balance),
                "passed": sum(1 for c in balance if c["passed"]),
                "max_rel_pct": max((c["rel_pct"] for c in balance), default=None),
            },
            "indirect_method": [{
                "period": c["period"], "residual": c["residual"],
                "residual_pct": c["residual_pct"], "passed": c["passed"],
            } for c in indirect],
        }

    def search_disclosure(self, query: str, code: str = None, kind: str = None,
                          topk: int = 5) -> dict:
        """在本地公告全文中检索。返回原文片段与页码，供交叉验证结论。"""
        # 索引里的 code 是 6 位交易所代码，过滤时须用同一口径，
        # 否则带 .SZ/.SH 后缀的入参会静默匹配为空。
        # 未指定 code 时收敛到本次分析主体，防止跨公司取证污染证据链。
        bare = code.split(".")[0] if code else self.focus_code
        hits = self.index().search(query, code=bare, kind=kind, topk=int(topk))
        return {
            "query": query,
            "results": [{
                "citation": h.citation(),
                "doc_id": h.doc_id,
                "page": h.page,
                "score": h.score,
                "text": h.text[:900],
            } for h in hits],
        }

    def read_page(self, doc_id: str, page: int) -> dict:
        """精确读取指定公告的指定页，用于逐字核对。"""
        idx = self.index()
        for ch in idx.chunks:
            if ch["doc_id"] == doc_id and ch["page"] == int(page):
                return {"doc_id": doc_id, "page": int(page),
                        "title": ch["title"], "date": ch["date"], "text": ch["text"]}
        return {"error": f"未找到 {doc_id} 第 {page} 页"}

    def list_corpus(self, code: str = None) -> dict:
        """列出本地已有的公告原文。全部分析均基于这批文件。

        未指定 code 时默认只列本次分析主体的材料；只有显式传入 code
        或本次运行没有主体（如未指定标的的全局巡检）才铺开全部语料。
        """
        if code:
            codes = [normalize_code(code).split(".")[0]]
        elif self.focus_code:
            codes = [self.focus_code]
        else:
            codes = [c["code"].split(".")[0] for c in self.cfg.get("companies", [])]
        out = []
        for c in codes:
            for entry in load_manifest(c, self.cfg["data"]["corpus_dir"]):
                out.append({"code": c, "date": entry["date"], "kind": entry["kind"],
                            "title": entry["title"], "doc_id":
                                f"{c}_{entry['date']}_{entry['kind']}",
                            "size_mb": round(entry["size"] / 1e6, 2)})
        return {"documents": len(out), "items": out}

    def list_skills(self) -> dict:
        """列出可用的分析方法论。"""
        return {"skills": skill_lib.catalog()}

    def load_skill(self, name: str) -> dict:
        """加载指定技能的完整执行步骤与判读标准。"""
        try:
            body = skill_lib.load_skill(name)
        except KeyError as exc:
            return {"error": str(exc)}
        return {"name": name, "content": body}

    def compare_companies(self, codes: list = None) -> dict:
        """同口径跨公司对照。所有指标均由同一套公式计算，具备可比性。"""
        codes = [normalize_code(c) for c in (codes or
                 [c["code"] for c in self.cfg.get("companies", [])])]
        keys = ["revenue", "parent_net_profit", "netcash_operate",
                "cash_conversion_naive", "cash_conversion_adjusted",
                "dep_to_revenue", "net_margin", "collect_ratio"]
        out = []
        for code in codes:
            tbl = self.table(code)
            if tbl.empty:
                continue
            y, q = tbl.index[-1]
            row = tbl.loc[(y, q)]
            out.append({
                "code": code, "period": f"{y}Q{q}",
                "metrics": {k: (None if pd.isna(row.get(k)) else round(float(row[k]), 4))
                            for k in keys if k in tbl.columns},
            })
        return {"comparison": out}


def _label(key: str) -> str:
    """统一解析中文标签：衍生指标取 LABELS，报表原始科目取 FIELD_MAP。"""
    if key in LABELS:
        return LABELS[key]
    spec = FIELD_MAP.get(key)
    return spec.label if spec else key


def _resolve(name: str, columns) -> str | None:
    """宽松匹配指标名：支持中文标签、别名。"""
    if name in columns:
        return name
    for key, label in LABELS.items():
        if name == label or name in label:
            return key if key in columns else None
    lowered = str(name).lower()
    for col in columns:
        if lowered in col.lower():
            return col
    return None


def _f(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        return round(float(v), 4)
    except (TypeError, ValueError):
        return None


# ---------------- 工具声明 ----------------

def build_tools(cfg: dict, trace=None, focus_code: str | None = None) -> dict:
    """构建工具注册表，返回 name -> Tool。"""
    impl = FinanceTools(cfg, trace, focus_code=focus_code)

    specs = [
        Tool(
            "list_corpus",
            "列出本地已落盘的公告原文清单（年份、类型、标题、文档ID）。"
            "在引用原文前先调用它，确认有哪些材料可用。"
            "留空时默认只列本次分析主体的材料。",
            {"type": "object", "properties": {
                "code": {"type": "string",
                         "description": "股票代码，如 002714；留空则取本次分析主体"}},
             "required": []},
            impl.list_corpus,
        ),
        Tool(
            "list_periods",
            "列出某公司可用的报告期。报告期为累计口径：Q1=一季报，Q2=中报，"
            "Q3=三季报，Q4=年报。",
            {"type": "object", "properties": {
                "code": {"type": "string", "description": "股票代码，如 002714"}},
             "required": ["code"]},
            impl.list_periods,
        ),
        Tool(
            "get_indicator",
            "读取指定公司、指定报告期、指定指标的数值。返回值为程序计算结果，"
            "并附证据ID、计算公式与来源。禁止自行计算或推算，一律使用本工具返回值。",
            {"type": "object", "properties": {
                "code": {"type": "string"},
                "indicator": {"type": "string", "description":
                              "指标名，如 parent_net_profit、asset_impairment、"
                              "cash_conversion_adjusted、dep_to_revenue 等"},
                "period": {"type": "string", "description": "报告期，如 2026Q2"}},
             "required": ["code", "indicator", "period"]},
            impl.get_indicator,
        ),
        Tool(
            "compute_growth",
            "计算指标的累计同比、单季度同比与单季度环比。环比基于还原后的单季度值，"
            "而不是累计值相除——后者会得出错误结论。",
            {"type": "object", "properties": {
                "code": {"type": "string"},
                "indicator": {"type": "string"},
                "period": {"type": "string", "description": "报告期，如 2026Q2"}},
             "required": ["code", "indicator", "period"]},
            impl.compute_growth,
        ),
        Tool(
            "run_anomaly_rules",
            "运行异常信号规则引擎，返回全部命中的结论。结论分为三类："
            "异常信号（数据偏离常态）、结构性特征（看似异常实为资产结构使然）、"
            "口径提示（计算口径存在限制）。务必区分三类，不可混为一谈。",
            {"type": "object", "properties": {
                "code": {"type": "string"},
                "since_year": {"type": "integer", "description": "只返回该年份及之后，默认2025"}},
             "required": ["code"]},
            impl.run_anomaly_rules,
        ),
        Tool(
            "check_articulation",
            "执行勾稽校验：资产负债表恒等式、以及间接法反推经营活动现金流量净额。"
            "用于判断解析出的数据是否自洽。残差不会被抹平，残差大本身就是线索。",
            {"type": "object", "properties": {"code": {"type": "string"}},
             "required": ["code"]},
            impl.check_articulation,
        ),
        Tool(
            "search_disclosure",
            "在本地公告全文中做全文检索，返回原文片段、所在公告与页码。"
            "用于验证规则引擎提出的假设——例如怀疑减值来自存货跌价，"
            "就用本工具检索附注确认。这是把推论落到证据上的唯一途径。",
            {"type": "object", "properties": {
                "query": {"type": "string", "description": "检索词，如「存货跌价准备」"},
                "code": {"type": "string", "description": "限定股票代码，可选"},
                "kind": {"type": "string", "description":
                         "限定公告类型：annual/semi/q1/q3，可选"},
                "topk": {"type": "integer", "description": "返回条数，默认5"}},
             "required": ["query"]},
            impl.search_disclosure,
        ),
        Tool(
            "read_page",
            "精确读取某份公告的指定页全文，用于逐字核对数字或表述。",
            {"type": "object", "properties": {
                "doc_id": {"type": "string", "description": "形如 002714_2026-08-21_semi"},
                "page": {"type": "integer"}},
             "required": ["doc_id", "page"]},
            impl.read_page,
        ),
        Tool(
            "list_skills",
            "列出可用的分析方法论（技能库）。技能沉淀了成熟的分析程序与判读标准，"
            "包括各类「常见误判」的提醒。开始分析前先看一遍有哪些技能可用。",
            {"type": "object", "properties": {}, "required": []},
            impl.list_skills,
        ),
        Tool(
            "load_skill",
            "加载指定技能的完整内容：适用场景、执行步骤、判读规则、常见误判。"
            "遇到对应类型的分析任务时，先加载技能再动手，可避免方法上的疏漏。",
            {"type": "object", "properties": {
                "name": {"type": "string", "description": "技能名，如 cash_flow_quality"}},
             "required": ["name"]},
            impl.load_skill,
        ),
        Tool(
            "compare_companies",
            "同口径跨公司对照。所有公司使用完全相同的指标公式，"
            "因此结果具备横向可比性；可直接看出资产结构差异导致的结论差异。",
            {"type": "object", "properties": {
                "codes": {"type": "array", "items": {"type": "string"},
                          "description": "股票代码列表，留空则对照全部配置公司"}},
             "required": []},
            impl.compare_companies,
        ),
    ]
    return {t.name: t for t in specs}


def tool_schemas(registry: dict) -> list:
    return [t.schema() for t in registry.values()]
