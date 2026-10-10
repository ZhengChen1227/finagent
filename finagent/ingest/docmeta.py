"""文档身份解析：从财报正文读出"这是谁、哪一期"。

存在的意义：本系统不接受用户输入公司名或股票代码，所有主体信息只能来自
上传的财报本身。因此这一层是整条证据链的第一环——解析错了，后面所有
指标都会挂在错误的公司名下，而且不会报错。

解析对象是定期报告里两处格式高度统一的固定写法：

- 季度报告的页眉：「证券代码：002714　证券简称：牧原股份　公告编号：…」
- 年报／半年报的「第二节 公司简介」：「股票简称　牧原股份　股票代码　002714（A股）、02714」

但有三处坑必须显式处理，否则会静默产出错的主体：

1. **报告类型必须按长短顺序匹配。**"半年度报告"里含有"年度报告"，
   先匹配"年度报告"会把中报认成年报，报告期随之从 6月30日 变成 12月31日。
2. **"债券代码"不是股票代码。** 同时发债的公司会在页眉并列两组代码，
   宽松匹配会把可转债代码当成股票代码。
3. **代码必须紧跟在标签之后。** 年报正文里的证券投资明细表同样有
   「证券代码　简称　最初投资成本」这样的表头，若只认标签不认位置，
   系统会把公司持有的某只股票当成公司自己。因此这里要求标签之后
   跳过空白与冒号就必须直接是六位数字，否则该次命中作废、继续向下找。
4. **A+B 股公司会并列两个代码**（如京东方 A/B）。取第一个 A 股代码，
   因为 B 股代码（200xxx／900xxx）在多数披露体系里不是主代码。
"""

from __future__ import annotations

import re

# 报告类型：顺序即优先级。长的、更具体的写法必须排在前面。
REPORT_TYPES = (
    ("q1", "第一季度报告", ("第一季度报告", "一季度报告", "一季报")),
    ("q3", "第三季度报告", ("第三季度报告", "三季度报告", "三季报")),
    ("semi", "半年度报告", ("半年度报告", "中期报告", "中报")),
    ("annual", "年度报告", ("年度报告", "年报")),
)

# 报告类型 -> 报告期截止日（月, 日）
PERIOD_END = {"q1": (3, 31), "semi": (6, 30), "q3": (9, 30), "annual": (12, 31)}
PERIOD_LABEL = {"q1": "一季报", "semi": "半年报", "q3": "三季报", "annual": "年报"}
# 报告期序数：用于把同一主体同一年内的多期材料排成时间轴。
PERIOD_ORDER = {"q1": 1, "semi": 2, "q3": 3, "annual": 4}

# 代码字段。刻意不含"债券代码"：同时发债的公司会并列两组代码。
_CODE_LABEL = re.compile(r"(?:证券代码|股票代码|公司代码|A\s*股代码)\s*[:：]?\s*")
# 标签之后必须直接是代码；多代码用 、,，/ 分隔。
_CODE_HEAD = re.compile(r"^\s*(\d{6}(?:\s*[、,，/]\s*\d{6})*)")
_LINE_TAIL = re.compile(r"[\r\n]")
_NAME_LABEL = re.compile(r"(?:证券简称|股票简称|公司简称|A\s*股简称)\s*[:：]?\s*")
_FULL_LABEL = re.compile(
    r"(?:公司的中文名称|公司中文名称|中文名称|公司名称|注册名称)\s*[:：]?\s*")
# 简称/全称的取值：到下一个字段标签或行尾为止。
_NAME_VALUE = re.compile(
    r"^(?P<value>[^\s]+(?:\s*[、,，]\s*[^\s]+)*)")
_STOP_LABEL = re.compile(
    r"(?:股票代码|证券代码|公司代码|债券代码|英文名称|英文简称|注册地址|办公地址|"
    r"公司注册地址|统一社会信用代码|公告编号|债券简称)\s*[:：]?")
# 报告期区间："本报告期自2026年1月1日起至2026年6月30日止"
_RANGE = re.compile(
    r"自\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*起"
    r"\s*至\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*止")
_YEAR_NEAR = re.compile(r"(\d{4})\s*年")
_FULLWIDTH = str.maketrans("０１２３４５６７８９：，", "0123456789:,")

# 主体信息的扫描页数上限。年报里的证券投资明细表通常排在
# 公司简介之后，按页序取首个命中即可避开它；设上限是为了不让
# 一份 250 页的年报把时间花在无关页面上。
SCAN_PAGES = 20


def _halfwidth(text: str) -> str:
    return (text or "").translate(_FULLWIDTH)


def detect_type(pages: list):
    """识别报告类型，返回 (key, 中文名) 或 (None, "")。

    先只看封面（第 1 页）：封面的"2026年半年度报告"是最权威的表述。
    封面取不到才往后翻，避免正文里偶然出现的"半年度报告"字样
    把年报认成中报。匹配顺序按 REPORT_TYPES，长写法在前。
    """
    for index in (0,):
        if index < len(pages) and pages[index]:
            for line in pages[index].split("\n"):
                for key, label, names in REPORT_TYPES:
                    if any(name in line for name in names):
                        return key, label
    for text in pages[1:4]:
        if not text:
            continue
        for key, label, names in REPORT_TYPES:
            if any(name in text for name in names):
                return key, label
    return None, ""


def _codes_on(text: str) -> list:
    """取一页里紧跟代码字段之后的六位数字。标签后不是数字则视为误命中。

    先做全角转半角：不少公司（尤其老版年报）的"公司代码：６００５１９"用的是
    全角数字。正则 \d 能匹配全角数字，于是会得到一个"长得像代码"的全角串，
    它既不等于任何真实代码，也会让归组、检索、证据编号全部对不上。
    """
    text = _halfwidth(text)
    out = []
    for match in _CODE_LABEL.finditer(text):
        rest = text[match.end():]
        rest = rest[:120]
        head = _CODE_HEAD.match(rest)
        if not head:
            # 标签后不是数字（例如证券投资表的"证券代码　简称　…"），作废
            continue
        for num in re.findall(r"\d{6}", head.group(1)):
            if num not in out:
                out.append(num)
    return out


def _value_after_label(text: str, label_re) -> "str | None":
    """取字段标签之后、到下一个字段标签或行尾为止的取值。"""
    match = label_re.search(text)
    if not match:
        return None
    rest = text[match.end():]
    line = _LINE_TAIL.split(rest)[0][:80]
    stop = _STOP_LABEL.search(line)
    if stop:
        line = line[:stop.start()]
    value = line.strip(" 　:：")
    return value or None


def pick_a_share(codes: list):
    """A+B 股并列时取首个 A 股代码：B 股为 200xxx（深）与 900xxx（沪）。"""
    for code in codes:
        if not code.startswith(("200", "900")):
            return code
    return codes[0] if codes else None


def _short_name(value: "str | None") -> "str | None":
    """简称可能是"A，B"并列（京东方A，京东方B），取第一个并去掉内部空白。"""
    if not value:
        return None
    first = re.split(r"[、,，/]", value)[0]
    first = re.sub(r"[\s\u3000]+", "", first)
    return first or None


def parse_report_date(pages: list, kind: str):
    """确定报告期截止日。

    优先用正文声明的报告期区间（"自…起至…止"）——它是原文的明确表述；
    取不到才用封面年份加报告类型对应的标准截止日推算，并标注来源，
    让读者能分辨"原文写的"与"按惯例推的"。
    """
    for text in pages[:6]:
        match = _RANGE.search(_halfwidth(text or ""))
        if match:
            year, month, day = int(match.group(4)), int(match.group(5)), int(match.group(6))
            return f"{year:04d}-{month:02d}-{day:02d}", "原文报告期区间"
    for text in pages[:4]:
        if not text:
            continue
        for line in text.split("\n"):
            if not line.strip() or not kind:
                continue
            if not any(name in line
                       for _, _, names in REPORT_TYPES for name in names):
                continue
            years = _YEAR_NEAR.findall(_halfwidth(line))
            if years:
                month, day = PERIOD_END[kind]
                return f"{int(years[0]):04d}-{month:02d}-{day:02d}", "封面年份＋报告类型"
    return None, "未能解析"


def parse(pages: list, filename: str = "") -> dict:
    """解析主体身份。返回 dict；解析不出的字段为 None，绝不臆造。"""
    out = {"full_name": None, "short_name": None, "code": None, "codes": [],
           "report_kind": None, "report_kind_label": None, "report_date": None,
           "report_date_source": "未能解析", "display": "", "warnings": []}
    if not pages:
        out["warnings"].append("PDF 未抽取到任何文本")
        return _finalize(out, filename)

    kind, label = detect_type(pages)
    out["report_kind"], out["report_kind_label"] = kind, label

    # 按页序扫描主体信息，首个同时给出代码或名称的页面即采用。
    for text in pages[:SCAN_PAGES]:
        if not text:
            continue
        if not out["codes"]:
            out["codes"] = _codes_on(text)
        if not out["short_name"]:
            out["short_name"] = _short_name(_value_after_label(text, _NAME_LABEL))
        if not out["full_name"]:
            name = _value_after_label(text, _FULL_LABEL)
            if name and len(name) >= 4:
                out["full_name"] = name
        if out["codes"] and out["short_name"]:
            break
    out["code"] = pick_a_share(out["codes"])

    # 全称兜底：封面写法「XX股份有限公司 2026年年度报告」，名称紧跟在年份之前。
    if not out["full_name"]:
        for text in pages[:4]:
            if not text:
                continue
            found = None
            for line in text.split("\n"):
                flat = _halfwidth(line)
                pos = _YEAR_NEAR.search(flat)
                if not pos:
                    continue
                if label and label not in flat and "报告" not in flat:
                    continue
                name = flat[:pos.start()].strip(" 　:：")
                if len(name) >= 4 and ("公司" in name or "集团" in name):
                    found = name
                    break
            if found:
                out["full_name"] = found
                break

    date, source = parse_report_date(pages, kind)
    out["report_date"], out["report_date_source"] = date, source
    return _finalize(out, filename)


def _finalize(out: dict, filename: str) -> dict:
    """补全展示名并记录必须让用户知道的问题。"""
    if not out["report_kind"]:
        out["warnings"].append("未能识别报告类型（年度/半年度/季度报告）")
    if not out["report_date"]:
        out["warnings"].append("未能识别报告期截止日")
    if not out["code"] and not out["full_name"] and not out["short_name"]:
        out["warnings"].append("未能识别公司名称与股票代码，已用文件名兜底，请人工确认")
        out["display"] = filename or "未识别主体"
    elif out["short_name"] and out["code"]:
        out["display"] = f"{out['short_name']}（{out['code']}）"
    elif out["full_name"]:
        out["display"] = out["full_name"]
    else:
        out["display"] = out["code"] or filename
    return out
