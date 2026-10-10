"""装配层：把抽取到的科目装成 canonical 契约的 frames。

存在的意义：本模块是整个改造的"适配器"。它产出与原来的联网取数**完全相同**
的契约——`{"income": DataFrame, "cashflow": DataFrame, "balance": DataFrame}`，
列为 canonical 字段名、外加一列 `report_date`，每行是一个报告期的**累计值**。
正因为契约一致，`metrics/`、`validate/`、`report/`、`agent/` 四层一行都不用改，
"换成上传驱动"这件事才不会牵动全系统。

一张财报自带比较列，因此**一份材料就能算出同比**，这是本方案的关键：

- 合并利润表／现金流量表的"上期金额"是**上年同期**，所以
  本期行 report_date = 报告期，上期行 report_date = 报告期减一年。
- 合并资产负债表的"上年年末余额"是**上一个资产负债表日**（上年 12 月 31 日），
  不是"去年同一天"。若按减一年处理，一季报与三季报的资产负债表会挂到
  一个根本不存在的报告期上，而指标层只认 3-31／6-30／9-30／12-31 四个日期，
  遇到非法日期会直接抛错——这个坑必须在这里填掉。

另外两件事也在这里完成：

1. **扣非归母净利润与每股收益只能来自「主要会计数据和财务指标」**，
   不在合并利润表里。抽不到它们，非经常性损益这条主线就断了。
   因此这两个科目在报表缺失时由摘要层补位，并如实标注来源。
2. **每一条数字都记下出处**（报表／页码／原文科目名），
   使报告里的每个数都能翻回 PDF 的某一页。这是"可核验"的物理基础。
"""

from __future__ import annotations

import pandas as pd

from finagent.datasource import schema
from finagent.ingest import docmeta as docmeta_mod

REPORTS = ("income", "cashflow", "balance")
REPORT_LABEL = {"income": "合并利润表", "cashflow": "合并现金流量表",
                "balance": "合并资产负债表", "supplement": "现金流量表补充资料"}

# 摘要层补位的科目：合并报表里没有，只有「主要会计数据和财务指标」才披露。
HIGHLIGHT_FIELDS = {
    "deduct_parent_net_profit": ("income", "扣非归母净利润"),
    "basic_eps": ("income", "基本每股收益"),
}


def prev_report_date(kind: str, report_date: str):
    """本期报告期 → 比较期报告期。

    利润表与现金流量表比的是"上年同期"，资产负债表比的是"上年年末"。
    两者在年报上重合，在季报上不同：2026-03-31 的资产负债表比较期是
    2025-12-31，而不是 2025-03-31。
    """
    current = pd.Timestamp(report_date)
    if kind == "balance":
        return current, pd.Timestamp(year=current.year - 1, month=12, day=31)
    return current, current - pd.DateOffset(years=1)


def _series(pairs: dict) -> dict:
    """只保留至少有一个非空值的列，避免整列空值污染宽表。"""
    return {key: value for key, value in pairs.items()
            if any(v is not None for v in value.values())}


def build(doc: dict, statements_out: dict, highlights_out: dict,
          source_pdf: str = "") -> dict:
    """装配 frames 与逐条证据。返回 dict，可直接交给指标层。"""
    kind = doc.get("report_kind")
    report_date = doc.get("report_date")
    if not kind or not report_date:
        raise ValueError("缺少报告类型或报告期，无法装配报表："
                         f"kind={kind!r} report_date={report_date!r}")
    cur_ts, prev_ts = prev_report_date(kind, report_date)
    # 资产负债表的比较列是"上年年末"，利润表与现金流量表比的是"上年同期"。
    # 两者在年报上重合，在半年报与季报上并不相同：2026-06-30 的资产负债表
    # 比较期是 2025-12-31（期初余额），而利润表的比较期是 2025-06-30。
    # 若一律按"上年同期"贴标签，期初余额会被写成 2025-06-30，
    # 于是"总资产同比"变成一个看似合理、实则是半年变动的错值——
    # 没有任何报错，只有错的结论。
    balance_prev = prev_report_date("balance", report_date)[1]
    code = doc.get("code") or ""

    frames, evidence = {}, []
    for report in REPORTS:
        compare_ts = balance_prev if report == "balance" else prev_ts
        fields = statements_out["fields"].get(report) or {}
        data, pages, origins = {}, {}, {}
        for key, item in fields.items():
            spec = schema.FIELD_MAP.get(key)
            values = {cur_ts: item.get("cur"), compare_ts: item.get("prev")}
            data[key] = values
            pages[key] = item.get("page")
            origins[key] = spec.label if spec else key
            for period, ts in (("本期", cur_ts), ("上期", compare_ts)):
                if item.get(period_key(period)) is not None:
                    evidence.append(_evidence(
                        code, report, key, origins[key], ts, period,
                        item[period_key(period)], item.get("page"),
                        "statements", source_pdf))
        # 摘要层补位。只在报表里确实没有该科目时才用，绝不覆盖报表原值——
        # 覆盖会让"报表与摘要不一致"这件事被悄悄抹平，而它正是口径变化的第一手线索。
        for key, (owner, label) in HIGHLIGHT_FIELDS.items():
            if owner != report or key in data:
                continue
            item = (highlights_out.get("key_metrics") or {}).get(key)
            if not item:
                continue
            values = {cur_ts: item.get("cur"), compare_ts: item.get("prev")}
            if not any(v is not None for v in values.values()):
                continue
            data[key] = values
            pages[key] = item.get("page")
            origins[key] = label
            for period, ts, field in (("本期", cur_ts, "cur"),
                                      ("上期", compare_ts, "prev")):
                if item.get(field) is not None:
                    evidence.append(_evidence(
                        code, report, key, label, ts, period, item[field],
                        item.get("page"), "highlights", source_pdf))
        rows = []
        for ts in (cur_ts, compare_ts):
            row = {"report_date": ts}
            for key, values in data.items():
                row[key] = values.get(ts)
            rows.append(row)
        df = pd.DataFrame(rows)
        # 两行都空说明这张表根本没抽到，返回空表而不是两行空值——
        # 指标层用 df.empty 判断"该公司无数据"，两行空值会被它当成有数据。
        if all(df.drop(columns=["report_date"]).isna().all()):
            df = pd.DataFrame(columns=["report_date"])
        frames[report] = df.sort_values("report_date").reset_index(drop=True)

    return {
        "frames": frames,
        "subject": {
            "code": doc.get("code"), "short_name": doc.get("short_name"),
            "full_name": doc.get("full_name"), "display": doc.get("display"),
            "report_kind": kind, "report_kind_label": doc.get("report_kind_label"),
            "report_date": str(cur_ts.date()),
            "compare_date": str(prev_ts.date()),
            # 资产负债表的比较期与利润表不同，必须单独给出来：
            # 界面与报告要用它说明"期初余额是哪一天"，不能含糊。
            "balance_compare_date": str(balance_prev.date()),
            "compare_note": "利润表与现金流量表为上年同期金额，"
                            "资产负债表为上年年末余额",
            "report_date_source": doc.get("report_date_source"),
        },
        "evidence": evidence,
        "contributions": {report: sorted((statements_out["fields"].get(report) or {}))
                          for report in REPORTS},
        "missing": statements_out.get("missing") or [],
        "regions": statements_out.get("regions") or {},
        "units": statements_out.get("units") or {},
        "highlights": highlights_out,
        "source_pdf": source_pdf,
    }


def period_key(period: str) -> str:
    return "cur" if period == "本期" else "prev"


def _evidence(code, report, key, label, ts, period, value, page,
              origin, source_pdf) -> dict:
    return {
        "report": report, "report_label": REPORT_LABEL.get(report, report),
        "field": key, "field_label": label, "report_date": str(ts.date()),
        "period": period, "value": value, "page": page, "origin": origin,
        "source_pdf": source_pdf,
        "evidence_id": schema.evidence_id(code or "*", ts.date(), key),
    }


def describe(subject: dict) -> str:
    """一句话说明这批材料是谁的哪一期，用于界面与轨迹。"""
    name = subject.get("display") or subject.get("full_name") or subject.get("code")
    label = subject.get("report_kind_label") or ""
    date = subject.get("report_date") or "未知报告期"
    return f"{name} {date} {label}".strip()


def period_order(subject: dict) -> tuple:
    """(年, 年内序数)：用于把多份材料的报告期排成时间轴。"""
    ts = pd.Timestamp(subject["report_date"])
    return ts.year, docmeta_mod.PERIOD_ORDER.get(subject.get("report_kind"), 0)
