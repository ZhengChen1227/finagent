# -*- coding: utf-8 -*-
"""把 Markdown 分析报告导出为 PDF。

实现路径刻意选了"不引入第三方依赖"的一种：
    Markdown -> 打印级 HTML -> 无头浏览器 --print-to-pdf -> PDF 字节

为什么不用 weasyprint / reportlab：
  1. 竞赛要求提交可复现的依赖清单。少一个重型依赖，评委复现就少一个坑。
  2. 中文字体在无头浏览器里直接走系统字体，不需要额外打包字体文件。
  3. 浏览器打印分页由排版引擎负责，表格跨页、标题不孤行等效果比手写分页可靠。

浏览器找不到时不会抛异常，而是返回 None —— 上层据此提示用户改用浏览器打印。
"""

from __future__ import annotations

import html as _html
import os
import re
import shutil
import subprocess
import tempfile

# ---------------------------------------------------------------- 浏览器探测

_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expanduser(r"~\AppData\Local\Google\Chrome\Application\chrome.exe"),
    os.path.expanduser(r"~\AppData\Local\Microsoft\Edge\Application\msedge.exe"),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
]


def find_browser() -> str | None:
    """返回可用于打印 PDF 的浏览器可执行文件路径；找不到返回 None。"""
    env = os.environ.get("FINAGENT_BROWSER")
    if env and os.path.isfile(env):
        return env
    for path in _CANDIDATES:
        if os.path.isfile(path):
            return path
    for name in ("msedge", "google-chrome", "chromium", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


# ---------------------------------------------------------------- Markdown 渲染

def _esc(text: str) -> str:
    return _html.escape(str(text), quote=False)


def _inline(text: str) -> str:
    out = _esc(text)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"(^|[^*])\*([^*\s][^*]*)\*", r"\1<em>\2</em>", out)
    out = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
                 r'<a href="\2">\1</a>', out)
    return out


_SEP = re.compile(r"^\s*\|[\s:\-|]+\|\s*$")


def _split_row(line: str) -> list[str]:
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]
    return [c.strip() for c in text.split("|")]


def _alignments(sep: str) -> list[str]:
    """从分隔行读取对齐方式，让数字列在 PDF 里右对齐。"""
    out = []
    for cell in _split_row(sep):
        left = cell.startswith(":")
        right = cell.endswith(":")
        if left and right:
            out.append("center")
        elif right:
            out.append("right")
        elif left:
            out.append("left")
        else:
            out.append("right" if re.search(r"\d", cell) else "left")
    return out


def markdown_to_html(md: str) -> str:
    lines = str(md or "").replace("\r\n", "\n").split("\n")
    out: list[str] = []
    i = 0
    list_type = None

    def close_list():
        nonlocal list_type
        if list_type:
            out.append("</%s>" % list_type)
            list_type = None

    while i < len(lines):
        line = lines[i]

        if re.match(r"^\s*```", line):
            close_list()
            buf = []
            i += 1
            while i < len(lines) and not re.match(r"^\s*```", lines[i]):
                buf.append(_esc(lines[i]))
                i += 1
            i += 1
            out.append("<pre><code>%s</code></pre>" % "\n".join(buf))
            continue

        if line.strip().startswith("|") and i + 1 < len(lines) and _SEP.match(lines[i + 1]):
            close_list()
            head = _split_row(line)
            aligns = _alignments(lines[i + 1])
            i += 2
            body = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                body.append(_split_row(lines[i]))
                i += 1
            cells = []
            for idx, cell in enumerate(head):
                style = ' style="text-align:%s"' % aligns[idx] if idx < len(aligns) else ""
                cells.append("<th%s>%s</th>" % (style, _inline(cell)))
            html = "<table><thead><tr>" + "".join(cells) + "</tr></thead><tbody>"
            for row in body:
                cells = []
                for idx, cell in enumerate(row):
                    style = (' style="text-align:%s"' % aligns[idx]
                             if idx < len(aligns) else "")
                    cells.append("<td%s>%s</td>" % (style, _inline(cell)))
                html += "<tr>" + "".join(cells) + "</tr>"
            out.append(html + "</tbody></table>")
            continue

        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading:
            close_list()
            level = len(heading.group(1))
            out.append("<h%d>%s</h%d>" % (level, _inline(heading.group(2)), level))
            i += 1
            continue

        if re.match(r"^\s*([-*_])\s*\1\s*\1[\s\-*_]*$", line):
            close_list()
            out.append("<hr>")
            i += 1
            continue

        if re.match(r"^\s*>\s?", line):
            close_list()
            quote = []
            while i < len(lines) and re.match(r"^\s*>\s?", lines[i]):
                quote.append(_inline(re.sub(r"^\s*>\s?", "", lines[i])))
                i += 1
            out.append("<blockquote>%s</blockquote>" % "<br>".join(quote))
            continue

        ul = re.match(r"^\s*[-*+]\s+(.*)$", line)
        ol = re.match(r"^\s*\d+[.)]\s+(.*)$", line)
        if ul or ol:
            want = "ul" if ul else "ol"
            if list_type and list_type != want:
                close_list()
            if not list_type:
                list_type = want
                out.append("<%s>" % want)
            out.append("<li>%s</li>" % _inline(ul.group(1) if ul else ol.group(1)))
            i += 1
            continue

        if not line.strip():
            close_list()
            i += 1
            continue

        close_list()
        out.append("<p>%s</p>" % _inline(line))
        i += 1

    close_list()
    return "\n".join(out)


def split_report(markdown: str):
    """把报告拆成（标题, 正文, 封面元信息）。

    报告的元信息原本是正文开头的一段引用块。若原样保留，PDF 里会与封面重复，
    因此这里把它提到封面上，正文从"执行摘要"开始。
    """
    lines = str(markdown or "").replace("\r\n", "\n").split("\n")
    title = "财务分析报告"
    idx = 0
    if lines and lines[0].startswith("# "):
        title = lines[0][2:].strip()
        idx = 1

    j = idx
    while j < len(lines) and not lines[j].strip():
        j += 1
    k = j
    meta = []
    while k < len(lines) and lines[k].lstrip().startswith(">"):
        raw = lines[k].lstrip()[1:].strip()
        if raw:
            meta.append(raw)
        k += 1
    if meta:
        idx = k
    # 去掉标题与元信息之后残留的空行，避免 PDF 正文第一页顶部空一大块
    body = "\n".join(lines[idx:]).lstrip("\n")
    return title, body, meta


def format_meta(meta_list, limit: int = 4) -> list[str]:
    """将报告元信息行整理成适合封面的 HTML 片段。"""
    out = []
    for raw in meta_list[:limit]:
        text = re.sub(r"`([^`]*)`", r"\1", raw)
        parts = [x.strip() for x in re.split(r"[|｜]", text) if x.strip()]
        chunks = []
        for part in parts:
            if "：" in part:
                key, val = part.split("：", 1)
                chunks.append("<b>%s</b> %s" % (_esc(key.strip()), _esc(val.strip())))
            else:
                chunks.append(_esc(part))
        if chunks:
            out.append("　|　".join(chunks))
    return out


# ---------------------------------------------------------------- HTML 模板

_PRINT_CSS = """
@page { size: A4; margin: 15mm 13mm 16mm 13mm; }
* { box-sizing: border-box; }
body {
  margin: 0; color: #14161a; background: #fff;
  font-family: "Microsoft YaHei", "PingFang SC", "Hiragino Sans GB",
               "Source Han Sans SC", "Noto Sans CJK SC", "Segoe UI", sans-serif;
  font-size: 10.5pt; line-height: 1.68;
  -webkit-print-color-adjust: exact; print-color-adjust: exact;
}
.cover { border-bottom: 2.5pt solid #2f5bd7; padding-bottom: 10pt; margin-bottom: 16pt; }
.cover .eyebrow { font-size: 8.5pt; letter-spacing: .22em; color: #2f5bd7; font-weight: 700; }
.cover h1 { font-size: 19pt; margin: 6pt 0 8pt; line-height: 1.3; border: none; padding: 0; }
.cover .meta { font-size: 9pt; color: #5b6472; line-height: 1.75; }
.cover .meta b { color: #14161a; font-weight: 600; }
h1, h2, h3, h4, h5, h6 { line-height: 1.35; page-break-after: avoid; break-after: avoid; }
h1 { font-size: 15pt; margin: 20pt 0 9pt; padding-bottom: 4pt; border-bottom: 1pt solid #dfe3ea; }
h2 { font-size: 13pt; margin: 18pt 0 8pt; padding-left: 8pt; border-left: 3.5pt solid #2f5bd7; }
h3 { font-size: 11.5pt; margin: 14pt 0 6pt; }
h4 { font-size: 10.5pt; margin: 12pt 0 5pt; color: #333c4d; }
p { margin: 5pt 0; text-align: justify; }
strong { font-weight: 700; }
code {
  font-family: Consolas, "Cascadia Mono", "SF Mono", Menlo, monospace;
  font-size: 9pt; background: #f1f3f7; border-radius: 3px; padding: 0.5pt 3pt;
  color: #1d3fa8; word-break: break-all;
}
pre { background: #f6f8fb; border: 1pt solid #e3e7ee; border-radius: 5px;
      padding: 8pt 10pt; overflow-x: auto; page-break-inside: avoid; }
pre code { background: none; padding: 0; color: #1d2433; }
table { width: 100%; border-collapse: collapse; margin: 8pt 0 12pt;
        font-size: 8.8pt; page-break-inside: auto; }
thead { display: table-header-group; }
tr { page-break-inside: avoid; }
th { background: #eef2fb; color: #1b2a52; font-weight: 700; }
th, td { border: 0.75pt solid #d8dde6; padding: 3.6pt 5pt; vertical-align: top; }
tbody tr:nth-child(even) { background: #fafbfd; }
blockquote { margin: 7pt 0; padding: 7pt 11pt; background: #f7f9fc;
             border-left: 3pt solid #b9c6e2; color: #3d4657; font-size: 9.5pt; }
blockquote br { line-height: 1.7; }
ul, ol { margin: 5pt 0; padding-left: 18pt; }
li { margin: 2.5pt 0; }
hr { border: none; border-top: 0.75pt solid #e2e6ed; margin: 13pt 0; }
a { color: #1d3fa8; text-decoration: none; }
.foot { margin-top: 20pt; padding-top: 8pt; border-top: 0.75pt solid #dfe3ea;
        font-size: 8.2pt; color: #77808f; line-height: 1.7; }
.foot b { color: #4a5364; }
"""

_DISCLAIMER = (
    "本报告由 FinAgent 依据公开披露信息自动生成，全部数值均由程序计算并绑定证据 ID，"
    "可回溯至原始披露文件。报告已尽力区分<strong>事实</strong>、<strong>推论</strong>与"
    "<strong>观点</strong>，其中推论与观点不构成任何投资建议。"
    "使用者应结合自身判断独立决策，并自行承担相应风险。"
)


def build_document(title: str, meta_lines: list[str], body_html: str,
                   footer_note: str = "") -> str:
    meta = "".join("<div>%s</div>" % line for line in meta_lines)
    foot = footer_note or ""
    return (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<title>%s</title><style>%s</style></head><body>"
        "<div class=\"cover\"><div class=\"eyebrow\">FINAGENT 财务报告分析</div>"
        "<h1>%s</h1><div class=\"meta\">%s</div></div>"
        "%s"
        "<div class=\"foot\">%s%s</div>"
        "</body></html>"
    ) % (_esc(title), _PRINT_CSS, _esc(title), meta, body_html, foot, _DISCLAIMER)


# ---------------------------------------------------------------- 打印

def html_to_pdf(document: str, timeout: int = 90) -> bytes | None:
    """把完整 HTML 文档打印成 PDF。找不到浏览器或打印失败时返回 None。"""
    browser = find_browser()
    if not browser:
        return None

    workdir = tempfile.mkdtemp(prefix="finagent-pdf-")
    src = os.path.join(workdir, "report.html")
    dst = os.path.join(workdir, "report.pdf")
    with open(src, "w", encoding="utf-8", newline="") as fh:
        fh.write(document)

    from urllib.parse import quote
    url = "file:///" + quote(os.path.abspath(src).replace("\\", "/"), safe="/:")

    cmd = [
        browser,
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--no-pdf-header-footer",
        "--user-data-dir=" + os.path.join(workdir, "profile"),
        "--virtual-time-budget=8000",
        "--print-to-pdf=" + dst,
        url,
    ]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None

    if not os.path.isfile(dst):
        return None
    with open(dst, "rb") as fh:
        data = fh.read()

    _cleanup(workdir)
    return data or None


def _cleanup(path: str) -> None:
    """删除临时目录。策略上禁止 shell 递归删除，这里用 Python 逐个删。"""
    for root, dirs, files in os.walk(path, topdown=False):
        for name in files:
            try:
                os.remove(os.path.join(root, name))
            except OSError:
                pass
        for name in dirs:
            try:
                os.rmdir(os.path.join(root, name))
            except OSError:
                pass
    try:
        os.rmdir(path)
    except OSError:
        pass
