"""版面行解析：把 layout 文本还原成「科目名 + 本期值 + 上期值」。

存在的意义：中国上市公司财报的三大表是严格分列的宽表格，
而纯文本抽取后只剩下"一行字符串"。若不做列还原，
就无法区分一行的两个数字哪个是本期、哪个是上期——
同比会被算成"两个不同科目相比"，而且这种错误不会报错，只会静静产出错数字。

这里不引入任何表格识别库，只利用 layout 模式的两个稳定特征：

1. 数值一律按列**右对齐**，同一列的数字结束列号高度一致（±1）。
   据此用聚类自动发现"这一页有几列数值、各列的右边界在哪"。
   不看表头文字，因为各家表头写法不一（本期金额/本期发生额/2026年半年度…），
   但"数字右对齐"在所有财报里都成立。
2. 科目名的文本区宽度固定，超出即折行，折行后的续行不含数字。

第 2 点**刻意不做阈值猜测**：折行位置随表格留给科目名的宽度变化，
任何基于列号的阈值都会在某份财报上失效（实测确有反例：
同一家公司半年报的资产负债表与现金流量表，折行列号就不同）。
本模块改为原样保留本行文本与相邻的无数字行，交给同义词匹配层
生成候选写法逐一精确比对——匹配是全等比较，因此不可能误配。
"""

from __future__ import annotations

import re
from typing import NamedTuple

# 数值单元：允许千分位、小数，以及四种负号写法。
# 百分号也认作数值：年报的「主要会计数据和财务指标」把同比增减与金额并列成列，
# 不认百分号，那一列就会被当成科目名文本，整张表随之错列。
_NUM_RE = re.compile(r"^[-−–—－]?\d[\d,]*(?:\.\d+)?%?$")
# 中文财报沿用会计惯例，用括号表示负数（京东方："(4,005,541,287)"）。
# 不认这种写法，资产减值损失、投资损失这类"本来就是负数"的科目会整行消失。
_PAREN_NUM_RE = re.compile(r"^[（(][-−–—－]?\d[\d,]*(?:\.\d+)?%?[)）]$")
_DASH_RE = re.compile(r"^[-−–—－]+$")
# 一个数值列至少要出现这么多次，才承认它是一列（防止把零星的页码当列）。
_MIN_COLUMN_HITS = 2
# 续行最多向后看几行
_TAIL_LIMIT = 2
# 科目名最多可能折成这么多行后才出现数值
_LEAD_LIMIT = 3

# 被版面抽取粘在一起的多数字单元，其每一段的形态。
# 末尾的百分号必须算进单元里：年报的"本年比上年增减"列是百分数，
# 不认百分号，粘在一起的那一串就会在这里断掉，整行退回成科目名文本。
_NUM_PART = re.compile(r"[-−–—－]?\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?%?")


class Cell(NamedTuple):
    """一行里的一个非空白片段及其字符列区间。"""

    text: str
    start: int
    end: int
    kind: str             # num / dash / text


class Line(NamedTuple):
    no: int
    page: int
    at: int               # 本行在"本页文本"中的起始偏移
    text: str
    cells: tuple

    @property
    def numbers(self) -> tuple:
        return tuple(c for c in self.cells if c.kind == "num")


class Columns(NamedTuple):
    """一张表的数值列布局：每列的右边界字符列号 + 这张表的列数（表宽）。"""

    ends: tuple
    # 表格应有的列数。列是聚类出来的，而数字被版面粘成一团时（相邻列之间
    # 没有空格）拆出来的偏移量只是推算值，有些列根本聚不出簇，
    # 簇数于是少于真实列数。"一行里最多出现过几个数字"才是可靠的表宽。
    width: int = 0

    @property
    def n(self) -> int:
        return len(self.ends)

    @property
    def span(self) -> int:
        """这张表的宽度：列数与表宽中较大的一个。"""
        return max(self.width, self.n)

    def assign(self, cell: Cell):
        """把一个数值单元分配到最近的列；超出容差则返回 None。"""
        best, best_gap = None, None
        for idx, end in enumerate(self.ends):
            gap = abs(cell.end - end)
            if best_gap is None or gap < best_gap:
                best, best_gap = idx, gap
        if best is None:
            return None
        # 容差取相邻列间距的一半，避免两列的数字都被塞进同一列
        if len(self.ends) > 1:
            gaps = [abs(self.ends[i + 1] - self.ends[i]) for i in range(len(self.ends) - 1)]
            limit = max(4, min(gaps) / 2)
        else:
            limit = 12
        return best if best_gap <= limit else None


def to_float(text: str):
    """财报数值转 float。识别不出数字时返回 None，绝不猜。

    末尾的百分号要剥掉：年报的「主要会计数据和财务指标」把金额与同比增减
    并列成列，若百分号让转换失败，那一列会整列变成 None，
    表现为"披露的同比率怎么都读不出来"。
    """
    t = (text or "").strip()
    negative = False
    if len(t) >= 2 and t[0] in "（(" and t[-1] in "）)":
        negative = True
        t = t[1:-1].strip()
    t = t.replace(",", "").replace("，", "").rstrip("%")
    for sign in ("−", "–", "—", "－"):
        t = t.replace(sign, "-")
    try:
        value = float(t)
    except ValueError:
        return None
    return -value if negative else value


def split_numbers(text: str) -> list:
    """把一个被粘在一起的多数字单元拆成若干数字，拆不开则原样返回。

    layout 模式按位置补空格，但列宽正好被数字填满时两个数字会直接相连。
    实测："90,703,260,964.4889,389,354,416.84"。若不拆，整个单元不是合法
    数字，会被当成科目名文本，两个金额一起消失——营业收入这类关键科目
    会静默缺失，而且不会报错。

    拆分必须足够保守，所以要求整串由数字严丝合缝拼成（不留任何残余字符），
    且第一段之后的每一段要么带千分位、要么带小数点，要么至少三位数字。
    这样"1.234"这种三位小数的普通数字不会被拆成"1.23"+"4"。
    """
    if _NUM_RE.match(text) or _PAREN_NUM_RE.match(text):
        return [text]
    parts, pos = [], 0
    while pos < len(text):
        match = _NUM_PART.match(text, pos)
        if not match:
            return [text]
        parts.append(match.group())
        pos = match.end()
    if len(parts) < 2:
        return [text]
    for part in parts[1:]:
        plain = part.lstrip("-−–—－")
        if len(plain) < 3 and "," not in part and "." not in part:
            return [text]
    return parts


def expand_cells(cells: tuple) -> list:
    """把粘在一起的数字单元展开成多个数值单元，其余单元原样保留。

    这一步对整条解析链路都生效：列检测、行还原、续行判断用的都是展开后的
    单元，否则粘在一起的两个数字会让整行被判成"没有数字"。
    """
    out = []
    for cell in cells:
        if cell.kind == "text":
            parts = split_numbers(cell.text)
            if len(parts) > 1:
                at = cell.start
                for part in parts:
                    out.append(Cell(part, at, at + len(part), "num"))
                    at += len(part)
                continue
        out.append(cell)
    return out


# 附注号写法："五、46"（京东方老版年报复合表）、"附注五"、"（三）"、"十一、3"。
# 这类记号夹在科目名和金额之间，若把它算进科目名，
# "一、营业收入 五、46" 就再也匹配不上"营业收入"，整张利润表读不出来。
_NOTE_REF = re.compile(r"^(?:附注)?[（(]?[一二三四五六七八九十百]{1,3}[）)]?[、.．]?\d{0,3}$")


def classify(token: str) -> str:
    if _NUM_RE.match(token) or _PAREN_NUM_RE.match(token):
        return "num"
    if _DASH_RE.match(token):
        return "dash"
    return "text"


def iter_lines(pages: list) -> list:
    """把逐页文本摊平为带页码的行序列。"""
    out = []
    no = 0
    for page, text in enumerate(pages, start=1):
        at = 0
        for raw in (text or "").split("\n"):
            no += 1
            start = at
            at += len(raw) + 1          # 换行符也占一个字符位
            if not raw.strip():
                continue
            cells = tuple(
                Cell(m.group(), m.start(), m.end(), classify(m.group()))
                for m in re.finditer(r"\S+", raw)
            )
            out.append(Line(no, page, start, raw, cells))
    return out


def _max_numbers_in_row(lines: list) -> int:
    """一行里最多出现过几个数字。用于自动推断"这张表有几列"。"""
    counts = [sum(1 for c in expand_cells(ln.cells) if c.kind == "num") for ln in lines]
    return max(counts) if counts else 0


# 附注号列的门槛。附注号是 1 到几百的整数，而报表里的金额以元计，
# 极少出现 1000 以下的整数（只有"每股收益"这类指标是小数字，而它不在三大表里）。
_NOTE_MAX = 1000
_NOTE_SHARE = 0.8


def is_note_column(values: list) -> bool:
    """判断一列数字是否只是"附注五 47"里的附注号，而不是金额。

    京东方这类公司的利润表带一列附注号，且附注号是**光秃秃的整数**
    （"附注五  47  204,590,222,888"）。若不把它剔除，聚类会把附注号
    当成一列金额，营业收入可能被读成 47——数字看着像模像样，
    却与真实收入差了七个数量级，这是最危险的一种错。
    """
    real = [v for v in values if v is not None]
    if not real:
        return False
    small = sum(1 for v in real
                if float(v).is_integer() and abs(v) < _NOTE_MAX)
    return small >= _NOTE_SHARE * len(real)


def detect_columns(lines: list, expected: "int | None" = None,
                   drop_note: bool = False) -> "Columns | None":
    """从数据本身反推数值列：按右边界聚类，取命中次数最多的若干列。

    expected 为 None 时自动推断列数（取一行里出现过的最大数字个数）。
    之所以要自动推断：年报与半年报、不同公司的表头写法与列数都不一样，
    写死列数会在列数更多的表上把两组数字挤进同一列，
    产出的数字看着正常、含义却完全错了。

    也有必须写死列数的场合：三大表一律是"本期／上期"两列，
    让程序自动数数列在那里反而危险——附注号、页码之类零星数字
    会凑出第三列，把本期数与上期数挤到一起。
    """
    pairs = sorted((c.end, to_float(c.text)) for ln in lines
                   for c in expand_cells(ln.cells) if c.kind == "num")
    if not pairs:
        return None
    clusters = [{"ends": [pairs[0][0]], "values": [pairs[0][1]]}]
    for end, value in pairs[1:]:
        if end - clusters[-1]["ends"][-1] <= 5:
            clusters[-1]["ends"].append(end)
            clusters[-1]["values"].append(value)
        else:
            clusters.append({"ends": [end], "values": [value]})
    # 表宽要放在聚类之前算：它是"一行里最多出现过几个数字"，
    # 不依赖聚类结果，因此数字粘连时也不会被带偏。
    if expected is None:
        expected = max(2, min(_max_numbers_in_row(lines), 6))
    clusters = [c for c in clusters if len(c["ends"]) >= _MIN_COLUMN_HITS]
    if not clusters:
        return None
    if len(clusters) == 1:
        # 只有一列数值的表（半年报的非经常性损益表就是这种）也要能还原，
        # 否则金额会整体丢失。表宽记为 1，使这类表的行按顺序读。
        return Columns((round(sum(clusters[0]["ends"]) / len(clusters[0]["ends"])),), 1)
    if drop_note and len(clusters) > expected:
        rest = [c for c in clusters if not is_note_column(c["values"])]
        if len(rest) >= expected:
            clusters = rest
    clusters.sort(key=lambda c: len(c["ends"]), reverse=True)
    kept = sorted(round(sum(c["ends"]) / len(c["ends"])) for c in clusters[:expected])
    return Columns(tuple(kept), expected)


def parse_rows(lines: list, columns: "Columns | None" = None) -> list:
    """还原数据行。返回 dict：label / lead / tail / values / page / raw。

    - label  本行中数值之前的文本
    - lead   紧邻其上的无数字行（逐行保存，可能是被折到上一行的科目名）
    - tail   紧随其后的无数字行（最多两行，可能是被折下来的后半截科目名）
    - values 按列对齐的数值，缺失为 None

    没有数值的行不作为数据行返回，只作为相邻行的 lead / tail 保留。
    """
    if columns is None:
        columns = detect_columns(lines)
    width = columns.span if columns else 2

    rows = []
    leads = []
    i, n = 0, len(lines)
    while i < n:
        ln = lines[i]
        if ln.cells and ln.cells[0].text == "项目":
            i += 1
            continue
        cells = expand_cells(ln.cells)
        nums = [c for c in cells if c.kind == "num"]
        if not nums:
            # 科目名可能折成三行以上，数值落在折完之后的那一行。
            # 这里必须累积而不是覆盖：只留最后一行，科目名的前半截就永久丢失，
            # 表现是"扣非净利润这类科目怎么也抽不到"——实测中确实踩到过。
            leads.append(ln.text.strip())
            leads = leads[-_LEAD_LIMIT:]
            i += 1
            continue

        # 科目名取到"第一个数值单元之前"，但要把夹在中间的附注号去掉：
        # 附注号是版面上的定位记号，不是科目名的一部分。
        pre = [c for c in cells if c.end <= nums[0].start]
        while pre and _NOTE_REF.match(pre[-1].text):
            pre.pop()
        label = ln.text[:pre[-1].end].strip() if pre else ""
        values = [None] * width
        # 位置对齐的前提是"每个数字的偏移量都是版面真值"。列宽被数字填满时，
        # 相邻两列的数字会直接相连（实测京东方年报：
        # "204,590,222,888.00198,380,605,661.003.13%174,543,445,895.00"），
        # 拆出来的偏移量只是按字符长度推算的，拿它去套列位置会把同比那一列
        # 整个丢掉，于是"2023 年的营业收入"被当成"本年比上年增减"写进报告。
        # 判据因此改用表宽：数字个数正好等于表宽，说明这一行没有空缺，
        # 按出现顺序对齐就是准确的（数字个数多于表宽的附注号行不在此列）。
        if columns is not None and len(nums) == columns.span:
            # 本行的数字个数与列数完全相同：这一行没有任何空缺，
            # 按出现顺序对齐就是准确的。位置对齐在这一行反而会出错——
            # 同一片区域里可能并排两张列宽不同的表（年报的"主要会计数据"
            # 与"主要财务指标"就是这样），拿其中一张表的列位置去套另一张，
            # 中间那列会因为超出容差而被丢掉，表现是"上年同期一列全是空的"。
            for idx, cell in enumerate(nums):
                values[idx] = to_float(cell.text)
        else:
            for cell in nums:
                if columns is None:
                    # 列结构完全没识别出来时才退化为按出现顺序填。
                    idx = 0 if values[0] is None else min(1, width - 1)
                else:
                    # 识别出了列结构，却有一个数字离任何列都太远：宁可丢弃。
                    # 硬塞进某一列会产出一个"看起来正常"的错值，
                    # 而错值会一路流进报告，丢值只会表现为该项缺失。
                    idx = columns.assign(cell)
                    if idx is None:
                        continue
                if values[idx] is None:
                    values[idx] = to_float(cell.text)

        tail = []
        j = i + 1
        while (j < n and len(tail) < _TAIL_LIMIT
               and not [c for c in expand_cells(lines[j].cells) if c.kind == "num"]):
            tail.append(lines[j].text.strip())
            j += 1
        rows.append({"label": label, "lead": list(leads), "tail": tail,
                     "values": values, "page": ln.page, "raw": ln.text})
        # 只前进一步：tail 里的那些无数字行同时也是"下一行的 lead"。
        # 若在这里跳过它们，被折在上一行的科目名前半截就会永久丢失——且
        # 实测中"归属于上市公司股东的扣除／非经常性损益的净利润"正是靠
        # 它们才能拼出完整科目名。两者究竟是"上一行的续尾"还是"下一行的
        # 开头"，在行层面无法判别，因此一律保留、由同义词层的全等比较定夺。
        leads = []
        i += 1
    return rows


def label_candidates(row: dict) -> list:
    """一行科目名的全部可能写法，按可信度从高到低。

    只有当本行文本被折行截断时，拼接才可能命中同义词表；
    完整科目名会在第一个候选就命中，因此拼接不会引入误配。

    lead 逐行保存、按行拼接，而不是先拼成一整串再相加。原因是折行后的
    科目名，其真正的开头一定落在某一行的**行首**，而上一行的残留
    （例如上一个科目折下来的"润（元）"）只落在行尾。按行拼接从最近一行
    往外扩，天然避开了这种残留——实测中"归属于上市公司股东的扣除非经常性
    损益的净利润"正是因为把上一行的"润（元）"粘了进来才永远匹配不上。
    """
    label, leads, tail = row["label"], list(row.get("lead") or []), row.get("tail") or []
    out = []

    def add(text):
        text = (text or "").strip()
        if text and text not in out:
            out.append(text)

    if label:
        add(label)
        if tail:
            add(label + tail[0])
        if len(tail) > 1:
            add(label + tail[0] + tail[1])
        # 从最近的一行往外扩：越近的续行越可能属于本科目。
        for start in range(len(leads) - 1, -1, -1):
            head = "".join(leads[start:])
            add(head + label)
            if tail:
                add(head + label + tail[0])
            if len(tail) > 1:
                add(head + label + tail[0] + tail[1])
    else:
        for start in range(len(leads) - 1, -1, -1):
            head = "".join(leads[start:])
            add(head)
            if tail:
                add(head + tail[0])
    return out


def rows_with_values(rows: list) -> list:
    """只保留至少有一列数值的行，用于科目匹配与指标提取。"""
    return [r for r in rows if any(v is not None for v in r["values"])]
