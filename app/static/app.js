/* ==========================================================================
   FinAgent 界面逻辑
   与后端的契约只有三个：GET /api/status、GET /api/universe、POST /api/run(SSE)。
   界面不做任何财务计算：指标与结论全部来自 run.py 的产物，
   界面只负责把参数翻译成命令行、把输出渲染出来。
   ========================================================================== */

(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var esc = function (s) { return FinMD.escape(s); };

  var state = {
    action: "analyze",
    targets: [],
    reports: [],
    activeReport: null,
    activeView: "md",
    traces: [],
    activeTrace: null,
    traceEvents: [],
    traceFilter: "all",
    running: false,
    suggest: [],
    suggestIndex: -1
  };

  var MARKET_LABEL = { sh: "沪", sz: "深", bj: "北", nq: "北" };
  var TABS = ["run", "report", "trace", "env"];

  function fmtBytes(n) {
    if (!n && n !== 0) return "—";
    if (n < 1024) return n + " B";
    if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
    if (n < 1073741824) return (n / 1048576).toFixed(1) + " MB";
    return (n / 1073741824).toFixed(2) + " GB";
  }

  function fmtTime(ts) {
    var d = new Date(ts * 1000);
    var p = function (v) { return (v < 10 ? "0" : "") + v; };
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) +
           " " + p(d.getHours()) + ":" + p(d.getMinutes());
  }

  function yi(v) {
    if (v === null || v === undefined || v === "" || isNaN(v)) return "—";
    var n = Number(v) / 1e8;
    var sign = n < 0 ? "-" : "";
    return sign + Math.abs(n).toLocaleString("zh-CN", { maximumFractionDigits: 2 }) + "亿";
  }

  /* ------------------------------------------------------------ 主题 */

  function applyTheme(theme, persist) {
    document.documentElement.setAttribute("data-theme", theme);
    if (persist === false) return;      // URL 强制指定的主题不写入本地偏好
    try { localStorage.setItem("finagent-theme", theme); } catch (e) {}
  }

  function initTheme() {
    var saved = null;
    try { saved = localStorage.getItem("finagent-theme"); } catch (e) {}
    /* 允许用 ?theme=light 强制主题：现场演示与截图核对时不必先点一次按钮 */
    var forced = (location.search.match(/[?&]theme=(dark|light)/) || [])[1];
    applyTheme(forced || saved || "dark", !forced);
    $("theme-toggle").addEventListener("click", function () {
      applyTheme(document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark");
    });
  }

  /* ------------------------------------------------------------ 标签页 */

  function showTab(name) {
    if (TABS.indexOf(name) < 0) name = "run";
    TABS.forEach(function (t) {
      var on = t === name;
      $("tab-" + t).setAttribute("aria-selected", on ? "true" : "false");
      $("page-" + t).classList.toggle("active", on);
    });
    if (location.hash.slice(1) !== name) history.replaceState(null, "", "#" + name);
  }

  function initTabs() {
    TABS.forEach(function (t) {
      $("tab-" + t).addEventListener("click", function () { showTab(t); });
    });
    window.addEventListener("hashchange", function () { showTab(location.hash.slice(1)); });
    showTab(location.hash.slice(1) || "run");
  }

  /* ------------------------------------------------------------ 状态栏 */

  function renderStatus(status) {
    var total = status.universe_total || 0;
    $("pill-universe").innerHTML = '<i class="dot"></i>全市场名录 <strong>' +
      total.toLocaleString("zh-CN") + "</strong> 只";

    $("pill-model").innerHTML = '<i class="dot"></i>' + esc(status.model || "—");
    $("pill-model").title = "接口：" + (status.base_url || "");

    var key = $("pill-key");
    key.className = "pill " + (status.api_key_set ? "is-ok" : "is-warn");
    key.innerHTML = '<i class="dot"></i>' + (status.api_key_set ? "密钥已配置" : "密钥未配置");
    key.title = status.api_key_set
      ? "分析将由大模型完成归因推理"
      : "未配置密钥时分析回落到离线归因；规则引擎与计算结果不受影响";

    setBusy(status.busy);

    if (!state.targets.length && status.companies && status.companies.length) {
      status.companies.forEach(function (c) { addTarget(c.code, c.name, true); });
      renderTargets();
    }

    renderSamples(status.coverage_samples || []);
    renderCorpus(status.corpus || []);
    renderSidebarFoot(status);
    renderReports(status.reports || []);
    renderTraces(status.traces || []);
    renderEnv(status);
    renderThresholds(status.thresholds || {});
  }

  function setBusy(busy) {
    var pill = $("pill-busy");
    pill.className = "pill " + (busy ? "is-busy" : "");
    pill.innerHTML = '<i class="dot"></i>' + (busy ? "正在运行" : "空闲");
    $("go").disabled = busy || state.running;
  }

  /* ------------------------------------------------------------ 侧栏摘要 */

  function renderSidebarFoot(status) {
    var corpus = status.corpus || [];
    var bytes = corpus.reduce(function (a, c) { return a + (c.bytes || 0); }, 0);
    var chunks = status.index ? status.index.chunks : 0;
    var rows = [
      ["全市场名录", (status.universe_total || 0).toLocaleString("zh-CN") + " 只", true],
      ["推理模型", esc(status.model || "—"), true],
      ["密钥", status.api_key_set ? "已配置" : "未配置（离线归因）", false],
      ["语料规模", corpus.length + " 家 · " + fmtBytes(bytes), false],
      ["索引文本块", chunks.toLocaleString("zh-CN"), true]
    ];
    $("sidebar-foot").innerHTML = rows.map(function (r) {
      return '<div class="sf"><span class="sf-k">' + r[0] + '</span>' +
             '<span class="sf-v' + (r[2] ? " mono" : "") + '">' + r[1] + "</span></div>";
    }).join("");
  }

  /* ------------------------------------------------------------ 覆盖样本 */

  function renderSamples(samples) {
    var box = $("samples");
    if (!samples.length) { box.innerHTML = '<span class="empty-inline">暂无样本</span>'; return; }
    var groups = {};
    samples.forEach(function (s) { (groups[s.segment] = groups[s.segment] || []).push(s); });
    box.innerHTML = Object.keys(groups).map(function (seg) {
      var items = groups[seg].map(function (s) {
        return '<button type="button" class="sample" data-code="' + esc(s.code) +
               '" data-name="' + esc(s.name) + '">' + esc(s.name) +
               '<span class="s-code">' + esc(String(s.code).split(".")[0]) + "</span></button>";
      }).join("");
      return '<div class="sample-group"><span>' + esc(seg) + "</span>" + items + "</div>";
    }).join("");
    box.querySelectorAll(".sample").forEach(function (btn) {
      btn.addEventListener("click", function () {
        if (state.action !== "analyze") setAction("analyze");
        addTarget(btn.dataset.code, btn.dataset.name);
        renderTargets();
      });
    });
  }

  /* ------------------------------------------------------------ 标的选择 */

  function addTarget(code, name, silent) {
    if (!code) return false;
    if (state.targets.some(function (t) { return t.code === code; })) return false;
    state.targets.push({ code: code, name: name || code });
    if (!silent) renderTargets();
    return true;
  }

  function renderTargets() {
    var box = $("targets");
    if (!state.targets.length) {
      box.innerHTML = '<span class="empty-inline">尚未选择标的，可从上方检索或使用下方快捷样本。</span>';
      $("clear-targets").hidden = true;
      return;
    }
    $("clear-targets").hidden = false;
    box.innerHTML = state.targets.map(function (t, i) {
      return '<span class="chip-target">' + esc(t.name) +
             '<span class="c-code">' + esc(t.code) + "</span>" +
             '<button type="button" class="c-x" data-i="' + i +
             '" aria-label="移除 ' + esc(t.name) + '">×</button></span>';
    }).join("");
    box.querySelectorAll(".c-x").forEach(function (btn) {
      btn.addEventListener("click", function () {
        state.targets.splice(Number(btn.dataset.i), 1);
        renderTargets();
      });
    });
  }

  function initTargets() {
    $("clear-targets").addEventListener("click", function () {
      state.targets = [];
      renderTargets();
    });
  }

  /* ------------------------------------------------------------ 公司检索 */

  var searchTimer = null;

  /* 与后端 finagent/datasource/codes.py 的前缀规则保持一致。
     只用于「用户直接敲回车、未走名录下拉」时的兜底归一化。 */
  function suffixOf(code) {
    if (/^900/.test(code)) return ".SH";     // 沪市 B 股
    if (/^92/.test(code)) return ".BJ";      // 北交所新代码段
    if (/^6/.test(code)) return ".SH";       // 沪市主板 / 科创板
    if (/^[023]/.test(code)) return ".SZ";   // 深市主板 / B 股 / 创业板
    if (/^[48]/.test(code)) return ".BJ";    // 北交所（原精选层）
    if (/^9/.test(code)) return ".SH";       // 其余 9 开头归沪市
    return ".SZ";
  }

  function renderSuggest() {
    var box = $("suggest");
    box.hidden = false;
    if (!state.suggest.length) {
      box.innerHTML = '<div class="suggest-empty">未匹配到主体。可直接输入 6 位代码后回车。</div>';
      return;
    }
    box.innerHTML = state.suggest.map(function (s, i) {
      return '<div class="suggest-item' + (i === state.suggestIndex ? " active" : "") +
             '" role="option" data-i="' + i + '">' +
             '<span class="s-name">' + esc(s.name) + "</span>" +
             '<span class="s-code">' + esc(s.secucode) + "</span>" +
             '<span class="s-market">' + esc(MARKET_LABEL[s.market] || s.market || "") +
             "</span></div>";
    }).join("");
    box.querySelectorAll(".suggest-item").forEach(function (el) {
      el.addEventListener("mousedown", function (ev) {
        ev.preventDefault();
        pickSuggest(Number(el.dataset.i));
      });
    });
  }

  function pickSuggest(i) {
    var s = state.suggest[i];
    if (!s) return;
    addTarget(s.secucode, s.name);
    renderTargets();
    $("company-search").value = "";
    state.suggest = [];
    state.suggestIndex = -1;
    $("suggest").hidden = true;
  }

  function doSearch(q) {
    fetch("/api/universe?q=" + encodeURIComponent(q))
      .then(function (r) { return r.json(); })
      .then(function (j) {
        state.suggest = (j.items || []).filter(function (s) { return s.code; });
        state.suggestIndex = state.suggest.length ? 0 : -1;
        renderSuggest();
      })
      .catch(function () { $("suggest").hidden = true; });
  }

  function initSearch() {
    var input = $("company-search");
    var box = $("suggest");

    input.addEventListener("input", function () {
      var q = input.value.trim();
      clearTimeout(searchTimer);
      if (!q) { state.suggest = []; box.hidden = true; return; }
      searchTimer = setTimeout(function () { doSearch(q); }, 170);
    });

    input.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") { box.hidden = true; return; }
      if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
        if (!state.suggest.length) return;
        ev.preventDefault();
        var step = ev.key === "ArrowDown" ? 1 : -1;
        state.suggestIndex = (state.suggestIndex + step + state.suggest.length) % state.suggest.length;
        renderSuggest();
        return;
      }
      if (ev.key === "Enter") {
        ev.preventDefault();
        if (state.suggestIndex >= 0 && state.suggest.length) { pickSuggest(state.suggestIndex); return; }
        var text = input.value.trim();
        if (!text) return;
        var code = /^\d{5,6}$/.test(text) ? text.padStart(6, "0") + suffixOf(text.padStart(6, "0")) : text;
        addTarget(code, text);
        renderTargets();
        input.value = "";
        box.hidden = true;
      }
    });

    document.addEventListener("click", function (ev) {
      if (!ev.target.closest(".search-shell")) box.hidden = true;
    });
  }

  /* ------------------------------------------------------------ 动作切换 */

  function setAction(action) {
    state.action = action;
    document.querySelectorAll(".segmented .seg").forEach(function (b) {
      b.classList.toggle("active", b.dataset.action === action);
    });
    $("params-analyze").hidden = action !== "analyze";
    $("params-fetch").hidden = action !== "fetch";
    $("params-index").hidden = action !== "index";
  }

  function initActions() {
    document.querySelectorAll(".segmented .seg").forEach(function (b) {
      b.addEventListener("click", function () { setAction(b.dataset.action); });
    });
  }

  /* ------------------------------------------------------------ 日志渲染 */

  function classify(line) {
    if (/^\s*\[FinAgent\]|^\[抓取\]|^\[索引\]|^\[提示\]|^\$ /.test(line)) return "l-head";
    if (/^执行轨迹|^运行编号|全部通过/.test(line)) return "l-ok";
    if (/Traceback|\[错误\]|\[警告\]|失败|Error/.test(line)) return "l-bad";
    if (/^\s*\[\d+\]/.test(line)) return "l-tool";
    if (/规则|异常信号|结构性|口径提示/.test(line)) return "l-rule";
    if (/公告原文|命中缓存|落盘|\.pdf|\.json|\.csv|\.md/.test(line)) return "l-file";
    return "l-dim";
  }

  function resetLog() {
    var log = $("log");
    log.innerHTML = "";
    log.dataset.cleared = "1";
  }

  function appendLog(text, cls) {
    var log = $("log");
    if (log.dataset.cleared !== "1") resetLog();
    var span = document.createElement("span");
    span.className = cls || classify(text);
    span.textContent = text + "\n";
    log.appendChild(span);
    log.scrollTop = log.scrollHeight;
  }

  function appendRaw(html) {
    var log = $("log");
    if (log.dataset.cleared !== "1") resetLog();
    var div = document.createElement("div");
    div.innerHTML = html;
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
  }

  function oneLine(obj, max) {
    var text;
    try { text = JSON.stringify(obj); } catch (e) { text = String(obj); }
    if (!text || text === "{}" || text === "null") return "";
    max = max || 150;
    return text.length > max ? text.slice(0, max) + "…" : text;
  }

  function traceLine(ev) {
    var kind = ev.event;
    if (kind === "tool_call") {
      return ["l-tool", "  ⚙ " + ev.tool + "(" + oneLine(ev.inputs, 90) + ")" +
              (ev.outputs ? " → " + oneLine(ev.outputs, 130) : "")];
    }
    if (kind === "file_access") {
      var p = String(ev.path || "").replace(/\\/g, "/");
      var idx = p.lastIndexOf("/");
      return ["l-file", "  ▤ " + (idx >= 0 ? p.slice(idx + 1) : p) + "  [" + ev.mode + "]" +
              (ev.note ? "  " + ev.note : "")];
    }
    if (kind === "compute") return ["l-dim", "  ƒ " + ev.name + " = " + oneLine(ev.output, 90)];
    if (kind === "finding") return ["l-rule", "  ▲ 结论 " + oneLine(ev, 170)];
    if (kind === "rule_error") return ["l-bad", "  ! 规则异常 " + ev.rule];
    if (kind === "result") return ["l-ok", "  ✔ " + oneLine(ev, 180)];
    if (kind === "target") return ["l-head", "  目标 " + ev.secucode + " " + (ev.name || "")];
    if (kind === "skip") return ["l-bad", "  跳过 " + ev.secucode + "：" + (ev.reason || "")];
    return null;
  }

  /* ------------------------------------------------------------ 运行 */

  function initRun() {
    $("go").addEventListener("click", run);
    $("clear-log").addEventListener("click", function () {
      $("log").innerHTML = '<span class="l-dim">已清空。</span>';
      $("log").dataset.cleared = "1";
      $("result-bar").innerHTML = "";
    });
  }

  function collectParams() {
    if (state.action === "analyze") {
      if (!state.targets.length) {
        var typed = $("company-search").value.trim();
        if (typed) { addTarget(/^\d+$/.test(typed) ? typed : typed, typed); renderTargets(); }
      }
      return {
        codes: state.targets.map(function (t) { return t.code; }),
        since_year: Number($("since-year").value || 2025),
        periods: Number($("periods").value || 6),
        refresh: $("refresh").checked,
        quiet: $("quiet").checked
      };
    }
    if (state.action === "fetch") {
      return { code: $("fetch-code").value.trim(), limit: Number($("limit").value || 3) };
    }
    return { force: $("force").checked };
  }

  function run() {
    if (state.running) return;
    var params = collectParams();
    params.action = state.action;

    if (state.action === "analyze" && !params.codes.length) {
      $("result-bar").innerHTML = '<span class="badge bad">请先选择或输入分析标的</span>';
      return;
    }
    if (state.action === "fetch" && !params.code) {
      $("result-bar").innerHTML = '<span class="badge bad">请填写抓取对象</span>';
      return;
    }

    state.running = true;
    $("go").disabled = true;
    setRunState("running", "正在运行");
    resetLog();
    $("result-bar").innerHTML = "";
    $("run-cmd").textContent = "正在构造命令…";

    fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(params)
    }).then(function (resp) {
      if (!resp.ok) {
        return resp.json().then(function (j) { throw new Error(j.error || ("HTTP " + resp.status)); });
      }
      return readStream(resp);
    }).catch(function (err) {
      appendLog("[错误] " + err.message, "l-bad");
      setRunState("bad", "运行失败");
      $("result-bar").innerHTML = '<span class="badge bad">' + esc(err.message) + "</span>";
    }).then(function () {
      state.running = false;
      $("go").disabled = false;
      refreshStatus();
    });
  }

  function readStream(resp) {
    var reader = resp.body.getReader();
    var decoder = new TextDecoder("utf-8");
    var buffer = "";

    function pump() {
      return reader.read().then(function (res) {
        if (res.done) {
          if (buffer.trim()) handleChunk(buffer);
          return;
        }
        buffer += decoder.decode(res.value, { stream: true });
        var parts = buffer.split("\n\n");
        buffer = parts.pop();
        parts.forEach(handleChunk);
        return pump();
      });
    }
    return pump();
  }

  function handleChunk(chunk) {
    var payload = null;
    chunk.split("\n").forEach(function (line) {
      if (line.indexOf("data: ") === 0) {
        try { payload = JSON.parse(line.slice(6)); } catch (e) {}
      }
    });
    if (!payload) return;
    var kind = payload.kind;

    if (kind === "start") {
      $("run-cmd").textContent = payload.command;
      $("run-cmd").title = payload.command + "\n工作目录：" + payload.cwd;
      appendLog("$ " + payload.command, "l-head");
      appendLog("工作目录 " + payload.cwd, "l-dim");
      return;
    }
    if (kind === "line") { appendLog(payload.text); return; }
    if (kind === "trace") {
      var rendered = traceLine(payload.event || {});
      if (rendered) {
        appendRaw('<span class="' + rendered[0] + '">' + esc(rendered[1]) + "</span>");
      }
      return;
    }
    if (kind === "error") { appendLog("[错误] " + payload.message, "l-bad"); return; }
    if (kind === "done") {
      var ok = payload.code === 0;
      setRunState(ok ? "ok" : "bad", ok ? "运行完成" : ("运行失败（退出码 " + payload.code + "）"));
      var bits = ['<span class="badge ' + (ok ? "ok" : "bad") + '">' +
                  (ok ? "完成" : "退出码 " + payload.code) + "</span>",
                  '<span class="badge plain">耗时 ' + payload.seconds + " 秒</span>"];
      if (payload.run_id) {
        bits.push('<span class="badge plain">运行编号 ' + esc(payload.run_id) + "</span>");
      }
      var reports = payload.reports || [];
      if (reports.length) {
        bits.push('<span class="badge ok">新生成 ' + reports.length + " 份产物</span>");
        reports.forEach(function (r) {
          bits.push('<button type="button" class="btn-ghost js-open-report" data-path="' +
                    esc(r.path) + '">打开 ' + esc(r.name) + "</button>");
        });
      }
      $("result-bar").innerHTML = bits.join("");
      $("result-bar").querySelectorAll(".js-open-report").forEach(function (b) {
        b.addEventListener("click", function () {
          showTab("report");
          setTimeout(function () { openReportByPath(b.dataset.path); }, 240);
        });
      });
      return;
    }
  }

  function setRunState(kind, text) {
    var el = $("run-state");
    el.dataset.state = kind;
    el.innerHTML = '<i class="dot"></i>' + esc(text);
  }

  /* ------------------------------------------------------------ 公告原文 */

  function renderCorpus(corpus) {
    var box = $("corpus-list");
    var docs = 0, bytes = 0;
    corpus.forEach(function (c) { docs += c.documents || 0; bytes += c.bytes || 0; });
    $("corpus-sub").textContent = corpus.length
      ? corpus.length + " 家公司 · " + docs + " 份 · " + fmtBytes(bytes)
      : "封闭数据环境内容";
    if (!corpus.length) {
      box.innerHTML = '<div class="empty">尚未抓取公告原文。可切换左侧「抓取公告」构建封闭数据环境。</div>';
      return;
    }
    box.innerHTML = corpus.map(function (c) {
      return '<div class="item"><div class="t">' + esc(c.code) + "</div>" +
             '<div class="m">' + c.documents + " 份 · " + fmtBytes(c.bytes) +
             (c.kinds && c.kinds.length ? " · " + esc(c.kinds.join("/")) : "") + "</div></div>";
    }).join("");
  }

  /* ------------------------------------------------------------ 报告 */

  function renderReports(reports) {
    state.reports = reports;
    var box = $("report-list");
    if (!reports.length) {
      box.innerHTML = '<div class="empty">尚无报告。先在「分析工作台」运行一次财务分析。</div>';
      return;
    }
    box.innerHTML = reports.map(function (r) {
      var on = state.activeReport && state.activeReport.path === r.path ? " active" : "";
      return '<div class="item' + on + '" data-path="' + esc(r.path) + '">' +
             '<div class="t">' + esc(r.name) + "</div>" +
             '<div class="m">' + fmtTime(r.mtime) + " · " + fmtBytes(r.bytes) + "</div></div>";
    }).join("");
    box.querySelectorAll(".item").forEach(function (el) {
      el.addEventListener("click", function () { openReportByPath(el.dataset.path); });
    });
    if (!state.activeReport && reports.length) {
      /* 默认选中最新一份有实质内容的报告，避免一进页面就看到几百字节的摘要 */
      var substantial = reports.filter(function (r) { return (r.bytes || 0) >= 4096; });
      openReportByPath((substantial[0] || reports[0]).path);
    }
  }

  function openReportByPath(path) {
    var meta = state.reports.filter(function (r) { return r.path === path; })[0];
    if (!meta) return;
    state.activeReport = meta;
    document.querySelectorAll("#report-list .item").forEach(function (n) {
      n.classList.toggle("active", n.dataset.path === path);
    });
    $("report-title").dataset.state = "ok";
    $("report-title").innerHTML = '<i class="dot"></i>' + esc(meta.name);

    $("report-body").innerHTML = '<div class="empty"><span class="spin"></span> 载入中…</div>';
    fetch("/api/report?path=" + encodeURIComponent(path))
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (!j.ok) throw new Error(j.error || "读取失败");
        $("report-body").innerHTML = FinMD.render(j.markdown);
      })
      .catch(function (e) {
        $("report-body").innerHTML = '<div class="empty">读取失败：' + esc(e.message) + "</div>";
      });

    if (meta.table) {
      loadMetrics(meta.table);
      loadTable(meta.table);
    } else {
      $("metrics").hidden = true;
      $("metrics").innerHTML = "";
      $("table-body").innerHTML = '<div class="empty">该报告没有对应的指标宽表。</div>';
    }
    setReportView("md");
  }

  function setReportView(view) {
    state.activeView = view;
    document.querySelectorAll(".view-switch .seg").forEach(function (b) {
      b.classList.toggle("active", b.dataset.view === view);
    });
    $("report-body").hidden = view !== "md";
    $("table-body").hidden = view !== "table";
  }

  function initReportView() {
    document.querySelectorAll(".view-switch .seg").forEach(function (b) {
      b.addEventListener("click", function () { setReportView(b.dataset.view); });
    });
  }

  function label(p) { return p.y + "Q" + p.q; }

  function ratioText(v) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    return Number(v).toFixed(2) + "×";
  }

  function fetchTable(tablePath) {
    return fetch("/api/table?path=" + encodeURIComponent(tablePath)).then(function (r) { return r.json(); });
  }

  function columnIndex(header) {
    var idx = {};
    (header || []).forEach(function (h, i) { if (h) idx[h] = i; });
    return idx;
  }

  /* 关键指标卡 + 走势：全部取自指标宽表，界面不重算任何数字 */
  function loadMetrics(tablePath) {
    fetchTable(tablePath).then(function (j) {
      if (!j.ok || !j.rows || !j.rows.length) { $("metrics").hidden = true; return; }
      var idx = columnIndex(j.header);
      var Y = 0, Q = 1;
      var pick = function (row, key) {
        if (!(key in idx)) return null;
        var v = row[idx[key]];
        return v === "" || v === undefined ? null : Number(v);
      };
      var lastRow = j.rows[j.rows.length - 1];
      var last = { y: lastRow[Y], q: lastRow[Q] };
      var tail = j.rows.slice(-6);
      var series = function (key) {
        return tail.map(function (r) { return pick(r, key); });
      };

      /* 同比：同一报告期的上年同期，口径对齐后再比，避免混用累计数 */
      var yoy = function (key) {
        var lastYear = String(Number(last.y) - 1);
        var lastQ = String(last.q);
        for (var i = j.rows.length - 1; i >= 0; i--) {
          if (String(j.rows[i][Y]) !== lastYear || String(j.rows[i][Q]) !== lastQ) continue;
          var cur = pick(lastRow, key), prev = pick(j.rows[i], key);
          if (cur === null || prev === null || prev === 0) return null;
          return (cur - prev) / Math.abs(prev) * 100;
        }
        return null;
      };

      var cards = [
        { label: "营业总收入", value: yi(pick(lastRow, "revenue")),
          foot: label(last) + " 累计", series: series("revenue"), delta: yoy("revenue") },
        { label: "归母净利润", value: yi(pick(lastRow, "parent_net_profit")),
          foot: label(last) + " 累计", neg: (pick(lastRow, "parent_net_profit") || 0) < 0,
          series: series("parent_net_profit"), delta: yoy("parent_net_profit") },
        { label: "经营活动现金流净额", value: yi(pick(lastRow, "netcash_operate")),
          foot: label(last) + " 累计", neg: (pick(lastRow, "netcash_operate") || 0) < 0,
          series: series("netcash_operate"), delta: yoy("netcash_operate") },
        { label: "现金含量（朴素）", value: ratioText(pick(lastRow, "cash_conversion_naive")),
          foot: "经营现金流 / 净利润" },
        { label: "现金含量（调整后）", value: ratioText(pick(lastRow, "cash_conversion_adjusted")),
          foot: "加回折旧摊销与减值" },
        { label: "扣非归母净利润", value: yi(pick(lastRow, "deduct_parent_net_profit")),
          foot: label(last) + " 累计", neg: (pick(lastRow, "deduct_parent_net_profit") || 0) < 0,
          delta: yoy("deduct_parent_net_profit") }
      ];

      $("metrics").hidden = false;
      $("metrics").innerHTML = cards.map(function (c) {
        var delta = "";
        if (typeof c.delta === "number" && isFinite(c.delta)) {
          var up = c.delta >= 0;
          delta = '<span class="m-delta ' + (up ? "up" : "down") + '">' +
                  (up ? "\u25b2" : "\u25bc") + Math.abs(c.delta).toFixed(1) + "% 同比</span>";
        }
        return '<div class="metric"><div class="m-label">' + esc(c.label) + "</div>" +
               '<div class="m-value' + (c.neg ? " neg" : "") + '">' + esc(c.value) + "</div>" +
               delta +
               '<div class="m-foot">' + esc(c.foot) + "</div>" +
               (c.series ? spark(c.series) : "") + "</div>";
      }).join("");
    }).catch(function () { $("metrics").hidden = true; });
  }

  /* 极简走势图：只表达方向与量级，不做无意义的装饰 */
  function spark(values) {
    var clean = values.filter(function (v) { return v !== null && !isNaN(v); });
    if (clean.length < 2) return "";
    var w = 128, h = 26, pad = 3;
    var max = Math.max.apply(null, clean), min = Math.min.apply(null, clean);
    var span = (max - min) || 1;
    var step = (w - pad * 2) / (values.length - 1);
    var pts = [], dots = [];
    values.forEach(function (v, i) {
      if (v === null || isNaN(v)) return;
      var x = pad + step * i;
      var y = h - pad - ((v - min) / span) * (h - pad * 2);
      pts.push(x.toFixed(1) + "," + y.toFixed(1));
      dots.push({ x: x.toFixed(1), y: y.toFixed(1), neg: v < 0 });
    });
    var svg = '<svg width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + " " + h +
              '" role="img" aria-label="近六期走势">';
    if (min < 0 && max > 0) {
      var zero = (h - pad - ((0 - min) / span) * (h - pad * 2)).toFixed(1);
      svg += '<line x1="0" y1="' + zero + '" x2="' + w + '" y2="' + zero +
             '" stroke="var(--border-strong)" stroke-width="1"/>';
    }
    svg += '<polyline points="' + pts.join(" ") +
           '" fill="none" stroke="var(--accent)" stroke-width="1.6"' +
           ' stroke-linejoin="round" stroke-linecap="round"/>';
    dots.forEach(function (d) {
      svg += '<circle cx="' + d.x + '" cy="' + d.y + '" r="1.9" fill="' +
             (d.neg ? "var(--bad)" : "var(--accent)") + '"/>';
    });
    return svg + "</svg>";
  }

  function loadTable(tablePath) {
    $("table-body").innerHTML = '<div class="empty"><span class="spin"></span> 载入中…</div>';
    fetchTable(tablePath).then(function (j) {
      if (!j.ok || !j.rows || !j.rows.length) {
        $("table-body").innerHTML = '<div class="empty">指标宽表为空。</div>';
        return;
      }
      var head = (j.header || []).map(function (h) {
        return "<th>" + esc(h || "—") + "</th>";
      }).join("");
      var body = j.rows.map(function (row) {
        return "<tr>" + row.map(function (cell, i) {
          if (i < 2) return '<td class="num">' + esc(cell) + "</td>";
          var num = Number(cell);
          var text = (cell === "" || cell === null || isNaN(num))
            ? (cell === "" ? "—" : esc(cell))
            : (Math.abs(num) >= 1000
                ? num.toLocaleString("zh-CN", { maximumFractionDigits: 2 })
                : num.toPrecision(6));
          return '<td class="num">' + text + "</td>";
        }).join("") + "</tr>";
      }).join("");
      $("table-body").innerHTML = '<table class="data"><thead><tr>' + head +
        "</tr></thead><tbody>" + body + "</tbody></table>";
    }).catch(function () {
      $("table-body").innerHTML = '<div class="empty">读取失败。</div>';
    });
  }

  /* ------------------------------------------------------------ 轨迹 */

  var EVENT_LABEL = {
    run_start: "运行开始", run_end: "运行结束", file_access: "文件访问",
    tool_call: "工具调用", compute: "指标计算", finding: "结论",
    rule_error: "规则异常", result: "结果", step: "阶段", target: "目标", skip: "跳过"
  };

  function renderTraces(traces) {
    state.traces = traces;
    var box = $("trace-list");
    if (!traces.length) {
      box.innerHTML = '<div class="empty">尚无运行记录。</div>';
      return;
    }
    box.innerHTML = traces.map(function (t) {
      var on = state.activeTrace === t.run ? " active" : "";
      return '<div class="item' + on + '" data-run="' + esc(t.run) + '">' +
             '<div class="t">' + esc(t.run) + "</div>" +
             '<div class="m">' + fmtTime(t.mtime) + " · " + fmtBytes(t.bytes) + "</div></div>";
    }).join("");
    box.querySelectorAll(".item").forEach(function (el) {
      el.addEventListener("click", function () { openTrace(el.dataset.run); });
    });
    if (!state.activeTrace && traces.length) openTrace(traces[0].run);
  }

  function openTrace(run) {
    state.activeTrace = run;
    document.querySelectorAll("#trace-list .item").forEach(function (n) {
      n.classList.toggle("active", n.dataset.run === run);
    });
    $("trace-title").dataset.state = "ok";
    $("trace-title").innerHTML = '<i class="dot"></i>' + esc(run);

    fetch("/api/trace?run=" + encodeURIComponent(run))
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (!j.ok) throw new Error(j.error || "读取失败");
        state.traceEvents = j.events || [];
        renderTraceFilters();
        renderTraceBody();
      })
      .catch(function (e) {
        $("trace-body").innerHTML = '<div class="empty">读取失败：' + esc(e.message) + "</div>";
      });
  }

  function renderTraceFilters() {
    var counts = {};
    state.traceEvents.forEach(function (e) { counts[e.event] = (counts[e.event] || 0) + 1; });
    var keys = Object.keys(counts).sort(function (a, b) { return counts[b] - counts[a]; });
    var html = ['<button type="button" class="seg' +
                (state.traceFilter === "all" ? " active" : "") +
                '" data-f="all">全部 ' + state.traceEvents.length + "</button>"];
    keys.forEach(function (k) {
      html.push('<button type="button" class="seg' +
                (state.traceFilter === k ? " active" : "") + '" data-f="' + esc(k) + '">' +
                esc(EVENT_LABEL[k] || k) + " " + counts[k] + "</button>");
    });
    var box = $("trace-filters");
    box.innerHTML = html.join("");
    box.querySelectorAll(".seg").forEach(function (b) {
      b.addEventListener("click", function () {
        state.traceFilter = b.dataset.f;
        renderTraceFilters();
        renderTraceBody();
      });
    });
  }

  /* 键值对渲染：结构化事件比一行 JSON 更容易逐项核对 */
  function kvTable(obj, title) {
    if (!obj) return "";
    var keys = Object.keys(obj);
    if (!keys.length) return "";
    return '<div class="ev-kv"><span class="ev-h">' + esc(title) + "</span>" +
      keys.map(function (k) {
        var v = obj[k];
        var text = (v !== null && typeof v === "object") ? oneLine(v, 160) : String(v);
        return '<span class="ev-k">' + esc(k) + '</span><span class="ev-v">' + esc(text) + "</span>";
      }).join("") + "</div>";
  }

  function restOf(ev, skip) {
    var copy = {};
    Object.keys(ev).forEach(function (k) {
      if (skip.indexOf(k) < 0) copy[k] = ev[k];
    });
    return copy;
  }

  var EV_META = ["seq", "ts", "run_id", "event"];

  function traceDetail(ev) {
    if (ev.event === "file_access") {
      return '<span class="ev-tool">' + esc(ev.path) + "</span>" +
             (ev.note ? '<div class="ev-note">' + esc(ev.note) + "</div>" : "");
    }
    if (ev.event === "tool_call") {
      var s = '<span class="ev-tool">' + esc(ev.tool) + "</span>" +
              (ev.ok === false ? ' <span class="tag tag-anomaly">失败</span>' : "");
      s += kvTable(ev.inputs, "入参");
      s += kvTable(ev.outputs, "出参");
      return s;
    }
    if (ev.event === "compute") {
      return '<span class="ev-tool">' + esc(ev.name) + "</span>" +
             (ev.formula ? '<div class="ev-note">' + esc(ev.formula) + "</div>" : "") +
             kvTable(ev.output, "计算输出");
    }
    if (ev.event === "step") {
      return '<span class="ev-tool">' + esc(ev.name || "阶段") + "</span>" +
             (ev.duration_ms ? ' <span class="tag tag-plain">' + ev.duration_ms + " ms</span>" : "") +
             (ev.secucode ? ' <span class="tag tag-plain">' + esc(ev.secucode) + "</span>" : "") +
             (ev.ok === false ? ' <span class="tag tag-anomaly">失败</span>' : "");
    }
    if (ev.event === "finding" || ev.event === "rule_error") {
      var level = ev.level || ev.severity || "";
      var cls = level.indexOf("异常") >= 0 ? "tag-anomaly"
              : (level.indexOf("结构性") >= 0 ? "tag-structural"
              : (level.indexOf("口径") >= 0 ? "tag-caliber" : "tag-plain"));
      return (level ? '<span class="tag ' + cls + '">' + esc(level) + "</span> " : "") +
             '<span class="ev-note">' + esc(ev.title || ev.rule || ev.name || "") + "</span>" +
             kvTable(restOf(ev, EV_META.concat(["level", "severity", "title", "rule", "name"])), "明细");
    }
    return kvTable(restOf(ev, EV_META), "明细");
  }

  function renderTraceBody() {
    var events = state.traceEvents;
    if (state.traceFilter !== "all") {
      events = events.filter(function (e) { return e.event === state.traceFilter; });
    }
    if (!events.length) {
      $("trace-body").innerHTML = '<div class="empty">没有符合筛选条件的事件。</div>';
      return;
    }
    var rows = events.slice(0, 600).map(function (ev) {
      return '<tr><td class="num">' + ev.seq + "</td>" +
             '<td><span class="tag tag-plain">' + esc(EVENT_LABEL[ev.event] || ev.event) +
             "</span></td>" +
             '<td class="wrap">' + traceDetail(ev) + "</td></tr>";
    }).join("");
    $("trace-body").innerHTML =
      '<table class="data"><thead><tr><th>序号</th><th>事件</th><th>明细</th></tr></thead>' +
      "<tbody>" + rows + "</tbody></table>";
  }

  /* ------------------------------------------------------------ 环境 */

  function renderEnv(status) {
    var rows = [
      ["Python", esc(status.python), true],
      ["项目根目录", esc(status.root), true],
      ["推理模型", esc(status.model), false],
      ["接口地址", esc(status.base_url), true],
      ["密钥状态", status.api_key_set ? "已配置（分析由大模型完成归因）" : "未配置（回落离线归因）", false],
      ["智能体步数上限", String(status.max_steps), false],
      ["全市场名录", (status.universe_total || 0).toLocaleString("zh-CN") + " 只证券", false],
      ["已落盘公告", (status.corpus || []).length + " 家公司", false],
      ["全文索引", (status.index ? status.index.chunks : 0) + " 个文本块", false],
      ["报告数量", (status.reports || []).length + " 份", false],
      ["运行记录", (status.traces || []).length + " 次", false]
    ];
    $("env-kv").innerHTML = rows.map(function (r) {
      return "<dt>" + r[0] + "</dt><dd" + (r[2] ? ' class="mono"' : "") + ">" + r[1] + "</dd>";
    }).join("");
  }

  function renderThresholds(thresholds) {
    var keys = Object.keys(thresholds);
    if (!keys.length) {
      $("thresholds").innerHTML = '<div class="empty">无阈值配置。</div>';
      return;
    }
    var rows = keys.map(function (k) {
      return '<tr><td class="mono">' + esc(k) + '</td><td class="num">' +
             esc(String(thresholds[k])) + "</td></tr>";
    }).join("");
    $("thresholds").innerHTML =
      '<table class="data auto"><thead><tr><th>配置项</th><th>取值</th></tr></thead><tbody>' +
      rows + "</tbody></table>";
  }

  /* ------------------------------------------------------------ 推理引擎配置 */

  function setSettingsMsg(text, kind) {
    var el = $("settings-msg");
    el.textContent = text || "";
    el.className = "settings-msg" + (kind ? " " + kind : "");
  }

  function loadSettings() {
    return fetch("/api/settings")
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (!j.ok) throw new Error(j.error || "读取失败");
        var llm = j.llm || {};
        $("set-base-url").value = llm.base_url || "";
        $("set-model").value = llm.model || "";
        $("set-api-key").value = "";
        var st = $("key-state");
        st.textContent = llm.api_key_set
          ? "已保存 " + (llm.api_key_hint || "")
          : "未配置";
        $("set-api-key").placeholder = llm.api_key_set
          ? "留空则保留已保存的密钥"
          : "粘贴你的 API Key";
      })
      .catch(function (e) { setSettingsMsg("读取失败：" + e.message, "bad"); });
  }

  function saveSettings() {
    var body = {
      base_url: $("set-base-url").value.trim(),
      model: $("set-model").value.trim(),
      api_key: $("set-api-key").value.trim()
    };
    if (!body.base_url && !body.model && !body.api_key) {
      setSettingsMsg("没有需要保存的内容", "bad");
      return;
    }
    var btn = $("save-settings");
    btn.disabled = true;
    setSettingsMsg("保存中…");
    fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (r) { return r.json(); })
      .then(function (j) {
        if (!j.ok) throw new Error(j.error || "保存失败");
        setSettingsMsg("已保存到 " + j.path + "，刷新状态即可生效", "ok");
        refreshStatus();
        return loadSettings();
      })
      .catch(function (e) { setSettingsMsg(e.message, "bad"); })
      .then(function () { btn.disabled = false; });
  }

  function initSettings() {
    $("save-settings").addEventListener("click", saveSettings);
    $("reload-settings").addEventListener("click", function () {
      setSettingsMsg("");
      loadSettings();
    });
    $("set-api-key").addEventListener("keydown", function (ev) {
      if (ev.key === "Enter") { ev.preventDefault(); saveSettings(); }
    });
    loadSettings();
  }

  /* ------------------------------------------------------------ 启动 */

  var statusTimer = null;

  function refreshStatus() {
    return fetch("/api/status")
      .then(function (r) { return r.json(); })
      .then(function (j) { if (j.ok) renderStatus(j.status); })
      .catch(function () {});
  }

  function boot() {
    initTheme();
    initTabs();
    initActions();
    initSearch();
    initTargets();
    initRun();
    initReportView();
    initSettings();
    renderTargets();
    refreshStatus();
    statusTimer = setInterval(function () {
      if (!state.running && document.visibilityState === "visible") refreshStatus();
    }, 5000);
    window.addEventListener("beforeunload", function () { clearInterval(statusTimer); });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
