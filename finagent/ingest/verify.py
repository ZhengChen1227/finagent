"""交叉校验闸门：用财报自身的三个独立口径互相印证。

存在的意义：抽取出错时，数字往往仍然"看起来正常"——
一个少了一位数的收入、一个错列的金额，都不会抛异常，
只会安安静静地流进报告。本模块是最后一道闸门，它不问"抽到了什么"，
只问"抽到的东西之间自相矛盾吗"。

用到的四个独立口径：

1. **报告自己披露的同比率**与系统自算的同比率。两者对不上，要么抽取错了，
   要么公司调了会计口径——两种都必须让读者知道，不能悄悄放过。
2. **会计恒等式**：资产 = 负债 + 所有者权益；
   所有者权益 = 归母权益 + 少数股东权益；净利润 = 归母 + 少数股东损益。
3. **利润表与资产负债表之间的桥**：利润总额 − 所得税费用 = 净利润。
4. **非经常性损益合计**与"归母净利润 − 扣非归母净利润"。这条最好用：
   它的两侧分别来自利润表和摘要表，任何一侧抽错都会立刻暴露。
5. **年报的「分季度主要财务指标」**四个季度之和，应当等于全年的累计数。

一条硬规则：**本校验只做判定，不改数字。** 判定不通过的科目会被标记为
"需人工复核"并随报告一起呈现；系统绝不会为了让校验通过而调整任何一个金额。
"""

from __future__ import annotations

# 同比率容差，单位百分点。报告披露值保留两位小数，
# 因此自算值与披露值的差在 0.005pp 内属正常四舍五入；这里放宽到 0.05pp，
# 既能容忍舍入，又足以抓住"两个数根本不是同一个口径"这类真问题。
TOL_YOY_PP = 0.05
# 恒等式的相对容差。金额以元计，允许 1e-9 的相对误差（浮点累加误差）。
TOL_REL = 1e-9
# 单位换算/取整可能带来的绝对容差（元）。财报金额都保留两位小数，
# 因此 1 元的差已属异常，但千分位四舍五入偶尔会留下 0.01 的尾差。
TOL_ABS = 0.05


def _close(a, b, tol_rel: float = TOL_REL, tol_abs: float = TOL_ABS) -> bool:
    if a is None or b is None:
        return False
    scale = max(abs(a), abs(b), 1.0)
    return abs(a - b) <= max(tol_abs, tol_rel * scale)


def _diff_pct(actual, expected):
    if actual is None or expected is None:
        return None
    scale = max(abs(expected), 1.0)
    return (actual - expected) / scale * 100.0


def yoy_of(cur, prev):
    """按报告披露的口径算同比率：分母取上期的绝对值。

    上期为负时，直接相除会把"亏损扩大"读成"增长"。
    证监会披露口径用绝对值作分母，本系统与之一致，
    这样自算值才能与报告披露值直接比对。
    """
    if cur is None or prev is None or prev == 0:
        return None
    return (cur - prev) / abs(prev) * 100.0


def _check(name, ok, detail, expected=None, actual=None, page=None,
           level=None, fields=()) -> dict:
    return {"name": name, "level": level or ("pass" if ok else "fail"),
            "ok": bool(ok), "detail": detail, "expected": expected,
            "actual": actual, "page": page, "fields": list(fields)}


def run(upload: dict) -> dict:
    """对一份材料执行全部交叉校验。

    入参是 assemble.build 的产物。返回 {checks, suspect, passed, failed}，
    其中 suspect 是"需要人工复核"的 canonical 科目 -> 原因。
    """
    checks, suspect = [], {}
    highlights = upload.get("highlights") or {}
    frames = upload.get("frames") or {}
    page_of = {e["field"] + "|" + e["period"]: e.get("page") for e in upload.get("evidence") or []}

    def note(field, reason, period="本期"):
        key = field
        suspect.setdefault(key, reason)

    # ---- 1) 自算同比 vs 报告披露同比 -------------------------------------
    # 科目 -> 需要一并降级的报表科目
    yoy_target = {
        "revenue": "revenue",
        "parent_net_profit": "parent_net_profit",
        "deduct_parent_net_profit": "deduct_parent_net_profit",
        "netcash_operate": "netcash_operate",
        "basic_eps": "basic_eps",
        "total_assets": "total_assets",
        "total_parent_equity": "total_parent_equity",
    }
    for key, item in (highlights.get("key_metrics") or {}).items():
        cur, prev, shown = item.get("cur"), item.get("prev"), item.get("yoy_pct")
        if shown is None:
            checks.append(_check(
                f"披露同比·{key}", True, "报告未披露同比率，跳过比对",
                level="skip", page=item.get("page")))
            continue
        mine = yoy_of(cur, prev)
        if mine is None:
            checks.append(_check(f"披露同比·{key}", True, "自算同比不可得，跳过比对",
                                 level="skip", page=item.get("page")))
            continue
        ok = abs(mine - shown) <= TOL_YOY_PP
        negative_base = prev is not None and prev < 0
        checks.append(_check(
            f"披露同比·{key}", ok,
            ("自算同比与报告披露一致" if ok else
             "自算同比与报告披露不一致，可能抽取错列或公司口径变化")
            + ("（上期为负，比率仅供参考）" if negative_base else ""),
            expected=shown, actual=round(mine, 4), page=item.get("page"),
            level="pass" if ok else ("warn" if negative_base else "fail"),
            fields=[yoy_target.get(key, key)]))
        if not ok and not negative_base:
            note(yoy_target.get(key, key),
                 f"自算同比 {mine:.2f}% 与报告披露 {shown:.2f}% 不一致（容差 {TOL_YOY_PP}pp）")

    # ---- 2) 会计恒等式 ---------------------------------------------------
    for label, ts, tag in _periods(upload):
        cur = _at(frames, "balance", ts)
        ta, tl, te = cur.get("total_assets"), cur.get("total_liabilities"), cur.get("total_equity")
        if None not in (ta, tl, te):
            ok = _close(ta, tl + te)
            checks.append(_check(f"资产=负债+权益（{label}）", ok,
                                 f"{ta:,.2f} vs {tl:,.2f}+{te:,.2f}",
                                 expected=ta, actual=tl + te,
                                 page=page_of.get("total_assets|" + tag),
                                 fields=["total_assets", "total_liabilities", "total_equity"]))
            if not ok:
                for f in ("total_assets", "total_liabilities", "total_equity"):
                    note(f, f"会计恒等式不成立（{label}）：资产 {ta:,.2f} ≠ 负债+权益 {tl + te:,.2f}")
        tpe, me = cur.get("total_parent_equity"), cur.get("minority_equity")
        if None not in (te, tpe, me):
            ok = _close(te, tpe + me)
            checks.append(_check(f"权益=归母+少数股东（{label}）", ok,
                                 f"{te:,.2f} vs {tpe:,.2f}+{me:,.2f}",
                                 expected=te, actual=tpe + me,
                                 fields=["total_equity", "total_parent_equity", "minority_equity"]))
            if not ok:
                note("minority_equity",
                     f"权益=归母+少数股东不成立（{label}）：{te:,.2f} ≠ {tpe + me:,.2f}")
        inc = _at(frames, "income", ts)
        np_, pnp, mi = inc.get("net_profit"), inc.get("parent_net_profit"), inc.get("minority_interest")
        if None not in (np_, pnp, mi):
            ok = _close(np_, pnp + mi)
            checks.append(_check(f"净利润=归母+少数股东损益（{label}）", ok,
                                 f"{np_:,.2f} vs {pnp:,.2f}+{mi:,.2f}",
                                 expected=np_, actual=pnp + mi,
                                 fields=["net_profit", "parent_net_profit", "minority_interest"]))
            if not ok:
                note("minority_interest",
                     f"净利润=归母+少数股东损益不成立（{label}）：{np_:,.2f} ≠ {pnp + mi:,.2f}")
        tp, tax = inc.get("total_profit"), inc.get("income_tax")
        if None not in (tp, tax, np_):
            ok = _close(tp - tax, np_)
            checks.append(_check(f"利润总额−所得税=净利润（{label}）", ok,
                                 f"{tp:,.2f}−{tax:,.2f}={tp - tax:,.2f} vs {np_:,.2f}",
                                 expected=np_, actual=tp - tax,
                                 fields=["total_profit", "income_tax", "net_profit"]))
            if not ok:
                note("income_tax",
                     f"利润总额−所得税费用 ≠ 净利润（{label}）：{tp - tax:,.2f} ≠ {np_:,.2f}")
        cf = _at(frames, "cashflow", ts)
        inflow, outflow, net = (cf.get("total_operate_inflow"),
                                cf.get("total_operate_outflow"),
                                cf.get("netcash_operate"))
        if None not in (inflow, outflow, net):
            # 现金流出的列示有两种惯例：多数公司写正数
            # （"经营活动现金流出小计 186,020,955,360"），也有公司用括号写成
            # 负数（实测京东方："(186,020,955,360)"），后者按"流入+流出"勾稽。
            # 两种都合规，判据必须认得出用的是哪一种，否则会把一份格式合规的
            # 报告误判成抽取错误——误判同样会误导读者。
            by_minus, by_plus = _close(inflow - outflow, net), _close(inflow + outflow, net)
            ok = by_minus or by_plus
            detail = f"{inflow:,.2f}−{outflow:,.2f}={inflow - outflow:,.2f} vs {net:,.2f}"
            if by_plus and not by_minus:
                detail += "（该表流出以负数列示：流入＋流出＝净额）"
            checks.append(_check(f"经营净额=流入−流出（{label}）", ok, detail,
                                 expected=net, actual=inflow - outflow,
                                 fields=["total_operate_inflow", "total_operate_outflow",
                                         "netcash_operate"]))
            if not ok:
                note("netcash_operate",
                     f"经营活动净额 ≠ 流入小计−流出小计（{label}）")

    # ---- 3) 非经常性损益：两份来源互证 ----------------------------------
    nr = highlights.get("nonrecurring") or {}
    km = highlights.get("key_metrics") or {}
    total = nr.get("total")
    pnp = (km.get("parent_net_profit") or {}).get("cur")
    dpnp = (km.get("deduct_parent_net_profit") or {}).get("cur")
    if None not in (total, pnp, dpnp):
        derived = pnp - dpnp
        ok = _close(total, derived, tol_abs=1.0)
        checks.append(_check("非经常性损益合计=归母−扣非", ok,
                             f"披露合计 {total:,.2f} vs 归母−扣非 {derived:,.2f}",
                             expected=derived, actual=total,
                             page=nr.get("page") or highlights.get("nonrecurring_total_page"),
                             fields=["deduct_parent_net_profit"]))
        if not ok:
            note("deduct_parent_net_profit",
                 f"非经常性损益合计 {total:,.2f} 与 归母−扣非 {derived:,.2f} 不一致")

    # ---- 4) 年报分季度表之和 = 全年累计 ----------------------------------
    quarterly = highlights.get("quarterly") or {}
    if quarterly:
        for key, item in quarterly.items():
            values = [v for v in (item.get("values") or []) if v is not None]
            base = (km.get(key) or {}).get("cur")
            if len(values) < 4 or base is None:
                continue
            ok = _close(sum(values), base, tol_abs=1.0)
            checks.append(_check(f"分季度之和·{key}", ok,
                                 f"四季合计 {sum(values):,.2f} vs 全年 {base:,.2f}",
                                 expected=base, actual=sum(values), page=item.get("page"),
                                 fields=[key]))
            if not ok:
                note(key, f"分季度主要财务指标四季之和 {sum(values):,.2f} 与全年 {base:,.2f} 不一致")

    failed = [c for c in checks if c["level"] == "fail"]
    return {"checks": checks, "suspect": suspect,
            "passed": sum(1 for c in checks if c["level"] == "pass"),
            "failed": len(failed),
            "warned": sum(1 for c in checks if c["level"] == "warn"),
            "skipped": sum(1 for c in checks if c["level"] == "skip")}


def _periods(upload: dict) -> list:
    """(标签, 报告期, 标记) 三元组：本期在前，其余各期在后。

    比较期直接从 frames 里取，而不是只读 subject 上的那一个日期：
    资产负债表的比较期是上年年末，利润表的是上年同期，半年报与季报上两者
    并不相同（2026-06-30 对 2025-12-31 与 2025-06-30）。只认一个日期，
    另一个报表的恒等式校验就会整段跳过——而"跳过"在外观上与"通过"一样安静。
    """
    import pandas as pd

    frames = upload.get("frames") or {}
    dates = set()
    for report in ("income", "cashflow", "balance"):
        df = frames.get(report)
        if df is None or getattr(df, "empty", True) or "report_date" not in df.columns:
            continue
        dates.update(pd.to_datetime(df["report_date"]).tolist())
    subject = upload.get("subject") or {}
    if subject.get("report_date"):
        dates.add(pd.Timestamp(subject["report_date"]))
    if not dates:
        return []
    current = max(dates)
    out = [("本期", current, "本期")]
    for date in sorted(dates):
        if date != current:
            out.append(("比较期", date, "上期"))
    return out


def _at(frames: dict, report: str, ts) -> dict:
    """取某报表某报告期的一行，转为普通 dict。"""
    import pandas as pd

    df = frames.get(report)
    if df is None or df.empty or "report_date" not in df.columns:
        return {}
    match = df[df["report_date"] == pd.Timestamp(ts)]
    if match.empty:
        return {}
    return {k: v for k, v in match.iloc[0].items() if k != "report_date" and v == v}


def single_quarter_check(uploads: list) -> list:
    """跨材料的单季还原校验：年报「分季度主要财务指标」四季之和 = 全年累计。

    这是季报与年报之间唯一可交叉验证的口径，也是"环比"是否算对的关键：
    若两处对不上，"单季还原"这条路就走不通，环比只能标为不可计算。
    返回的结构与其他校验一致，可直接并入闸门结论。
    """
    from finagent.datasource import schema

    out = []
    for up in uploads:
        subject = up.get("subject") or {}
        if subject.get("report_kind") != "annual":
            continue
        table = up.get("table")
        quarterly = (up.get("highlights") or {}).get("quarterly") or {}
        if table is None or getattr(table, "empty", True) or not quarterly:
            continue
        for key, item in quarterly.items():
            values = [v for v in (item.get("values") or []) if v is not None]
            if key not in table.columns or len(values) < 4:
                continue
            series = table[key].dropna()
            if series.empty:
                continue
            annual = float(series.iloc[-1])
            quarters = [values[0], values[1], values[2], values[3]]
            ok = _close(sum(quarters), annual, tol_abs=1.0)
            spec = schema.FIELD_MAP.get(key)
            label = spec.label if spec else key
            out.append(_check(
                f"单季还原·{label}", ok,
                f"分季度四季合计 {sum(quarters):,.2f} vs 全年累计 {annual:,.2f}",
                expected=annual, actual=sum(quarters),
                page=item.get("page"), fields=[key]))
    return out
