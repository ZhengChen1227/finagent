"""上传财报抽取层测试：不需要 PDF，也不需要联网。

存在的意义：抽取层最危险的失败方式是"静默错值"——数字看着正常，含义却完全
错了。实测中踩到过的坑包括：把附注号当成金额（京东方老版年报"附注五 46"）、
把上期数当成本期数、单位差一万倍（万元列示的报表）、科目名折行导致整条读不
出来（"归属于上市公司股东的净利／润"）。这些错误都不会抛异常，只会静静变成
报告里的一个错误结论。

因此这里用**手工排版的文本片段**把每一种坑逐条钉死。片段的行文与列位置照抄
真实报告（牧原 2026 中报、京东方老版年报）的版面，只把金额换成整数以便心算
核对——这也让本测试在任何一台机器上都能跑，不需要先下载 20MB 的 PDF。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from finagent.datasource import schema
from finagent.ingest import assemble, docmeta, highlights, layout, statements, verify

RESULTS = []


def check(title: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((title, bool(ok)))
    print(f"  [{'通过' if ok else '失败'}] {title}" + (f"  {detail}" if detail else ""))
    return bool(ok)


# --------------------------------------------------------------------- 版面工具


def row(label: str, *values: str, col: int = 92, gap: int = 30) -> str:
    """排一行"科目名 + 右对齐的两列金额"，模拟 layout 模式的行文本。

    右对齐是关键：列检测按数值单元的**右边界**聚类，真实报表也是这么排的。
    """
    line = "  " + label
    pos = col
    for value in values:
        if value is None:
            continue
        line += " " * max(1, pos - len(line) - len(value)) + value
        pos += gap
    return line


def page(*lines: str) -> str:
    return "\n".join(lines)


def footer(n: int) -> str:
    return " " * 86 + str(n)


def mini_report(unit: str = "元", restated: bool = False,
                break_balance: bool = False) -> list:
    """一份可心算核对的迷你半年报。

    数字之间满足全部勾稽关系（资产=负债+权益、净利润=归母+少数股东损益、
    经营净额=流入−流出、非经常性损益合计=归母−扣非），因此校验闸门应当
    全部通过；把其中任一处改坏，就能验证闸门确实会拦下来。
    """
    scale = {"元": "元", "万元": "万元"}[unit]
    tick, box = "■", "□"
    rest_line = (f"{tick}是 {box}否" if restated else f"{box}是 {tick}否")
    pages = [
        # 0 封面
        page("证券代码：002714          证券简称：牧原股份",
             "牧原食品股份有限公司 2026 年半年度报告",
             "本报告期自 2026 年 1 月 1 日起至 2026 年 6 月 30 日止"),
        # 1 主要会计数据和财务指标
        page("四、主要会计数据和财务指标",
             "",
             "公司是否需追溯调整或重述以前年度会计数据",
             rest_line,
             row("", "本报告期", "上年同期", "本报告期比上年同期增减", col=78, gap=30),
             row("营业收入（元）", "100,000,000.00", "80,000,000.00", "25.00%"),
             row("归属于上市公司股东的净利", "7,200,000.00", "2,800,000.00", "157.14%"),
             "润（元）",
             row("归属于上市公司股东的扣除",
                 "6,000,000.00", "2,500,000.00", "140.00%"),
             "非经常性损益的净利润（元）",
             row("经营活动产生的现金流量净", "-5,000,000.00", "10,000,000.00", "-150.00%"),
             "额（元）",
             row("总资产（元）", "200,000,000.00", "180,000,000.00", "11.11%"),
             footer(7)),
        # 2 非经常性损益
        page("六、非经常性损益项目及金额",
             "适用 □不适用",
             "                                       单位：元",
             row("项目", "金额", "说明", col=64, gap=30),
             row("非流动性资产处置损益（包括已计提", "1,500,000.00"),
             "资产减值准备的冲销部分）",
             "计入当期损益的政府补助",
             row("", "200,000.00"),
             row("除上述各项之外的其他营业外收入和", "-100,000.00"),
             "支出",
             row("减：所得税影响额", "400,000.00"),
             row("合计", "1,200,000.00"),
             footer(8)),
        # 3 合并资产负债表（上）
        page("1、合并资产负债表",
             "编制单位：牧原食品股份有限公司",
             "                                   2026年06月30日",
             f"                                                   单位：{scale}",
             row("项目", "期末余额", "期初余额"),
             row("流动资产：", None, None),
             row("货币资金", "30,000,000.00", "20,000,000.00"),
             row("存货", "20,000,000.00", "15,000,000.00"),
             row("流动资产合计", "50,000,000.00", "35,000,000.00"),
             row("固定资产", "150,000,000.00", "145,000,000.00"),
             footer(9)),
        # 4 合并资产负债表（下）：跨页重复表头，表头行不能变成数据行
        page("1、合并资产负债表（续）",
             row("项目", "期末余额", "期初余额"),
             row("资产总计", "200,000,000.00", "180,000,000.00"),
             row("短期借款", "40,000,000.00", "30,000,000.00"),
             row("流动负债合计", "60,000,000.00", "50,000,000.00"),
             row("负债合计", "120,000,000.00", "110,000,000.00"),
             row("少数股东权益", "4,000,000.00", "4,000,000.00"),
             row("归属于母公司所有者权益合计", "76,000,000.00", "66,000,000.00"),
             row("所有者权益合计", "80,000,000.00", "70,000,000.00"),
             footer(10)),
        # 5 合并利润表：科目名折行（tail 拼接）
        page("2、合并利润表",
             f"                                                            单位：{scale}",
             row("项目", "本期发生额", "上期发生额"),
             row("一、营业总收入", "100,000,000.00", "80,000,000.00"),
             row("二、营业总成本", "90,000,000.00", "76,000,000.00"),
             row("其中：营业成本", "60,000,000.00", "50,000,000.00"),
             row("三、营业利润", "10,000,000.00", "4,000,000.00"),
             row("四、利润总额", "10,000,000.00", "4,000,000.00"),
             row("减：所得税费用", "2,500,000.00", "1,000,000.00"),
             row("五、净利润", "7,500,000.00", "3,000,000.00"),
             row("归属于母公司股东的净利润", "7,200,000.00", "2,800,000.00"),
             row("少数股东损益", "300,000.00", "200,000.00"),
             footer(11)),
        # 6 合并现金流量表：负号用 U+2212，长科目名折行
        page("3、合并现金流量表",
             f"                                                            单位：{scale}",
             row("项目", "本期发生额", "上期发生额"),
             row("销售商品、提供劳务收到的现金", "88,000,000.00", "60,000,000.00"),
             row("收到的税费返还", "2,000,000.00", "500,000.00"),
             row("经营活动现金流入小计", "90,000,000.00", "70,000,000.00"),
             row("购买商品、接受劳务支付的现金", "80,000,000.00", "55,000,000.00"),
             row("购建固定资产、无形资产和其他长期资产支付", "30,000,000.00", "25,000,000.00"),
             "的现金",
             row("经营活动现金流出小计", "95,000,000.00", "60,000,000.00"),
             row("经营活动产生的现金流量净额", "\u22125,000,000.00", "10,000,000.00"),
             footer(12)),
        # 7 现金流量表补充资料（间接法调节项）
        page("补充资料",
             f"                                                    单位：{scale}",
             row("项目", "本期金额", "上期金额"),
             row("固定资产折旧、油气资产折耗、生产性生物资产折旧",
                 "8,000,000.00", "7,000,000.00"),
             row("无形资产摊销", "1,000,000.00", "900,000.00"),
             row("存货的减少", "-3,000,000.00", "-2,000,000.00"),
             row("经营性应收项目的减少", "-1,000,000.00", "-500,000.00"),
             row("经营性应付项目的增加", "2,000,000.00", "1,500,000.00"),
             footer(13)),
    ]
    if break_balance:
        # 把"负债合计"改成与"资产−权益"差 1 元的数，用于验证校验闸门会拦下来
        pages[4] = pages[4].replace("120,000,000.00", "120,000,001.00")
    return pages


# ------------------------------------------------------------------ 1 数值解析


def test_numbers():
    cases = {
        "1,234.56": 1234.56,
        "\u22121,234.56": -1234.56,      # U+2212 真负号
        "\u20131,234.56": -1234.56,      # en dash
        "\u20141,234.56": -1234.56,      # em dash
        "\uff0d1,234.56": -1234.56,      # 全角连字符
        "（1,234.56）": -1234.56,         # 全角括号表负
        "(1,234.56)": -1234.56,          # 半角括号表负
        "-22.30%": -22.3,                # 同比列带百分号
        "0.00": 0.0,
        "—": None,                       # 纯破折号是"没有数"，不是 0
        "不适用": None,
    }
    bad = {k: layout.to_float(k) for k, want in cases.items() if layout.to_float(k) != want}
    check("数值解析（负号变体／括号／百分号／破折号）", not bad, str(bad))


def test_split_numbers():
    stuck = "90,703,260,964.4889,389,354,416.84"
    parts = layout.split_numbers(stuck)
    ok = len(parts) == 2 and layout.to_float(parts[0]) == 90703260964.48
    check("粘连数字拆分（列宽被填满时两个数会连在一起）", ok, str(parts))
    # 三位小数的普通数字不能被拆开
    check("普通小数不被误拆", layout.split_numbers("1.234") == ["1.234"])


def test_note_column():
    note = [46, 47, 48, 49]
    money = [204590222888, 180000000000, 12345678, 999999]
    check("附注号列识别（小整数）", layout.is_note_column(note))
    check("金额列不误判为附注号", not layout.is_note_column(money))
    check("空列不误判", not layout.is_note_column([None, None]))


def test_note_column_dropped():
    """京东方老版年报的利润表：金额左侧并列一列"附注五 46"，必须被剔除。"""
    lines = layout.iter_lines([page(
        row("项目", "附注五", "本期发生额", "上期发生额", col=40, gap=30),
        row("一、营业总收入", "46", "204,590,222,888.00", "180,000,000,000.00",
            col=40, gap=30),
        row("二、营业总成本", "47", "180,000,000,000.00", "170,000,000,000.00",
            col=40, gap=30),
        row("三、营业利润", "48", "24,590,222,888.00", "10,000,000,000.00",
            col=40, gap=30),
        row("四、利润总额", "49", "24,590,222,888.00", "10,000,000,000.00",
            col=40, gap=30),
    )])
    cols = layout.detect_columns(lines, 2, drop_note=True)
    rows = {r["label"].strip(): r["values"] for r in layout.parse_rows(lines, cols)}
    got = rows.get("一、营业总收入")
    ok = got is not None and got[0] == 204590222888.0 and got[1] == 180000000000.0
    check("附注号列被剔除，本期／上期没有被挤位", ok, str(got))


# ------------------------------------------------------------------ 2 文本识别


def test_docmeta():
    pages = mini_report()
    doc = docmeta.parse(pages, "牧原2026半年报.pdf")
    ok = (doc["code"] == "002714" and doc["short_name"] == "牧原股份"
          and doc["report_kind"] == "semi"
          and doc["report_date"] == "2026-06-30"
          and doc["report_date_source"] == "原文报告期区间")
    check("封面主体识别（代码／简称／报告类型／报告期）", ok, str(
        {k: doc[k] for k in ("code", "short_name", "report_kind", "report_date",
                             "report_date_source")}))
    # A+B 股并列时取 A 股：京东方B 是 200725，不能把主体认成 B 股
    ab = docmeta.parse([page("股票代码：000725、200725   股票简称：京东方A，京东方B",
                             "京东方科技集团股份有限公司 2026 年半年度报告")], "x.pdf")
    check("A+B 股取 A 股代码", ab["code"] == "000725" and ab["short_name"] == "京东方A",
          str(ab["codes"]))
    # 全角冒号与"公司代码"
    fw = docmeta.parse([page("公司代码：６００５１９　　公司简称：贵州茅台",
                             "贵州茅台酒股份有限公司 2025 年年度报告")], "y.pdf")
    check("全角冒号＋公司代码", fw["code"] == "600519" and fw["report_kind"] == "annual",
          str(fw["code"]))


def test_financial_synonyms():
    cases = {
        "营业支出": "total_operate_cost",          # 银行／保险的利润表
        "现金及存放中央银行款项": "monetaryfunds",   # 银行的"货币资金"
        "资产总计": "total_assets",
        "负债合计": "total_liabilities",
        "所有者权益合计": "total_equity",
    }
    bad = {k: schema.match(k) for k, want in cases.items() if schema.match(k) != want}
    check("金融业科目同义词（银行／保险／证券）", not bad, str(bad))


def test_label_wrap():
    """科目名折行：label 在本行、续行在下一行，必须能拼回完整科目名。"""
    lines = layout.iter_lines([page(
        row("归属于母公司股东的净利", "7,200,000.00", "2,800,000.00"),
        "润",
    )])
    rows = layout.parse_rows(lines, layout.detect_columns(lines, 2))
    key = None
    for r in rows:
        hit = schema.match_any(layout.label_candidates(r))
        if hit:
            key = hit
    check("折行科目名拼接（归属于母公司股东的净利／润）", key == "parent_net_profit",
          str(key))


# ------------------------------------------------------------------ 3 三表抽取


def test_statements():
    pages = mini_report()
    st = statements.extract(pages)
    bal, inc, cf = st["fields"]["balance"], st["fields"]["income"], st["fields"]["cashflow"]
    checks = [
        ("资产负债表跨页抽取（货币资金）", bal.get("monetaryfunds", {}).get("cur"), 30000000.0),
        ("资产负债表跨页抽取（资产总计）", bal.get("total_assets", {}).get("cur"), 200000000.0),
        ("会计要素（负债合计）", bal.get("total_liabilities", {}).get("cur"), 120000000.0),
        ("利润表折行科目（营业总收入）", inc.get("revenue", {}).get("cur"), 100000000.0),
        ("利润表归母净利润", inc.get("parent_net_profit", {}).get("cur"), 7200000.0),
        ("现金流量表补充资料（固定资产折旧）", cf.get("depr_fa", {}).get("cur"), 8000000.0),
        ("现金流量表补充资料（存货的减少）", cf.get("inventory_reduce", {}).get("cur"), -3000000.0),
    ]
    for title, got, want in checks:
        check(title, got == want, f"{got!r} 期望 {want!r}")
    # 折行的"购建固定资产…支付的现金"
    check("现金流出长科目名折行",
          cf.get("construct_long_asset", {}).get("cur") == 30000000.0,
          str(cf.get("construct_long_asset")))
    # U+2212 负号
    check("现金流量净额负号（U+2212）识别",
          cf.get("netcash_operate", {}).get("cur") == -5000000.0,
          str(cf.get("netcash_operate")))
    # 页眉与页码不能变成数据行
    check("页脚页码未污染抽取结果",
          all(str(v).strip() not in ("", "None") for v in
              [bal.get("total_assets", {}).get("cur")]))
    return st


def test_units():
    st = statements.extract(mini_report(unit="万元"))
    check("单位识别（万元）", st["units"].get("balance") == 1e4,
          str(st["units"]))
    doc = docmeta.parse(mini_report(unit="万元"), "x.pdf")
    built = assemble.build(doc, st, highlights.extract(mini_report(unit="万元")))
    df = built["frames"]["balance"]
    row0 = df[df["report_date"] == "2026-06-30"].iloc[0]
    check("万元单位换算（20000 万元 = 2 亿元）",
          row0["total_assets"] == 200000000.0 * 1e4, str(row0["total_assets"]))
    check("元单位不换算", statements.extract(mini_report())["units"]["balance"] == 1.0)


def test_highlights():
    pages = mini_report(restated=False)
    hl = highlights.extract(pages)
    km = hl["key_metrics"]
    check("主要会计数据（折行科目）",
          (km.get("parent_net_profit") or {}).get("cur") == 7200000.0, str(km.get("parent_net_profit")))
    check("披露同比被读成数值而非文本",
          (km.get("revenue") or {}).get("yoy_pct") == 25.0, str(km.get("revenue")))
    check("扣非归母净利润",
          (km.get("deduct_parent_net_profit") or {}).get("cur") == 6000000.0)
    nr = hl["nonrecurring"] or {}
    check("非经常性损益合计", nr.get("total") == 1200000.0, str(nr.get("total")))
    check("非经常性损益明细条数", len(nr.get("items") or []) >= 3,
          str([i["label"] for i in nr.get("items") or []]))
    check("追溯调整勾选框（明确勾了“否”）", hl["restated"]["value"] is False,
          str(hl["restated"]["value"]))
    check("追溯调整勾选框（勾选“是”）",
          highlights.extract(mini_report(restated=True))["restated"]["value"] is True)


# ------------------------------------------------------------------ 4 装配与校验


def test_assemble_and_verify():
    pages = mini_report()
    doc = docmeta.parse(pages, "牧原2026半年报.pdf")
    st = statements.extract(pages)
    st["missing"] = statements.missing(pages, st)
    built = assemble.build(doc, st, highlights.extract(pages), "mini.pdf")
    result = verify.run(built)
    failed = [c for c in result["checks"] if c["level"] == "fail"]
    check("校验闸门全部通过（勾稽关系自洽的报表）", result["failed"] == 0,
          str([c["name"] for c in failed]))
    check("校验项覆盖披露同比／恒等式／非经常性损益",
          result["passed"] >= 8, f"通过 {result['passed']} 项")
    check("可疑科目为空", not result["suspect"], str(result["suspect"]))
    inc = built["frames"]["income"]
    got = inc[inc["report_date"] == "2026-06-30"].iloc[0]
    check("装配后的 frames 契约（本期行）",
          got["revenue"] == 100000000.0 and got["netcash_operate"] == 0
          if "netcash_operate" in inc.columns else got["revenue"] == 100000000.0)
    check("利润表比较期（上年同期）落在 2025-06-30",
          "2025-06-30" in [str(d.date()) for d in inc["report_date"]],
          str([str(d.date()) for d in inc["report_date"]]))
    check("资产负债表比较期是上年年末（2025-12-31）",
          "2025-12-31" in [str(d.date()) for d in built["frames"]["balance"]["report_date"]],
          str([str(d.date()) for d in built["frames"]["balance"]["report_date"]]))
    # 证据链：每个抽到的数字都要能指回文件名与页码
    ev = [e for e in built["evidence"] if e["field"] == "total_assets"]
    # 页码必须是 1 基的真实页号（资产总计排在合成材料的第 5 页），
    # 不能是行号，否则"翻回第几页核对"这句话就是假的。
    check("证据带页码与文件名",
          bool(ev) and ev[0]["page"] == 5 and ev[0]["source_pdf"] == "mini.pdf",
          str(ev[:1]))


def test_gate_blocks_bad_numbers():
    """关键验证：把"负债合计"改坏 1 元，闸门必须拦下来并标为需人工复核。"""
    pages = mini_report(break_balance=True)
    doc = docmeta.parse(pages, "x.pdf")
    st = statements.extract(pages)
    built = assemble.build(doc, st, highlights.extract(pages), "bad.pdf")
    result = verify.run(built)
    names = [c["name"] for c in result["checks"] if c["level"] == "fail"]
    check("资产≠负债+权益被拦下", any("资产=负债+权益" in n for n in names), str(names))
    check("对应科目降级为“需人工复核”",
          {"total_assets", "total_liabilities", "total_equity"} & set(result["suspect"]),
          str(sorted(result["suspect"])))


def test_glued_year_table():
    """三年并列表 + 数字粘连：同比列必须读成 3.13%，而不是 2023 年的收入。

    这是京东方 2025 年报"主要会计数据和财务指标"表的真实版面：
    ``204,590,222,888.00198,380,605,661.003.13%174,543,445,895.00``，
    四列分别是 2025／2024／增减／2023，数字之间没有空格。
    """
    pages = [page("四、主要会计数据和财务指标",
                  row("项目", "2025年", "2024年", "本年比上年", "2023年",
                      col=76, gap=22),
                  "增减",
                  row("营业收入（元）",
                      "204,590,222,888.00198,380,605,661.003.13%174,543,445,895.00"),
                  row("归属于上市公司股东的净利润（元）",
                      "5,856,966,754.005,323,248,974.0010.03%  2,547,435,360.00"),
                  row("总资产（元）",
                      "436,378,322,803.00429,978,221,541.001.49%419,187,099,795.00"))]
    km = highlights.extract(pages)["key_metrics"]
    rev = km.get("revenue") or {}
    check("粘连的三年并列表：本期与上年同期读对",
          rev.get("cur") == 204590222888.0 and rev.get("prev") == 198380605661.0,
          str(rev))
    check("粘连的三年并列表：同比列读成 3.13% 而不是 2023 年收入",
          rev.get("yoy_pct") == 3.13, str(rev.get("yoy_pct")))
    check("其后的行同样对位",
          (km.get("total_assets") or {}).get("yoy_pct") == 1.49,
          str(km.get("total_assets")))


def test_nonrecurring_stops_at_total():
    """非经常性损益表的合计不能从后面那张表里取。

    实测茅台 2025 年报：非经常性损益表之后紧跟"采用公允价值计量的项目"，
    区域按页数封顶时把后者的"合计"当成了非经常性损益合计，
    读出一个与"归母−扣非"差两个数量级的数。
    """
    pages = [
        page("十、非经常性损益项目和金额",
             "适用 □不适用",
             row("非经常性损益项目", "2025年金额", "2024年金额", col=84, gap=30),
             row("非流动性资产处置损益（包括已计提资产减值准备的冲销部分）",
                 "-2,384,586.28", "-6,898,481.82"),
             row("除上述各项之外的其他营业外收入和支出",
                 "-48,197,347.69", "-42,713,924.90"),
             row("合计", "26,959,446.43", "-12,759,555.80")),
        page("十二、采用公允价值计量的项目",
             row("项目名称", "期初余额", "期末余额", "当期变动", col=66, gap=24),
             row("交易性金融资产", "248,513,280.00", "-248,513,280.00", "265,351.99"),
             row("合计", "4,277,492,275.56", "1,000,000,000.00", "2,000,000,000.00")),
    ]
    nr = (highlights.extract(pages)["nonrecurring"] or {})
    check("非经常性损益合计取自本表的合计行", nr.get("total") == 26959446.43,
          str(nr.get("total")))
    check("合计行之后的行不再计入本表明细",
          len(nr.get("items") or []) == 2, str(len(nr.get("items") or [])))


def main() -> int:
    print("=" * 74)
    print("上传财报抽取层测试（合成版面，不需要 PDF）")
    print("=" * 74)
    test_numbers()
    test_split_numbers()
    test_note_column()
    test_note_column_dropped()
    test_docmeta()
    test_financial_synonyms()
    test_label_wrap()
    test_statements()
    test_units()
    test_highlights()
    test_assemble_and_verify()
    test_gate_blocks_bad_numbers()
    test_glued_year_table()
    test_nonrecurring_stops_at_total()
    bad = [t for t, ok in RESULTS if not ok]
    print("-" * 74)
    print(f"抽取层测试：{len(RESULTS) - len(bad)}/{len(RESULTS)} 项通过")
    for title in bad:
        print("  未通过：" + title)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
