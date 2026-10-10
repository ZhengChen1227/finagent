"""摘要层抽取：主要会计数据、非经常性损益、分季度指标、追溯调整标记。

存在的意义：这四个部分是赛题点名的考点，而且**只有这里才有**：

- 扣非归母净利润不在合并利润表里，只在「主要会计数据和财务指标」披露；
  没有它就无法识别非经常性损益对利润的影响。
- 「非经常性损益项目及金额」把损益拆成逐条明细，是判断"利润是否可持续"
  的一手材料，比任何推算都可靠。
- 年报的「分季度主要财务指标」给出四个季度的单季数，
  因此**只上传一份年报**也能算出环比；缺了它，单季还原只能靠猜。
- 报告披露的同比增减率是免费的校验基准：系统自算的同比若与它不符，
  要么抽取错了，要么公司的会计口径变了——两种都必须让读者知道。

这里坚持一条底线：**抽不到就报缺失**。
例如追溯调整勾选框在 PDF 里常因字体缺字而丢标记，
此时只能如实回报"原文为『□是 否』，未能判定"，绝不代为判断。
"""

from __future__ import annotations

import re

from finagent.ingest import layout

# 摘要区锚点。按文档顺序切分区间：每个锚点的区域终止于下一个锚点。
MARKERS = (
    ("key_metrics", ("主要会计数据和财务指标",)),
    # 同一张表有两种标题：证监会格式写"分季度主要财务指标"，
    # 上交所部分公司（如贵州茅台）写"分季度主要财务数据"。
    ("quarterly", ("分季度主要财务指标", "分季度主要财务数据")),
    ("nonrecurring", ("非经常性损益项目及金额", "非经常性损益项目")),
)

# 每个区域最多向下延伸几页。上限是必须的：位于报告末尾的
# 「非经常性损益」之后没有任何锚点，不封顶就会一路扫到全文结束，
# 把后续所有含数字的表格都当成非经常性损益明细（实测会产出两千多条垃圾）。
REGION_CAP = {"key_metrics": 2, "quarterly": 2, "nonrecurring": 2}

# 主要会计数据里的科目名与合并报表略有不同（"上市公司股东"vs"母公司股东"、
# "总资产"vs"资产总计"），因此单独维护一份映射，而不是复用主表同义词表——
# 复用会让"总资产"这种只在该表出现的写法永远匹配不上。
KEY_METRICS = {
    "revenue": ("营业收入", "营业总收入"),
    "parent_net_profit": ("归属于上市公司股东的净利润", "归属于母公司股东的净利润",
                          "归属于母公司所有者的净利润"),
    "deduct_parent_net_profit": (
        "归属于上市公司股东的扣除非经常性损益的净利润",
        "归属于上市公司股东的扣除非经常性损益后净利润",
        "扣除非经常性损益后归属于上市公司股东的净利润",
        "扣除非经常性损益后归属于母公司股东的净利润",
    ),
    "netcash_operate": ("经营活动产生的现金流量净额",),
    "basic_eps": ("基本每股收益",),
    "total_assets": ("总资产", "资产总计"),
    "total_parent_equity": ("归属于上市公司股东的净资产", "归属于上市公司股东的股东权益",
                            "归属于上市公司股东的所有者权益",
                            "归属于母公司所有者权益合计"),
}

QUARTERLY_ORDER = ("revenue", "parent_net_profit", "deduct_parent_net_profit",
                   "netcash_operate")

# 非经常性损益的标准项目，抄自证监会《公开发行证券的公司信息披露解释性公告
# 第1号——非经常性损益》。用它把每一行归到标准项目上，理由有二：
#
# 1. 明细行的科目名在 PDF 里常被折成两三行，靠拼接还原出来的文字时对时错；
#    而归到标准项目后，报告里写出的名称始终规范、可比、可核对。
# 2. 非经常性损益区间的末尾没有下一个锚点，靠页数封顶仍会扫进后文的叙述段落。
#    标准项目表天然是一道闸门：对不上任何标准项目的行直接丢弃，
#    而不是把一段"优质五花肉"的分割描述当成一笔收益记进报告。
#
# 每个标准项目给出若干关键短语，按标准顺序匹配，首个命中即采用。
STANDARD_NONRECURRING = (
    ("非流动性资产处置损益（包括已计提资产减值准备的冲销部分）",
     ("非流动性资产处置损益", "非流动资产处置损益")),
    ("越权审批或无正式批准文件的税收返还、减免", ("越权审批",)),
    ("计入当期损益的政府补助", ("计入当期损益的政府补助", "当期损益的政府补助",
                                 "政府补助计入当期损益")),
    ("计入当期损益的对非金融企业收取的资金占用费", ("资金占用费",)),
    ("企业取得子公司、联营企业及合营企业的投资成本小于取得投资时应享有"
     "被投资单位可辨认净资产公允价值产生的收益", ("投资成本小于取得投资",)),
    ("非货币性资产交换损益", ("非货币性资产交换",)),
    ("委托他人投资或管理资产的损益", ("委托他人投资或管理资产",)),
    ("因不可抗力因素而计提的各项资产减值准备", ("不可抗力", "自然灾害")),
    ("债务重组损益", ("债务重组",)),
    ("企业重组费用", ("企业重组费用", "重组费用")),
    ("交易价格显失公允的交易产生的超过公允价值部分的损益", ("显失公允",)),
    ("同一控制下企业合并产生的子公司期初至合并日的当期净损益",
     ("同一控制下企业合并",)),
    ("与公司正常经营业务无关的或有事项产生的损益", ("或有事项",)),
    ("除同公司正常经营业务相关的有效套期保值业务外的持有与处置损益",
     ("套期保值",)),
    ("单独进行减值测试的应收款项减值准备转回",
     ("减值测试的应收款项", "应收款项减值准备转回")),
    ("对外委托贷款取得的损益", ("对外委托贷款", "委托贷款")),
    ("采用公允价值模式进行后续计量的投资性房地产公允价值变动产生的损益",
     ("投资性房地产公允价值变动",)),
    ("根据税收、会计等法律、法规的要求对当期损益进行一次性调整对当期损益的影响",
     ("法律法规", "法律、法规")),
    ("受托经营取得的托管费收入", ("受托经营", "托管费收入")),
    ("除上述各项之外的其他营业外收入和支出", ("除上述各项之外",)),
    ("其他符合非经常性损益定义的损益项目", ("其他符合非经常性损益定义",)),
    ("减：所得税影响额", ("所得税影响额",)),
    ("少数股东权益影响额（税后）", ("少数股东权益影响额",)),
)

# 「合计」行的识别：真正的合计行只有"合计"两个字，后面跟着金额。
# 有些公司的表述会把下一段小标题并到同一行（"合计其他符合非经常性损益定义
# 的损益项目的具体情况："），那种行不是合计行，不能把它的金额当成合计。
_TOTAL_FLAT = "合计"
# 叙述性小标题（不是明细行）。它们与明细表共用版面区间，必须排除，
# 否则一段经营描述会被当成一笔非经常性损益。
_NARRATIVE = ("具体情况", "情况说明", "适用", "不适用")


def flat(text: str) -> str:
    """去掉全部空白，用于关键短语匹配（不受折行与对齐空格影响）。"""
    return re.sub(r"[\s\u3000]+", "", text or "")


def nonrecurring_key(text: str):
    """把一行非经常性损益明细归到标准项目；对不上任何标准项目则返回 None。"""
    body = flat(text)
    if not body or any(word in body for word in _NARRATIVE):
        return None
    if body.rstrip("：:") == _TOTAL_FLAT:
        return _TOTAL_FLAT
    for label, phrases in STANDARD_NONRECURRING:
        if any(phrase in body for phrase in phrases):
            return label
    return None

_HEADER_JUNK = {"项目", "说明", "附注", "注", "注释"}
# 表头列标签的形态。年报用"2025年／2024年／本年比上年增减"这种写法，
# 半年报用"本报告期／上年同期／…增减"，季度表用"第一季度…第四季度"，
# 三者没有公共关键词，因此只能逐个形态匹配，而不是找固定词。
_LABEL_TOKEN = re.compile(r"\d{4}\s*年|金额|报告期|同期|期末|季度|增减|发生额|年度末")

# 勾选标记。PDF 里"已勾选"的方框常用 Wingdings 等字体的私有区字符表示，
# 抽取后落在 U+E000–U+F8FF；而"未勾选"是普通的 □。
_CHECKED = re.compile(r"[\u2713\u2714\u221a\u2611\u25a0\u25a3\u25cf\u25aa\ue000-\uf8ff]")
_EMPTY_BOX = "\u25a1"


def _normalize(label: str) -> str:
    text = re.sub(r"[\s\u3000]+", "", label or "")
    text = re.sub(r"^[（(]?[一二三四五六七八九十0-9]+[）)]?[、.]?", "", text)
    text = re.sub(r"[（(][^（()）]*[）)]?", "", text)
    return text.strip()


_KEY_INDEX = {}
for _key, _names in KEY_METRICS.items():
    for _name in _names:
        _KEY_INDEX[_name] = _key


def key_of(candidates) -> "str | None":
    """按候选写法依次比对，返回首个命中的主要会计数据科目。"""
    for text in candidates:
        hit = _KEY_INDEX.get(_normalize(text))
        if hit:
            return hit
    return None


_PCT_TOKEN = re.compile(r"[-\u2212\u2013\u2014\uff0d]?[\d,]+(?:\.\d+)?%")


def _percent_index(rows: list) -> "int | None":
    """定位"同比增减"那一列：它是整张表里唯一带百分号的列。

    只数"这一行的第几个数字带百分号"，**不依赖列位置**：数字被版面粘成
    一团时，拆出来的偏移量只是推算值（见 layout.parse_rows 的说明），
    用它去套列位置会把票投给相邻的金额列——实测京东方年报的同比列
    因此被读成上一年的营业收入。数字的先后顺序则永远可信。
    整张表都没有百分号时返回 None，由调用方退回按位置取。
    """
    votes = {}
    for row in rows:
        line = layout.iter_lines([row["raw"]])[0]
        numbers = [c for c in layout.expand_cells(line.cells) if c.kind == "num"]
        for idx, cell in enumerate(numbers):
            if cell.text.endswith("%"):
                votes[idx] = votes.get(idx, 0) + 1
    if not votes:
        return None
    return max(votes, key=lambda idx: (votes[idx], idx))


def _header_tokens(lines: list) -> list:
    """找出表头行的列标签。

    写死列数会在年报上翻车：年报的非经常性损益表并列三年，
    半年报只有一年，两者列数不同，而区间切分无法区分这一点。
    表头的判据是"除项目/说明这类固定列之外，每个列标签都长得像列标签"：
    只有这样，"（二） 非经常性损益项目和金额"这种小标题行才不会被误判成表头。
    """
    for ln in lines[:80]:
        tokens = [c.text for c in ln.cells]
        # 真正的表头行必然有多个空白分隔的列标签；
        # 只有单个 token 的行（"八、分季度主要财务指标"、"单位：元"）不是表头。
        if len(tokens) < 2:
            continue
        matched = [t for t in tokens if t not in _HEADER_JUNK and _LABEL_TOKEN.search(t)]
        others = [t for t in tokens if t not in _HEADER_JUNK and not _LABEL_TOKEN.search(t)]
        if matched and not others:
            return matched
    return []


# 表头里出现这几个字，说明该列是"年初至今累计"而不是单季数。
_CUMULATIVE_HINT = "年初至报告期期末"


def _cumulative_index(lines: list) -> int:
    """金额取第几列才算"累计数"。

    同一张非经常性损益表在不同报告期含义不同，必须看表头：
      一季报「本报告期金额」                     -> 第 0 列
      半年报「金额」                            -> 第 0 列
      三季报「本报告期金额｜年初至报告期期末金额」 -> 第 1 列
      年报「2025年金额｜2024年金额｜2023年金额」  -> 第 0 列
    不区分的话，三季报会把第三季度的单季数当成前三季度累计数存下来，
    数字看着正常，口径却完全错了，而且不会有任何报错。
    """
    for index, token in enumerate(_header_tokens(lines)):
        if _CUMULATIVE_HINT in token:
            return index
    return 0


def _anchors(pages: list) -> dict:
    found = {}
    for index, text in enumerate(pages):
        if not text:
            continue
        at = 0
        for raw in text.split("\n"):
            start = at
            at += len(raw) + 1
            if not raw.strip() or _toc(raw):
                continue
            for key, titles in MARKERS:
                if key in found:
                    continue
                if any(title in raw for title in titles):
                    found[key] = (index, start)
    return found


def _toc(line: str) -> bool:
    return "…" in line or "...." in line or "．．" in line


def _regions(pages: list) -> dict:
    ordered = sorted(_anchors(pages).items(), key=lambda kv: kv[1])
    out = {}
    for i, (key, pos) in enumerate(ordered):
        cap = pos[0] + REGION_CAP.get(key, 2)
        end = (cap, 1 << 30)
        if i + 1 < len(ordered):
            nxt = ordered[i + 1][1]
            end = min(nxt, end)
        out[key] = (pos, end)
    return out


def extract(pages: list, trace=None) -> dict:
    lines = layout.iter_lines(pages)
    regions = _regions(pages)
    out = {"key_metrics": {}, "quarterly": {}, "nonrecurring": None,
           "nonrecurring_total_page": None,
           "restated": {"value": None, "raw": "", "page": None}, "regions": {}}

    for key, region in regions.items():
        sub = _slice(lines, region)
        out["regions"][key] = region[0][0] + 1
        if not sub:
            continue
        columns = layout.detect_columns(sub)
        rows = layout.parse_rows(sub, columns)

        if key == "key_metrics":
            # 同比列的位置不能写死成第三列：年报表头常把更早一年的数据并列
            # 进来，"2025／2024／2023／增减"这种排法下第三列是 2023 年的金额。
            # 带百分号的列才是同比；整张表找不到百分号（有的公司只写数字、
            # 在表头注明"(%)"）时再退回第三列。
            pct_at = _percent_index(rows)
            if pct_at is None:
                pct_at = 2
            for row in rows:
                name = key_of(layout.label_candidates(row))
                if not name or name in out["key_metrics"]:
                    continue
                vals = row["values"]
                out["key_metrics"][name] = {
                    "cur": vals[0] if vals else None,
                    "prev": vals[1] if len(vals) > 1 else None,
                    "yoy_pct": vals[pct_at] if len(vals) > pct_at else None,
                    "page": row["page"], "label": row["label"],
                }
            out["restated"] = _restated(pages, region)

        elif key == "quarterly":
            for row in rows:
                name = key_of(layout.label_candidates(row))
                if name not in QUARTERLY_ORDER or name in out["quarterly"]:
                    continue
                out["quarterly"][name] = {"values": list(row["values"]),
                                          "page": row["page"]}

        elif key == "nonrecurring":
            items, total, seen = [], None, set()
            amount_col = _cumulative_index(sub)
            for row in rows:
                if total is not None:
                    # 合计行就是这张表的结尾。区域是按页数封顶的，末尾那份
                    # 定期报告的非经常性损益表后面往往紧跟另一张表
                    # （实测茅台年报的区域落到了"采用公允价值计量的项目"上），
                    # 把后一张表的"合计"当成本表合计，会得到一个与
                    # "归母−扣非"差两个数量级的数。
                    break
                vals = [v for v in row["values"] if v is not None]
                if not vals or is_furniture(row["raw"]):
                    continue
                if layout.to_float(row["label"]) is not None:
                    continue
                # 一行里可能有多个数字（单季数、累计数、上年数）。
                # 取"累计数"所在的序号，缺位时才回退到第一个数字。
                amount = vals[amount_col] if len(vals) > amount_col else vals[0]
                # 合计行只认本行科目名。合计行的下一行往往是"其他符合…的
                # 具体情况："这类小标题，拼接进来的话就再也认不出它是合计行，
                # 表现是"各项明细都抽到了，却没有合计"。
                if flat(row["label"]).rstrip("：:") == _TOTAL_FLAT:
                    total = amount
                    out["nonrecurring_total_page"] = row["page"]
                    continue
                text = row_text(row)
                if not text:
                    continue
                name = nonrecurring_key(text)
                if not name or name in seen:
                    continue          # 对不上标准项目、或重复的标准项目：丢弃
                seen.add(name)
                items.append({"label": name, "amount": amount,
                              "raw": text, "page": row["page"]})
            if items or total is not None:
                out["nonrecurring"] = {"items": items, "total": total}

    if trace:
        trace.record("ingest_highlights",
                     key_metrics=sorted(out["key_metrics"]),
                     quarterly=sorted(out["quarterly"]),
                     nonrecurring_items=len((out["nonrecurring"] or {}).get("items") or []),
                     restated=out["restated"]["value"])
    return out


# 版面上的固定文字（页眉、填写说明、页码），不是科目名的一部分。
_FURNITURE = ("适用", "单位：", "单位:", "报告全文", "股份有限公司",
              "年度报告", "半年度报告", "季度报告")


def is_furniture(text: str) -> bool:
    """判断一行是否只是页眉、填写说明或页码。"""
    if not text:
        return True
    if any(key in text for key in _FURNITURE):
        return True
    return bool(re.fullmatch(r"[\d\s.、]+", text))


def _lead_text(row: dict) -> str:
    """取紧邻数据行上方、确实属于本科目名的续行。

    同一张「非经常性损益」表，有的公司把科目名截在上一行
    （"非流动性资产处置损"＋"益（包括…）"），有的公司截在下一行
    （"非流动性资产处置损益（包括已计提"＋"资产减值准备的冲销部分）"），
    两种排版都真实存在，因此只能按内容判断。

    lead 是逐行保存的，越靠近数据行的行越可能属于本科目，所以从最近一行
    往外取；一旦碰上版面固定文字（页眉、"单位：元"、填写说明）或以右括号
    结尾的上一科目残留，就立即停止——再往上的行只可能属于更早的科目，
    把它们粘进来会造出一个不存在的科目名。
    """
    out = []
    for text in reversed(row.get("lead") or []):
        if is_furniture(text) or text.rstrip().endswith(("）", ")")):
            break
        out.insert(0, text)
        if len(out) >= 2:
            break
    return "".join(out)


def row_text(row: dict) -> str:
    """科目的完整名称：被截断的前半截 + 本行 + 折到下一行的后半截。"""
    # 只取紧跟其后的那一行：tail 的第二行往往已经是下一个科目的开头，
    # 拼进来会把两条明细混成一条（实测出现过"…冲销部分）计入当期损益的政府补助"）。
    tail = (row.get("tail") or [""])[0]
    label = row["label"]
    lead = _lead_text(row)
    if label:
        return (lead + label + tail).strip()
    return (lead + tail).strip()


def _slice(lines: list, region) -> list:
    (p0, o0), (p1, o1) = region
    out = []
    for ln in lines:
        if ln.page - 1 < p0 or ln.page - 1 > p1:
            continue
        if ln.page - 1 == p0 and ln.at < o0:
            continue
        if ln.page - 1 == p1 and ln.at >= o1:
            continue
        out.append(ln)
    return out


def _restated(pages: list, region) -> dict:
    """读取「是否需追溯调整或重述以前年度会计数据」。

    只承认明确出现勾选标记的情形。未勾选的方框在原文里就是普通的 □，
    而已勾选的方框多半是字体私有区字符；若按"没有勾就是否"来推断，
    会把"公司其实重述了上年数"读成"没重述"，
    而重述正是会计口径变化最直接的信号。因此宁可回报"未能判定"。
    """
    (p0, o0), (p1, _) = region
    for index in range(p0, min(p1 + 1, len(pages), p0 + 3)):
        text = pages[index] or ""
        pos = text.find("追溯调整")
        if pos < 0:
            continue
        window = text[pos:pos + 140]
        value = None
        for flag in ("是", "否"):
            mark = _CHECKED.search(window)
            hit = re.search(re.escape(flag) + r"(?![\u4e00-\u9fa5])", window)
            if mark and hit and mark.start() < hit.start():
                # 标记出现在该选项之前，且与它之间只隔着空白
                between = window[mark.end():hit.start()]
                if between.strip() == "":
                    value = flag == "是"
                    break
        return {"value": value, "raw": " ".join(window.split())[:80],
                "page": index + 1}
    return {"value": None, "raw": "", "page": None}
