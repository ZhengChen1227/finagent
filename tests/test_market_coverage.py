"""全市场覆盖测试：验证"任意上市公司都能分析"这条要求在代码层成立。

这组用例刻意全部离线——只测规则与映射逻辑，不访问网络。
理由：覆盖度是系统正确性的地基，不能因为断网或数据源限流而测不出来。
真实取数由 smoke.py / test_rules.py 在联网环境下另行验证。

覆盖四件事：
  1. 证券代码归一化与上市地后缀（含北交所两段代码、沪市B股）
  2. 金融业四套报表格式的路由与科目口径回退
  3. 折旧摊销合并列不被重复加总（会直接污染调整后现金含量）
  4. 同一套指标在金融业下的定性：口径提示而非异常信号
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from finagent.datasource.codes import bare, market_of, normalize_code, secucode_candidates
from finagent.datasource.eastmoney import (
    EMPTY, FAMILY_LABEL, FAMILY_ORDER, FAMILY_REPORTS, HIT, EastMoneySource, _pick,
)
from finagent.datasource.schema import FIELD_MAP
from finagent.metrics import indicators as I
from finagent.validate.anomaly import (
    FINANCIAL_FAMILIES, rule_cash_conversion, rule_financial_indicator_caliber,
)

RESULTS: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok)))
    print(f"  [{'通过' if ok else '失败'}] {name}" + (f"  {detail}" if detail else ""))


# ------------------------------------------------------------------ 1 代码

def test_codes() -> None:
    print("代码归一化与上市地")
    cases = [
        ("600519", "600519.SH", "sh", ("600519.SH",)),      # 沪市主板
        ("688981", "688981.SH", "sh", ("688981.SH",)),      # 科创板
        ("900948", "900948.SH", "sh", ("900948.SH",)),      # 沪市 B 股
        ("000001", "000001.SZ", "sz", ("000001.SZ",)),      # 深市主板
        ("002714", "002714.SZ", "sz", ("002714.SZ",)),      # 深市主板（中小板并入）
        ("300750", "300750.SZ", "sz", ("300750.SZ",)),      # 创业板
        ("200725", "200725.SZ", "sz", ("200725.SZ",)),      # 深市 B 股
        ("835185", "835185.BJ", "bj", ("835185.BJ", "835185.NQ")),   # 北交所（原精选层）
        ("430047", "430047.BJ", "bj", ("430047.BJ", "430047.NQ")),
        ("873169", "873169.BJ", "bj", ("873169.BJ", "873169.NQ")),
        ("920002", "920002.BJ", "bj", ("920002.BJ", "920002.NQ")),   # 北交所新代码段
    ]
    for raw, norm, market, cands in cases:
        check(f"{raw} → {norm} / {market}", normalize_code(raw) == norm and market_of(raw) == market,
              normalize_code(raw) + " " + market_of(raw))
        check(f"{raw} 候选后缀", secucode_candidates(raw) == cands, str(secucode_candidates(raw)))

    # 幂等性：归一化后再归一化必须稳定，否则缓存与证据 ID 会漂移
    for raw in [c[0] for c in cases] + [c[1] for c in cases]:
        once = normalize_code(raw)
        check(f"归一化幂等 {raw}", normalize_code(once) == once, once)

    # 9 开头不能一律当沪市：920xxx 属北交所，900xxx 属沪市B股
    check("900xxx 归沪市", market_of("900948") == "sh")
    check("920xxx 归北交所", market_of("920002") == "bj")
    # 显式后缀优先于前缀推断
    check("显式后缀优先", secucode_candidates("835185.SH")[0] == "835185.SH")
    check("去后缀取值稳定", bare("000725.SZ") == "000725" and bare("725") == "000725")


# ------------------------------------------------------------------ 2 报表族

def test_families() -> None:
    print("金融业报表族路由")
    check("族集合完整", set(FAMILY_ORDER) == {"G", "B", "I", "S"}, str(FAMILY_ORDER))
    check("族标签完整", set(FAMILY_LABEL) == {"G", "B", "I", "S"})
    for fam in FAMILY_ORDER:
        tables = FAMILY_REPORTS[fam]
        check(f"族 {fam} 三张表齐备",
              set(tables) == {"income", "cashflow", "balance"}, str(sorted(tables)))
        check(f"族 {fam} 报表名唯一", len(set(tables.values())) == 3)
    check("金融业族集合", FINANCIAL_FAMILIES == ("B", "I", "S"))


def test_field_fallback() -> None:
    print("科目口径回退")
    # 金融业利润表的"营业总收入"写作 OPERATE_INCOME
    rev = FIELD_MAP["revenue"]
    check("营业收入回退链", rev.candidates() == ("TOTAL_OPERATE_INCOME", "OPERATE_INCOME"),
          str(rev.candidates()))
    cost = FIELD_MAP["operating_cost"]
    check("营业成本回退链", cost.candidates() == ("OPERATE_COST", "OPERATE_EXPENSE"),
          str(cost.candidates()))

    check("取值优先首选字段", _pick({"A": 1.0, "B": 2.0}, ("A", "B")) == 1.0)
    check("首选缺失回退备选", _pick({"A": None, "B": 2.0}, ("A", "B")) == 2.0)
    check("字段全缺取空", _pick({"A": None, "B": None}, ("A", "B")) is None)
    check("布尔值不被当作金额", _pick({"A": True, "B": 5.0}, ("A", "B")) == 5.0)
    check("字符串数字可解析", _pick({"A": "3.5"}, ("A",)) == 3.5)
    check("非数字字符串取空", _pick({"A": "--"}, ("A",)) is None)


# ------------------------------------------------------------------ 3 折旧摊销

def _frames(cashflow_cols: dict) -> dict:
    """构造最小现金流量表框架（列名为规范化科目名，与取数层输出一致）。"""
    rows = []
    for month, day in ((3, 31), (6, 30)):
        row = {"report_date": pd.Timestamp(f"2025-{month:02d}-{day:02d}")}
        row.update(cashflow_cols)
        rows.append(row)
    return {"income": pd.DataFrame(), "cashflow": pd.DataFrame(rows), "balance": pd.DataFrame()}


def test_dep_amort_no_double_count() -> None:
    print("折旧摊销合计（防重复加总）")
    # 工商业：分列披露无形资产摊销与长期待摊摊销 → 100 + 10 + 5
    split = _frames({"depr_fa": 100.0, "amort_ia": 10.0, "amort_lpe": 5.0})
    value = I.dep_amort(split).iloc[-1]
    check("分列披露时为三项之和", abs(value - 115.0) < 1e-9, f"{value}")

    # 金融业：只披露合并列 → 100 + 15，绝不能变成 100 + 15 + 15
    merged = _frames({"depr_fa": 100.0, "amort_ia_lpe": 15.0})
    value = I.dep_amort(merged).iloc[-1]
    check("合并列披露时取合并值一次", abs(value - 115.0) < 1e-9, f"{value}")

    # 两者并存时以分项为准（合并列只用于补空缺期），合并列刻意取不同值以便识别误加
    both = _frames({"depr_fa": 100.0, "amort_ia": 10.0, "amort_lpe": 5.0,
                    "amort_ia_lpe": 40.0})
    value = I.dep_amort(both).iloc[-1]
    check("两者并存不重复计算", abs(value - 115.0) < 1e-9, f"{value}")


# ------------------------------------------------------------------ 4 金融业定性

def _table(**cols) -> pd.DataFrame:
    """构造单报告期的指标表，索引与 compute_all 输出一致（year, q）。"""
    idx = pd.MultiIndex.from_tuples([(2026, 2)], names=["year", "q"])
    return pd.DataFrame({k: [float(v)] for k, v in cols.items()}, index=idx, dtype="float64")


def test_financial_caliber() -> None:
    print("金融业指标定性")
    cfg = {"thresholds": {"adjusted_cash_conv_low": 0.5, "adjusted_cash_conv_high": 1.8,
                          "depr_to_revenue_high_pct": 15.0}}
    # 银行典型的取值：现金含量数倍于 1，但这是存款科目造成的口径问题
    fin_table = _table(cash_conversion_naive=7.76, cash_conversion_adjusted=4.27,
                       gross_margin=0.45, dep_to_revenue=0.037)
    out = rule_cash_conversion("601398.SH", {}, cfg, fin_table, "B")
    check("金融业现金含量定性为口径提示",
          len(out) == 1 and out[0].category == "口径提示" and out[0].rule == "R2c",
          f"{[(f.rule, f.category) for f in out]}")
    check("金融业不再报异常信号", all(f.category != "异常信号" for f in out))

    # 工商业同样的取值必须仍然按异常信号处理（不能把新规则做成"一刀切豁免"）
    out_g = rule_cash_conversion("000725.SZ", {}, cfg, fin_table, "G")
    check("工商业同值仍判定为异常信号",
          any(f.category == "异常信号" and f.rule == "R2b" for f in out_g),
          str([(f.rule, f.category) for f in out_g]))

    # 口径适配说明：只在金融业出现
    out_c = rule_financial_indicator_caliber("601398.SH", {}, cfg, fin_table, "B")
    check("金融业口径适配说明存在", len(out_c) == 1 and out_c[0].rule == "R8",
          str([f.rule for f in out_c]))
    check("口径提示带可解析报告期", out_c[0].period == "2026Q2", out_c[0].period)
    out_cg = rule_financial_indicator_caliber("000725.SZ", {}, cfg, fin_table, "G")
    check("工商业不出金融业口径说明", out_cg == [])


# ------------------------------------------------------------------ 5 分表代码

class _SplitCodeSource(EastMoneySource):
    """离线替身：还原"资产负债表在 .BJ、利润表与现金流量表只在 .NQ"的真实情形。

    830964 润农节水、830809 安达科技等北交所主体均属于这一类。只按资产负债表
    确定主体代码，会把一家确有披露的公司读成"没有利润表"，且系统不会报错——
    这正是最危险的一类静默空洞，所以它必须有回归用例守着。
    """

    DATA = {
        ("RPT_F10_FINANCE_GBALANCE", "830964.BJ"): 39,
        ("RPT_F10_FINANCE_GINCOME", "830964.NQ"): 48,
        ("RPT_F10_FINANCE_GCASHFLOW", "830964.NQ"): 48,
        # 主体画像同样只在 .NQ 上有：不回溯的话，报告会把代码当公司名、行业写成"未分类"
        ("RPT_F10_BASIC_ORGINFO", "830964.NQ"): 1,
    }

    def query(self, report, secucode, page_size: int = 60, sort: bool = True) -> dict:
        rows = self.DATA.get((report, secucode), 0)
        if not rows:
            return {"success": False, "message": "返回数据为空", "code": 9201, "result": None}
        record = {"REPORT_DATE": "2025-12-31", "SECUCODE": secucode,
                  "SECURITY_NAME_ABBR": "润农节水",
                  "ORG_NAME": "河北润农节水科技股份有限公司",
                  "EM2016": "建筑-建筑施工-专业工程", "PROVINCE": "河北",
                  "LISTING_DATE": "2014-08-08 00:00:00",
                  "TOTAL_OPERATE_INCOME": 1.0e8}
        return {"success": True, "result": {"data": [dict(record) for _ in range(min(rows, page_size))]}}

    def probe(self, report, secucode, sort: bool = True) -> str:
        return HIT if self.DATA.get((report, secucode)) else EMPTY


def test_statement_code_split() -> None:
    print("分表代码解析（北交所 .BJ / .NQ 数据分裂）")
    tmp = tempfile.mkdtemp(prefix="finagent_stmt_")
    try:
        src = _SplitCodeSource(tmp)
        check("主体代码仍按上市地取 .BJ", src.secucode("830964") == "830964.BJ",
              src.secucode("830964"))
        check("报表族仍为一般工商业", src.family("830964.BJ") == "G")

        # 核心断言：同一主体、不同报表可以落在不同代码上
        check("利润表回退到 .NQ", src.statement_code("income", "830964") == "830964.NQ",
              src.statement_code("income", "830964"))
        check("现金流量表回退到 .NQ", src.statement_code("cashflow", "830964") == "830964.NQ",
              src.statement_code("cashflow", "830964"))
        check("资产负债表仍走 .BJ", src.statement_code("balance", "830964") == "830964.BJ",
              src.statement_code("balance", "830964"))

        frames = src.frames("830964")
        check("三张表全部取到数据",
              all(len(frames[k]) > 0 for k in ("income", "cashflow", "balance")),
              str({k: len(frames[k]) for k in frames}))

        # 缓存文件名必须带上实际代码后缀，否则 .BJ 与 .NQ 两份响应会互相覆盖
        name = os.path.basename(src._cache_path_stmt("G", "income", "830964.NQ"))
        check("缓存键保留代码后缀", name == "G_income_830964.NQ.json", name)
        check("不同代码不共用缓存文件",
              src._cache_path_stmt("G", "income", "830964.NQ") !=
              src._cache_path_stmt("G", "income", "830964.BJ"))

        # 幂等：第二次解析直接命中缓存，结果不变
        check("二次解析结果稳定", src.statement_code("income", "830964") == "830964.NQ")

        # 主体画像也要回溯，否则报告标题会把代码当公司名、行业写成未分类
        info = src.profile("830964.BJ")
        check("主体画像回退到 .NQ", info.get("queried") == "830964.NQ", str(info.get("queried")))
        check("公司名解析正确", info.get("name") == "润农节水", str(info.get("name")))
        check("所属行业解析正确", info.get("industry") == "建筑-建筑施工-专业工程",
              str(info.get("industry")))

        # 主代码即命中时不得凭空改写（多数公司属于此情形）
        plain = _SplitCodeSource(tmp)
        plain.DATA = {("RPT_F10_FINANCE_GBALANCE", "600519.SH"): 5,
                      ("RPT_F10_FINANCE_GINCOME", "600519.SH"): 5,
                      ("RPT_F10_FINANCE_GCASHFLOW", "600519.SH"): 5}
        check("主代码命中则不改写", plain.statement_code("income", "600519") == "600519.SH",
              plain.statement_code("income", "600519"))
        # 离线名称兜底：名录缓存存在时，配置外的主体也应能拿到官方简称
        listing = Path("data/raw/_universe.json")
        if listing.exists():
            from finagent.datasource.universe import resolve as resolve_stock
            hit = resolve_stock("830964") or {}
            check("巨潮名录可离线兜底公司名（无需联网）", hit.get("name") == "润农节水",
                  str(hit.get("name")))
        else:
            print("  [跳过] 名录缓存不存在，跳过离线兜底用例（可运行 run.py search 生成）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    for fn in (test_codes, test_families, test_field_fallback,
               test_dep_amort_no_double_count, test_financial_caliber,
               test_statement_code_split):
        print("=" * 74)
        fn()

    total = len(RESULTS)
    failed = [n for n, ok in RESULTS if not ok]
    print("=" * 74)
    print(f"通过 {total - len(failed)} 项，失败 {len(failed)} 项")
    for name in failed:
        print("  失败：" + name)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
