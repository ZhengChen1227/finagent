"""合并报表抽取：从版面行还原三大表与现金流量表补充资料。

存在的意义：三大表的数字是全部指标与结论的源头，而"把哪一行认成哪个科目"
是本系统最容易出错、也最不容易被发现的一步。因此这里有两条硬规则：

1. **按报表标题切分区域，只在本表的页区间内取数。**
   全文扫描看似省事，但年报开头的「主要会计数据和财务指标」与年报末尾的
   「分季度主要财务指标」都含有"营业收入""归属于上市公司股东的净利润"
   这类行，且后者是四个季度并列的四列。不切区域，系统会把第一季度的
   单季收入当成全年收入，且不会报任何错。
2. **同一科目取首次出现。** 中国财报一律先列合并报表、后列母公司报表，
   因此按文档顺序首次命中的必然是合并口径。若反过来取，指标会全部
   变成母公司口径——数字看着正常，含义完全不同。
"""

from __future__ import annotations

import re

from finagent.datasource import schema
from finagent.ingest import layout

# 报表标题的识别不用精确字符串，而用"合并/母公司 ＋ 表名"的组合。
# 理由是同一张表有多种写法，精确匹配一定会漏：
#   年报   "合并资产负债表" / "母公司资产负债表"
#   京东方 "合并股东权益变动表"，且母公司三张表只用裸称"资产负债表"
#   季报报 "2、合并年初到报告期末利润表"（表名里插了报告期描述）
# 漏掉一个标题不只是少一张表：区域的终点就是下一个标题，
# 找不到利润表标题，利润表的数字会被当成资产负债表科目去匹配。
TITLE_KINDS = ("资产负债表", "利润表", "现金流量表", "权益变动表")
# 权益变动表与另外三张表的关系：它不含"资产负债表""利润表""现金流量表"，
# 因此放在最后匹配不会抢走前者的命中。
EQUITY_KIND = "权益变动表"

# 标题行里不该出现的东西。出现任何一个，这一行就是正文或附注的引用，而非报表标题。
# "合并资产负债表项目："这类经营讨论的小标题也靠它排除——
# 这类行若被认成标题，资产负债表区域会提前结束，表就读不全了。
# 标题行去掉行首序号后的最大字数。"合并年初到报告期末现金流量表"是 14 个字。
_TITLE_MAX = 18
_LEAD_NO = re.compile(r"^\s*[0-9０-９]+\s*[、.．]\s*")
# 标题尾部允许的括注："（续）""(续)""（未经审计）"。
_TAIL_PAREN = re.compile(r"[（(][^（()）]*[)）]\s*$")


def _toc_like(line: str) -> bool:
    """目录行（带点线引导）不是报表标题，必须排除。"""
    return "…" in line or "...." in line or "．．" in line


def _title_of(line: str) -> "tuple | None":
    """判断一行是否是报表标题；是则返回 (区域名, 表名)。

    返回 None 表示不是标题。判据是"去掉行首序号后，整行以表名结尾且足够短"。

    用"结尾"而不是"包含"，是因为审计报告与附注里有大量以表名开头的正文，
    例如"将临近资产负债表日前后记录的收入与相""63.现金流量表项目注释"
    "资产负债表项目：""对合并利润表、合并现金流量表无重大影响"。
    这些行一旦被当成标题，报表区域会提前结束或从错误的页开始，
    抽出来的数字挂到错误的科目上，而且不会有任何报错。
    """
    text = _LEAD_NO.sub("", line.strip())
    text = _TAIL_PAREN.sub("", text).strip()
    if not text or len(text) > _TITLE_MAX:
        return None
    merged = "合并" in text
    if not any(text.endswith(kind) for kind in TITLE_KINDS):
        return None
    for kind in TITLE_KINDS:
        if not text.endswith(kind):
            continue
        if kind == EQUITY_KIND:
            key = "equity_change" if merged else "parent_equity_change"
        elif kind == "资产负债表":
            key = "balance" if merged else "parent_balance"
        elif kind == "利润表":
            key = "income" if merged else "parent_income"
        else:
            key = "cashflow" if merged else "parent_cashflow"
        return key, kind
    return None


def find_anchors(pages: list) -> dict:
    """定位各报表标题在文档中的位置，返回 {区域名: (页索引, 页内偏移)}。"""
    found = {}
    for index, text in enumerate(pages):
        if not text:
            continue
        at = 0
        for raw in text.split("\n"):
            start = at
            at += len(raw) + 1
            if not raw.strip() or _toc_like(raw):
                continue
            hit = _title_of(raw)
            if not hit:
                continue
            key = hit[0]
            if key in found:
                continue
            # 正文的报表一定排在附注之前，按页序取首个即可避开
            # "63.现金流量表项目注释"这类同名引用。
            found[key] = (index, start)
    return found


# 区域 -> (报表, 区域性质)
REGION_SCOPE = {
    "balance": ("balance", "main"),
    "income": ("income", "main"),
    "cashflow": ("cashflow", "main"),
    "supplement": ("cashflow", "supplement"),
}


def build_regions(pages: list, anchors: dict) -> dict:
    """把锚点转成半开区间 [(起始页, 起始偏移), (结束页, 结束偏移))。"""
    ordered = sorted(anchors.items(), key=lambda kv: kv[1])
    regions = {}
    for i, (key, pos) in enumerate(ordered):
        if not key.startswith(("balance", "income", "cashflow")):
            continue
        if key.startswith("parent_"):
            continue
        end = ordered[i + 1][1] if i + 1 < len(ordered) else (len(pages), 1 << 30)
        regions[key] = (pos, end)

    # 现金流量表补充资料单独成区：它排在最后一张合并表之后，
    # 用主表区间切不到，但它承载着拆解"利润与现金流差额"的全部调节项。
    for index, text in enumerate(pages):
        if text and "经营性应收项目" in text:
            regions["supplement"] = ((index, 0), (min(index + 3, len(pages)), 1 << 30))
            break
    return regions


def slice_lines(lines: list, region) -> list:
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


# 报表单位。绝大多数公司以元列示，但也有以万元、千元列示的；
# 认不出单位，金额会整体差几个数量级，而且不会有任何报错。
_UNIT_RE = re.compile(r"单位[：:]\s*(人民币)?\s*(万元|千元|元|百万元)")
_UNIT_SCALE = {"元": 1.0, "千元": 1e3, "万元": 1e4, "百万元": 1e6}


def detect_unit_scale(pages: list, region) -> float:
    """读取「单位：元 / 万元 / 千元」。读不到按元处理并留痕。"""
    (p0, o0), (p1, _) = region
    for index in range(p0, min(p1 + 1, len(pages))):
        match = _UNIT_RE.search(pages[index] or "")
        if match:
            return _UNIT_SCALE.get(match.group(2), 1.0)
    return 1.0


def extract(pages: list, trace=None) -> dict:
    """抽取全部报表科目。返回 fields / regions / units / unmatched。"""
    lines = layout.iter_lines(pages)
    anchors = find_anchors(pages)
    regions = build_regions(pages, anchors)

    out = {
        "fields": {"income": {}, "cashflow": {}, "balance": {}},
        "regions": {},
        "units": {},
        "unmatched": [],
    }
    if trace:
        trace.record("ingest_regions",
                     regions={k: [v[0][0] + 1, v[1][0] + 1] for k, v in regions.items()})

    for region_key, region in regions.items():
        report, area = REGION_SCOPE[region_key]
        sub = slice_lines(lines, region)
        if not sub:
            continue
        # 三大表一律是"本期／上期"两列，因此写死 2。
        # 让程序自动数数列在这里反而危险：附注号、页码之类零星数字
        # 会凑出第三列，把本期数与上期数挤到一起。
        # 真多出一列时（京东方这类公司会在金额左侧并列一列附注号），
        # 由 drop_note 把"全是 1000 以下整数"的那一列剔掉。
        columns = layout.detect_columns(sub, 2, drop_note=True)
        rows = layout.parse_rows(sub, columns)
        scale = detect_unit_scale(pages, region)
        out["regions"][region_key] = {"pages": [region[0][0] + 1, region[1][0] + 1],
                                      "columns": list(columns.ends) if columns else [],
                                      "rows": len(rows), "unit_scale": scale}
        out["units"][region_key] = scale

        bucket = out["fields"][report]
        for row in rows:
            key = schema.match_any(layout.label_candidates(row))
            if key is None:
                continue
            spec = schema.FIELD_MAP[key]
            if spec.report != report or spec.area != area:
                continue
            values = [(v * scale if v is not None else None) for v in row["values"]]
            item = {"cur": values[0] if values else None,
                    "prev": values[1] if len(values) > 1 else None,
                    "page": row["page"], "label": row["label"]}
            if key in bucket:
                if schema.COMBINE.get(key) == "sum":
                    old = bucket[key]
                    bucket[key] = dict(
                        old,
                        cur=_add(old["cur"], item["cur"]),
                        prev=_add(old["prev"], item["prev"]),
                        label=old["label"] + "＋" + item["label"],
                    )
                continue          # 非求和科目一律取首次出现
            bucket[key] = item

    if trace:
        for report, bucket in out["fields"].items():
            trace.record("ingest_fields", report=report,
                         matched=sorted(bucket), count=len(bucket))
    return out


def _add(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return a + b


def missing(pages: list, extracted: dict) -> list:
    """列出配置了同义词但一份材料里也没抽到的科目，用于界面与报告披露。"""
    out = []
    for report in ("income", "cashflow", "balance"):
        for key, spec in schema.FIELD_MAP.items():
            if spec.report != report:
                continue
            if key not in extracted["fields"].get(report, {}):
                out.append({"report": report, "field": key, "label": spec.label,
                            "area": spec.area})
    return out
