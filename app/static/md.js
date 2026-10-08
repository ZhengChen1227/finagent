/* 极简 Markdown 渲染器：只覆盖报告实际用到的语法，避免引入第三方库。
   支持：# 标题、**粗体**、`代码`、[链接](url)、- / 1. 列表、> 引用、--- 分割线、
        围栏代码块、管道表格。
   安全：所有文本先做 HTML 转义，再插入标签。 */

(function (global) {
  "use strict";

  function esc(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function inline(s) {
    s = esc(s);
    s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
                  '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
    return s;
  }

  function splitRow(line) {
    var t = line.trim();
    if (t.charAt(0) === "|") { t = t.slice(1); }
    if (t.charAt(t.length - 1) === "|") { t = t.slice(0, -1); }
    return t.split("|").map(function (c) { return c.trim(); });
  }

  var SEP = /^\s*\|[\s:\-|]+\|\s*$/;

  function render(md) {
    var lines = String(md || "").replace(/\r\n/g, "\n").split("\n");
    var out = [];
    var i = 0;
    var listType = null;

    function closeList() {
      if (listType) { out.push("</" + listType + ">"); listType = null; }
    }

    while (i < lines.length) {
      var line = lines[i];

      /* 围栏代码块 */
      if (/^\s*```/.test(line)) {
        closeList();
        var buf = [];
        i++;
        while (i < lines.length && !/^\s*```/.test(lines[i])) {
          buf.push(esc(lines[i]));
          i++;
        }
        i++;
        out.push("<pre><code>" + buf.join("\n") + "</code></pre>");
        continue;
      }

      /* 管道表格 */
      if (/^\s*\|/.test(line) && i + 1 < lines.length && SEP.test(lines[i + 1])) {
        closeList();
        var head = splitRow(line);
        i += 2;
        var body = [];
        while (i < lines.length && /^\s*\|/.test(lines[i])) {
          body.push(splitRow(lines[i]));
          i++;
        }
        var html = "<table><thead><tr>" +
          head.map(function (c) { return "<th>" + inline(c) + "</th>"; }).join("") +
          "</tr></thead><tbody>";
        html += body.map(function (r) {
          return "<tr>" + r.map(function (c) { return "<td>" + inline(c) + "</td>"; }).join("") + "</tr>";
        }).join("");
        out.push(html + "</tbody></table>");
        continue;
      }

      /* 标题 */
      var h = line.match(/^(#{1,6})\s+(.*)$/);
      if (h) {
        closeList();
        var lvl = h[1].length;
        out.push("<h" + lvl + ">" + inline(h[2]) + "</h" + lvl + ">");
        i++;
        continue;
      }

      /* 分割线 */
      if (/^\s*([-*_])\s*\1\s*\1[\s\-*_]*$/.test(line)) {
        closeList();
        out.push("<hr>");
        i++;
        continue;
      }

      /* 引用块 */
      if (/^\s*>\s?/.test(line)) {
        closeList();
        var quote = [];
        while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
          quote.push(inline(lines[i].replace(/^\s*>\s?/, "")));
          i++;
        }
        out.push("<blockquote>" + quote.join("<br>") + "</blockquote>");
        continue;
      }

      /* 列表 */
      var ul = line.match(/^\s*[-*+]\s+(.*)$/);
      var ol = line.match(/^\s*\d+[.)]\s+(.*)$/);
      if (ul || ol) {
        var want = ul ? "ul" : "ol";
        if (listType && listType !== want) { closeList(); }
        if (!listType) { listType = want; out.push("<" + want + ">"); }
        out.push("<li>" + inline(ul ? ul[1] : ol[1]) + "</li>");
        i++;
        continue;
      }

      /* 空行 */
      if (!line.trim()) { closeList(); i++; continue; }

      /* 普通段落 */
      closeList();
      out.push("<p>" + inline(line) + "</p>");
      i++;
    }

    closeList();
    return out.join("\n");
  }

  global.FinMD = { render: render, escape: esc };
})(window);
