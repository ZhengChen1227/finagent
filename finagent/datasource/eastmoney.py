# -*- coding: utf-8 -*-
"""东方财富数据源：采集、缓存与口径标准化。

定位说明：
本模块提供的是"第三方核验源"，用于交叉核对与快速取数。
报告中作为权威出处的仍是巨潮资讯网/交易所的原始公告，
两者比对结果会进入校验层的交叉核对环节。

全市场覆盖（这是"任意上市公司都能分析"的关键）：
A 股并非只有一套报表格式。银行、保险、证券三类金融企业适用金融业报表格式，
其科目名与一般工商业不同（营业总收入在 OPERATE_INCOME、减值在
CREDIT_IMPAIRMENT_LOSS、无形资产与长期待摊摊销合并为一列……）。
若只认一般格式，金融企业会整表取空——系统"能跑"但输出空白，
属于最危险的静默失败。因此本模块先解析报表族（G/B/I/S），再按族取数。

另一条硬约束：绝不把"网络失败"当成"没有数据"。
数据源会限流并返回空结果，若把空结果当作事实，系统会安安静静地
输出一份"该公司无披露"的报告。故本模块严格区分三种探测结果：
hit（有数据）/ empty（接口正常但确实无数据）/ error（请求失败）。
"""

from __future__ import annotations

import hashlib
import json
import os
import time

import pandas as pd
import requests

from finagent.datasource.codes import bare, normalize_code, secucode_candidates
from finagent.datasource.schema import FIELD_MAP, by_report
from finagent.trace import Trace

WEB_BASE = "https://datacenter-web.eastmoney.com/api/data/v1/get"
SEC_BASE = "https://datacenter.eastmoney.com/securities/api/data/v1/get"

# 报表族：G 一般工商业 / B 银行 / I 保险 / S 证券
FAMILY_REPORTS = {
    "G": {"income": "RPT_F10_FINANCE_GINCOME",
          "cashflow": "RPT_F10_FINANCE_GCASHFLOW",
          "balance": "RPT_F10_FINANCE_GBALANCE"},
    "B": {"income": "RPT_F10_FINANCE_BINCOME",
          "cashflow": "RPT_F10_FINANCE_BCASHFLOW",
          "balance": "RPT_F10_FINANCE_BBALANCE"},
    "I": {"income": "RPT_F10_FINANCE_IINCOME",
          "cashflow": "RPT_F10_FINANCE_ICASHFLOW",
          "balance": "RPT_F10_FINANCE_IBALANCE"},
    "S": {"income": "RPT_F10_FINANCE_SINCOME",
          "cashflow": "RPT_F10_FINANCE_SCASHFLOW",
          "balance": "RPT_F10_FINANCE_SBALANCE"},
}
FAMILY_LABEL = {"G": "一般工商业", "B": "银行", "I": "保险", "S": "证券"}
# 探测顺序：一般格式覆盖绝大多数公司，先试它可让九成以上公司只花一次请求。
FAMILY_ORDER = ("G", "B", "I", "S")

BASIC_REPORT = "RPT_F10_BASIC_ORGINFO"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://data.eastmoney.com/",
}

REQUEST_RETRIES = 3      # 数据源会限流，单次失败不足以判定"无数据"
RETRY_BACKOFF = 1.6      # 退避基数（秒）

HIT, EMPTY, ERROR = "hit", "empty", "error"


class DataSourceError(RuntimeError):
    """数据源不可达。必须显式抛出，不能退化成"该公司无数据"。"""


def evidence_id(secucode: str, report_date, canonical: str) -> str:
    """确定性证据 ID：同一 (主体, 报告期, 指标) 恒定，便于报告回溯与人工复核。"""
    spec = FIELD_MAP.get(canonical)
    token = "|".join([
        secucode, str(report_date),
        spec.report if spec else "-", spec.field if spec else canonical,
    ])
    return "EM-" + hashlib.sha1(token.encode("utf-8")).hexdigest()[:10].upper()


class EastMoneySource:
    """带本地缓存的取数客户端。

    缓存的作用不只是提速：它让同一次运行的输入冻结，保证结果可复现，
    不受数据源后续更新影响。
    """

    def __init__(self, cache_dir: str = "data/raw", trace: Trace | None = None,
                 refresh: bool = False) -> None:
        self.cache_dir = cache_dir
        self.trace = trace
        self.refresh = refresh
        os.makedirs(cache_dir, exist_ok=True)
        self._secucode_memo: dict = {}
        self._family_memo: dict = {}
        self._stmtcode_memo: dict = {r: {} for r in ("income", "cashflow", "balance")}

    # ------------------------------------------------------------ 基础查询

    @staticmethod
    def _url(report: str, secucode: str, page_size: int, sort: bool = True) -> str:
        """拼装查询地址。

        非时序报表（如主体基本信息）没有 REPORT_DATE 字段，
        带上 sortColumns=REPORT_DATE 会被接口判为空结果——
        这一度让行业解析静默返回空字典。故按时序与否分别拼装。
        """
        url = (f"{SEC_BASE}?reportName={report}&columns=ALL"
               f"&filter=(SECUCODE%3D%22{secucode}%22)&pageSize={page_size}")
        if sort:
            url += "&sortColumns=REPORT_DATE&sortTypes=-1"
        return url

    def query(self, report: str, secucode: str, page_size: int = 60,
              sort: bool = True) -> dict:
        """直接查询指定报表，不落盘。带重试，用于探测报表族、代码后缀与主体画像。"""
        url = self._url(report, secucode, page_size, sort=sort)
        last = None
        for attempt in range(REQUEST_RETRIES):
            try:
                resp = requests.get(url, headers=HEADERS, timeout=30)
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:                      # 限流/超时/网络抖动
                last = exc
                if attempt < REQUEST_RETRIES - 1:
                    time.sleep(RETRY_BACKOFF ** attempt)
        raise DataSourceError(f"数据源请求失败 {report} {secucode}: {last}")

    def probe(self, report: str, secucode: str, sort: bool = True) -> str:
        """探测报表是否有数据，返回 hit / empty / error。

        区分 empty 与 error 是本模块的关键设计：前者是事实（该公司确无该报表），
        后者是故障。混为一谈会让限流悄悄变成一份"无数据"的报告。
        """
        try:
            return HIT if _records(self.query(report, secucode, page_size=1, sort=sort)) else EMPTY
        except Exception:
            return ERROR

    # ------------------------------------------------------------ 磁盘缓存

    def _cache_path(self, name: str) -> str:
        return os.path.join(self.cache_dir, name)

    def _load(self, path: str):
        if os.path.exists(path) and not self.refresh:
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    return json.load(fh)
            except Exception:
                return None
        return None

    def _store(self, path: str, payload) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------ 代码解析

    def secucode(self, code: str) -> str:
        """把用户输入的任意写法解析成数据源可识别的 SECUCODE。

        解析结果落盘缓存：同一代码只探测一次，后续运行直接命中，
        既省请求也让"实际用了哪个代码"在磁盘上留痕、可人工复核。
        """
        key = str(code).strip().upper()
        if key in self._secucode_memo:
            return self._secucode_memo[key]

        path = self._cache_path(f"_secucode_{bare(key)}.json")
        cached = self._load(path)
        if isinstance(cached, dict) and cached.get("secucode"):
            self._secucode_memo[key] = cached["secucode"]
            if self.trace:
                self.trace.file_access(path, "read", f"代码解析命中缓存 {key}")
            return cached["secucode"]

        tried, statuses, resolved = [], [], None
        for candidate in self._candidate_list(key):
            status = self.probe(FAMILY_REPORTS["G"]["balance"], candidate)
            tried.append(candidate)
            statuses.append(status)
            if status == HIT:
                resolved = candidate
                break
        if resolved is None and all(s == ERROR for s in statuses):
            raise DataSourceError(
                f"代码 {key} 解析失败：数据源全部请求失败（疑似限流或断网），"
                f"已尝试 {tried}。请检查网络后重试，不要据此判断该公司无披露。")

        resolved = resolved or normalize_code(key)
        self._store(path, {"input": key, "secucode": resolved, "tried": tried,
                           "status": statuses})
        self._secucode_memo[key] = resolved
        if self.trace:
            self.trace.tool_call("resolve_secucode", {"input": key, "tried": tried},
                                 {"secucode": resolved, "status": statuses})
            self.trace.file_access(path, "write", f"代码解析落盘 {key} -> {resolved}")
        return resolved

    @staticmethod
    def _candidate_list(key: str) -> tuple:
        """一般格式取不到时，再试金融业三族，以覆盖金融企业的代码写法差异。"""
        return secucode_candidates(key)

    def family(self, secucode: str) -> str:
        """解析该主体适用的报表族（G/B/I/S）。

        以资产负债表为判别依据：同一主体只可能在其中一族的资产负债表里有数据。
        全部探测均失败（而非返回空）时抛出异常，不缓存、不猜测。
        """
        if secucode in self._family_memo:
            return self._family_memo[secucode]

        path = self._cache_path(f"_family_{bare(secucode)}.json")
        cached = self._load(path)
        if isinstance(cached, dict) and cached.get("family") in FAMILY_REPORTS:
            self._family_memo[secucode] = cached["family"]
            if self.trace:
                self.trace.file_access(path, "read", f"报表族命中缓存 {secucode}")
            return cached["family"]

        found, probed, statuses = None, [], []
        for family in FAMILY_ORDER:
            report = FAMILY_REPORTS[family]["balance"]
            status = self.probe(report, secucode)
            probed.append(report)
            statuses.append(status)
            if status == HIT:
                found = family
                break
        if found is None:
            if all(s == ERROR for s in statuses):
                raise DataSourceError(
                    f"{secucode} 报表族解析失败：数据源全部请求失败（疑似限流或断网）。")
            found = "G"          # 接口正常但四族皆空：该公司确实没有可用报表

        self._store(path, {"secucode": secucode, "family": found,
                           "label": FAMILY_LABEL[found], "probed": probed,
                           "status": statuses})
        self._family_memo[secucode] = found
        if self.trace:
            self.trace.tool_call("resolve_report_family", {"secucode": secucode},
                                 {"family": found, "label": FAMILY_LABEL[found],
                                  "probed": len(probed), "status": statuses})
            self.trace.file_access(path, "write", f"报表族落盘 {secucode} -> {found}")
        return found

    # ------------------------------------------------------------ 取数

    def _cache_path_stmt(self, family: str, report: str, secucode: str) -> str:
        """报表缓存以"实际取数用的完整代码"为键。

        文件名里保留后缀是必要的：同一主体在 .BJ 与 .NQ 下是两份不同的响应，
        若只按纯数字代码命名，两份数据会落到同一个文件、互相覆盖。
        落盘内容必须与其对应的查询一一对应，否则缓存就失去了可复核性。
        """
        return self._cache_path(f"{family}_{report}_{secucode}.json")

    def statement_code(self, report: str, code: str) -> str:
        """针对单张报表确定 SECUCODE，独立于主体代码。

        北交所存在"同一主体、数据分散在两个代码下"的情形：早期由新三板平移
        而来的公司，资产负债表挂在 .BJ 上，利润表与现金流量表却只挂在 .NQ 上。
        只用资产负债表判定主体代码，会把一家确有披露的公司读成"没有利润表"，
        而系统对此并不报错——这正属于最危险的一类静默错误。故按报表逐个确认。

        绝大多数公司主代码即命中，此时不额外发起探测请求，不留额外开销。
        """
        key = bare(code)
        memo = self._stmtcode_memo[report]
        if key in memo:
            return memo[key]

        path = self._cache_path(f"_stmtcode_{report}_{key}.json")
        cached = self._load(path)
        if isinstance(cached, dict) and cached.get("secucode"):
            memo[key] = cached["secucode"]
            if self.trace:
                self.trace.file_access(path, "read",
                                       f"{report} 取数代码命中缓存 {cached['secucode']}")
            return cached["secucode"]

        target = FAMILY_REPORTS[self.family(self.secucode(code))][report]
        primary = self.secucode(code)
        # 先用主代码实取一次：命中则直接沿用，不为常见情形多花一次请求。
        if _records(self.query(target, primary)):
            self._store(path, {"code": key, "report": report, "secucode": primary,
                               "tried": [primary], "status": [HIT]})
            memo[key] = primary
            return primary

        tried, statuses, resolved = [primary], [EMPTY], primary
        for candidate in secucode_candidates(code):
            if candidate == primary:
                continue
            status = self.probe(target, candidate)
            tried.append(candidate)
            statuses.append(status)
            if status == HIT:
                resolved = candidate
                break

        self._store(path, {"code": key, "report": report, "secucode": resolved,
                           "tried": tried, "status": statuses})
        memo[key] = resolved
        if self.trace and resolved != primary:
            self.trace.tool_call(
                "resolve_statement_code",
                {"report": report, "code": key, "tried": tried},
                {"secucode": resolved, "status": statuses},
            )
            self.trace.file_access(path, "write",
                                   f"{report} 改用 {resolved} 取数（主代码 {primary} 无数据）")
        return resolved

    def fetch(self, report: str, code: str) -> dict:
        secucode = self.statement_code(report, code)
        family = self.family(self.secucode(code))
        path = self._cache_path_stmt(family, report, secucode)
        if os.path.exists(path) and not self.refresh:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            if self.trace:
                self.trace.file_access(path, "read", f"{family}/{report}/{secucode} 命中缓存")
            return payload

        payload = self.query(FAMILY_REPORTS[family][report], secucode)
        self._store(path, payload)
        if self.trace:
            self.trace.file_access(path, "write", f"{family}/{report}/{secucode} 首次拉取并落盘")
            self.trace.tool_call(
                "http_get",
                {"url": self._url(FAMILY_REPORTS[family][report], secucode, 60),
                 "family": family, "family_label": FAMILY_LABEL[family]},
                {"rows": _row_count(payload)},
            )
        return payload

    def frame(self, report: str, code: str) -> pd.DataFrame:
        """取数并标准化为 DataFrame。按口径映射取值，首选字段缺失时回退备选字段。"""
        payload = self.fetch(report, code)
        records = _records(payload)
        specs = by_report(report)

        rows = []
        for rec in records:
            row = {"report_date": pd.to_datetime(rec.get("REPORT_DATE"))}
            for canon, spec in specs.items():
                row[canon] = _pick(rec, spec.candidates())
            rows.append(row)

        df = pd.DataFrame(rows)
        if df.empty:
            return df
        if self.trace:
            self.trace.tool_call(
                "normalize", {"report": report, "secucode": self.secucode(code)},
                {"rows": len(df), "columns": [c for c in df.columns if c != "report_date"]},
            )
        return df.sort_values("report_date").reset_index(drop=True)

    def frames(self, code: str) -> dict:
        """取全部报表，返回 {income, cashflow, balance}。"""
        return {report: self.frame(report, code) for report in ("income", "cashflow", "balance")}

    # ------------------------------------------------------------ 主体信息

    def security_name(self, code: str) -> str | None:
        """解析证券简称。

        作用：系统必须能处理配置之外的公司（决赛现场由评委提供样例数据），
        此时无法依赖 config.yaml 中的名称，只能从数据源解析。
        """
        profile = self.profile(code)
        if profile.get("name"):
            return profile["name"]
        for report in ("income", "cashflow", "balance"):
            try:
                for rec in _records(self.fetch(report, code)):
                    name = rec.get("SECURITY_NAME_ABBR")
                    if name:
                        return name
            except Exception:
                continue
        return None

    def profile(self, code: str) -> dict:
        """解析主体画像：简称、公司全称、行业、上市日期、注册地。

        用于"任意上市公司都能分析"场景：配置里没有的公司，
        报告仍应写出公司名称与所属行业，而不是以代码充当名称、以"未分类"充当行业。
        取不到时返回空字典，但会把失败原因记进轨迹，不做静默吞掉。
        """
        secucode = self.secucode(code)
        path = self._cache_path(f"_profile_{bare(secucode)}.json")
        cached = self._load(path)
        if isinstance(cached, dict) and cached.get("secucode"):
            if self.trace:
                self.trace.file_access(path, "read", f"主体画像命中缓存 {secucode}")
            return cached

        # 主体画像同样会遇到"两个代码"的情形：部分北交所主体的
        # 基本资料只挂在 .NQ 上（如 830964 润农节水）。
        # 不回退就会让报告把代码当公司名、把行业写成"未分类"。
        order = [secucode] + [c for c in secucode_candidates(code) if c != secucode]
        info: dict = {}
        tried, errors = [], 0
        for candidate in order:
            tried.append(candidate)
            try:
                recs = _records(self.query(BASIC_REPORT, candidate, page_size=1, sort=False))
            except Exception:
                errors += 1
                continue
            if not recs:
                continue
            rec = recs[0]
            info = {
                "secucode": candidate,
                "queried": candidate,
                "name": rec.get("SECURITY_NAME_ABBR") or "",
                "org_name": rec.get("ORG_NAME") or "",
                "industry": rec.get("EM2016") or "",
                "industry_csrc": rec.get("INDUSTRYCSRC1") or "",
                "province": rec.get("PROVINCE") or "",
                "listing_date": str(rec.get("LISTING_DATE") or "")[:10],
            }
            break

        if info:
            self._store(path, info)
            if self.trace:
                self.trace.tool_call("resolve_profile", {"secucode": secucode, "tried": tried},
                                     {"name": info.get("name"),
                                      "industry": info.get("industry"),
                                      "queried": info["queried"]})
                self.trace.file_access(path, "write", f"主体画像落盘 {info['queried']}")
        elif self.trace:
            reason = ("数据源请求全部失败（疑似限流或断网）" if errors == len(order)
                      else "接口正常但无该主体画像")
            self.trace.tool_call("resolve_profile", {"secucode": secucode, "tried": tried},
                                 {"ok": False, "error": reason}, ok=False)
        return info


def _pick(rec: dict, fields: tuple):
    """按候选字段顺序取值，返回第一个非空数值。

    金融业报表中"营业总收入"等科目名不同，回退机制让同一张口径表
    可以覆盖 G/B/I/S 四种报表格式，避免逐族维护四份字段表。
    """
    for name in fields:
        val = rec.get(name)
        if isinstance(val, bool):
            continue
        if isinstance(val, (int, float)):
            return float(val)
        if isinstance(val, str):
            try:
                return float(val)
            except ValueError:
                continue
    return None


def _records(payload) -> list:
    return ((payload or {}).get("result") or {}).get("data") or []


def _row_count(payload) -> int:
    return len(_records(payload))
