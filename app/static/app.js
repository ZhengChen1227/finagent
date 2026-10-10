/* ==========================================================================
   FinAgent 界面控制器
   --------------------------------------------------------------------------
   这个文件只做四件事：
       1. 把用户拖进来的财报 PDF 送到本机服务端解析；
       2. 把 run.py 子进程的输出与轨迹事件渲染成"运行过程"；
       3. 把产物（报告 / 指标表 / 结构化结论 / 轨迹）读回来展示；
       4. 把用户填写的 API Key 交给本机服务端，用完即弃。

   它刻意不做任何金融计算。界面上出现的每一个数字都来自
   output/ 下的产物文件，或直接把 CSV 原样渲染成表格。
   这样"界面好看"就不会以牺牲可核验性为代价。
   ========================================================================== */

(function () {
  "use strict";

  /* ---------------------------------------------------------------- 基础 */

  var $ = function (id) { return document.getElementById(id); };
  var esc = function (s) { return FinMD.escape(s); };
  var Store = {
    get: function (k, d) {
      try { var v = localStorage.getItem("finagent." + k); return v ? JSON.parse(v) : d; }
      catch (e) { return d; }
    },
    set: function (k, v) {
      try { localStorage.setItem("finagent." + k, JSON.stringify(v)); } catch (e) { }
    },
    del: function (k) { try { localStorage.removeItem("finagent." + k); } catch (e) { } }
  };

  var state = {
    view: "chat",
    status: null,
    pending: [],
    running: false,
    abort: null,
    logLines: 0,
    traceRun: null,
    traceEvents: [],
    traceFilter: "全部",
    reportPath: null,
    reportTable: null,
    reportJson: null,
    runResult: null,
    pendingRun: null,
    pendingNotes: []
  };

  /* 轨迹事件的中文名。轨迹里记的是程序用的键名，
     直接展示给用户看会像在念代码，所以在此统一翻译。 */
  var TRACE_LABEL = {
    run_start: "开始", run_end: "结束", target: "分析对象", question: "用户追问",
    step: "阶段", compute: "指标计算", tool_call: "工具调用",
    file_access: "文件访问", finding: "结论", agent_start: "模型启动",
    agent_end: "模型结束", result: "结果", report: "报告生成",
    rule_error: "规则异常", llm_call: "模型推理", uploads_open: "打开上传目录",
    upload_analyzed: "财报解析", upload_cache_hit: "命中缓存", objective: "任务描述",
    metrics_block: "指标快照", skip: "跳过", corpus_missing: "原文缺失",
    verification_failed: "校验未过"
  };

  /* 内部标识 -> 中文名 */
  var METRIC_LABEL = {
    cash_conversion_naive: "现金含量(朴素)", cash_conversion_adjusted: "现金含量(调整后)",
    gross_margin: "毛利率", net_margin: "净利率", deduct_net_margin: "扣非净利率",
    nonrecurring_ratio: "非经常性损益占比", effective_tax_rate: "实际税率",
    dep_to_revenue: "折旧摊销占收入", free_cash_flow: "自由现金流",
    minority_ratio: "少数股东损益占比", collect_ratio: "收现比"
  };
  var STAGE_LABEL = {
    compute_metrics: "算指标", articulation_check: "勾稽校验",
    anomaly_rules: "跑规则", agent_reasoning: "模型推理"
  };
  var TOOL_LABEL = {
    get_indicator: "取指标", compute_growth: "算同比环比",
    search_disclosure: "检索上传原文", run_anomaly_rules: "异常规则",
    check_articulation: "勾稽校验", list_materials: "列出上传材料",
    list_periods: "列出报告期", compare_subjects: "跨主体对比",
    list_verification: "查看交叉校验", read_page: "读原文页",
    load_skill: "载入技能", list_skills: "列出技能", build_index: "建全文索引",
    compute_metrics: "算指标"
  };

  /* ---------------------------------------------------------------- 小工具 */

  function fmtBytes(n) {
    n = Number(n) || 0;
    if (n < 1024) return n + " B";
    if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
    if (n < 1073741824) return (n / 1048576).toFixed(1) + " MB";
    return (n / 1073741824).toFixed(2) + " GB";
  }

  function fmtTime(ts) {
    var d = new Date((Number(ts) || 0) * 1000);
    if (isNaN(d.getTime())) return "—";
    var p = function (v) { return (v < 10 ? "0" : "") + v; };
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) +
      " " + p(d.getHours()) + ":" + p(d.getMinutes());
  }

  function fmtClock() {
    var d = new Date();
    var p = function (v) { return (v < 10 ? "0" : "") + v; };
    return p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
  }

  function yi(v) {
    if (v === null || v === undefined || isNaN(Number(v))) return "—";
    var n = Number(v) / 1e8;
    return (Math.abs(n) >= 1000 ? n.toFixed(0) : n.toFixed(2)) + " 亿";
  }

  function pctText(v, digits) {
    if (v === null || v === undefined || isNaN(Number(v))) return "—";
    digits = digits === undefined ? 2 : digits;
    var n = Number(v);
    return (n >= 0 ? "+" : "") + n.toFixed(digits) + "%";
  }

  function applyTheme(theme, persist) {
    document.documentElement.setAttribute("data-theme", theme);
    $("theme-label").textContent = theme === "dark" ? "浅色模式" : "深色模式";
    $("btn-theme").querySelector("use").setAttribute("href",
      theme === "dark" ? "#i-sun" : "#i-moon");
    if (persist) Store.set("theme", theme);
  }

  function initTheme() {
    var saved = Store.get("theme", null);
    if (!saved) {
      saved = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
        ? "dark" : "light";
    }
    applyTheme(saved, false);
  }

  function showView(name) {
    state.view = name;
    var views = ["chat", "report", "trace", "env"];
    views.forEach(function (v) {
      $("view-" + v).classList.toggle("active", v === name);
      var nav = $("nav-" + v);
      nav.classList.toggle("active", v === name);
      nav.setAttribute("aria-selected", v === name ? "true" : "false");
    });
    if (name === "env") loadEnv();
    document.getElementById("app").classList.remove("rail-open");
    if (window.innerWidth <= 900) { $("app").classList.remove("rail-open"); }
  }

  /* ---------------------------------------------------------------- 网络 */

  function api(path, options) {
    return fetch(path, options).then(function (r) {
      return r.json().then(function (j) {
        if (!r.ok || j.ok === false) throw new Error(j.error || ("请求失败：" + r.status));
        return j;
      });
    });
  }

  function apiPost(path, body) {
    return api(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    });
  }

  function currentLLM() {
    var k = Store.get("api_key", "");
    return k ? { api_key: k, model: Store.get("model", "deepseek-chat") } : null;
  }

  function hasKey() { return !!currentLLM(); }

  function refreshKeyUI() {
    var ok = hasKey();
    var llm = currentLLM();
    $("key-label").textContent = ok ? "已配置密钥" : "未配置密钥";
    $("key-dot").className = "dot " + (ok ? "on" : "off");
    $("key-chip").textContent = ok ? "更换 API Key" : "填写 API Key";
    $("btn-key-2").classList.toggle("warn", !ok);
    $("model-dot").className = "dot " + (ok ? "" : "off");
  }

  function openKeyModal() {
    var cfg = (state.status && state.status.model) || "deepseek-chat";
    $("in-key").value = Store.get("api_key", "");
    $("in-model").value = Store.get("model", cfg);
    $("in-remember").checked = !!Store.get("api_key", "");
    $("key-err").hidden = true;
    $("key-ok").hidden = true;
    $("modal-key").classList.add("open");
    $("mask").classList.add("on");
    setTimeout(function () { $("in-key").focus(); }, 60);
  }

  function closeKeyModal() {
    $("modal-key").classList.remove("open");
    $("mask").classList.remove("on");
  }

  function readKeyForm() {
    return {
      api_key: $("in-key").value.trim(),
      model: $("in-model").value
    };
  }

  function testKey() {
    var v = readKeyForm();
    if (!v.api_key) { $("key-err").hidden = false; $("key-err").textContent = "请先填写 API Key"; return; }
    $("key-err").hidden = true;
    $("key-ok").hidden = true;
    $("btn-key-test").disabled = true;
    $("btn-key-test").textContent = "测试中…";
    apiPost("/api/key/verify", v).then(function (r) {
      $("key-ok").hidden = false;
      $("key-ok").textContent = "连接正常，可用模型：" + (r.models || []).join("、");
    }).catch(function (e) {
      $("key-err").hidden = false;
      $("key-err").textContent = e.message;
    }).then(function () {
      $("btn-key-test").disabled = false;
      $("btn-key-test").textContent = "测试连接";
    });
  }

  function saveKey() {
    var v = readKeyForm();
    if (!v.api_key) { $("key-err").hidden = false; $("key-err").textContent = "请先填写 API Key"; return; }
    if ($("in-remember").checked) {
      Store.set("api_key", v.api_key);
    } else {
      Store.del("api_key");
    }
    Store.set("model", v.model);
    // 同时写进本机 config.local.yaml，供命令行 run.py 复现同一套配置。
    apiPost("/api/settings", v).then(function () {
      pushLog("推理引擎配置已保存（接口地址固定为 api.deepseek.com）", "sys");
    }).catch(function (e) {
      pushLog("配置写入本机文件失败：" + e.message + "（不影响本次使用）", "err");
    });
    refreshKeyUI();
    closeKeyModal();
  }

  /* ---------------------------------------------------------------- 日志抽屉 */

  function pushLog(text, cls) {
    var box = $("logbox");
    if (!box) return;
    var div = document.createElement("div");
    div.className = "l " + (cls || "sys");
    div.textContent = "[" + fmtClock() + "] " + text;
    box.appendChild(div);
    state.logLines++;
    while (box.childNodes.length > 800) { box.removeChild(box.firstChild); }
    if (box.parentElement.scrollHeight - box.parentElement.scrollTop < 900 ||
        state.logLines % 12 === 0) {
      box.parentElement.scrollTop = box.parentElement.scrollHeight;
    }
  }

  function classifyLog(line) {
    if (!line) return "sys";
    if (/错误|失败|error|Error|Traceback|Exception/.test(line)) return "err";
    if (/\[工具\]|工具调用|tool_call/.test(line)) return "tool";
    if (/结论|命中|异常信号/.test(line)) return "hit";
    return "sys";
  }

  function openDrawer() { $("drawer").classList.add("open"); $("drawer").setAttribute("aria-hidden", "false"); }
  function closeDrawer() { $("drawer").classList.remove("open"); $("drawer").setAttribute("aria-hidden", "true"); }

  /* ---------------------------------------------------------------- 上传 */

  function renderPending() {
    var items = state.pending || [];
    $("pending-count").textContent = items.length + " 份";
    $("count-pending").textContent = items.length;
    var box = $("up-list");
    if (!items.length) {
      box.innerHTML = "";
      box.hidden = true;
    } else {
      box.hidden = false;
      box.innerHTML = items.map(function (it) {
        var cls = it.confidence === "error" ? "bad" : (it.confidence === "warn" ? "warn" : "ok");
        var icon = it.confidence === "ok" ? "#i-check" : "#i-alert";
        var meta = [];
        if (it.code) meta.push(it.code);
        meta.push(it.report_kind_label || "未知报告类型");
        meta.push(it.report_date || "未识别报告期");
        meta.push(it.pages + " 页 · " + fmtBytes(it.bytes));
        meta.push("抽到科目 " + (it.fields_total || 0) + " 项");
        if (it.checks_passed !== null && it.checks_passed !== undefined) {
          meta.push("校验通过 " + it.checks_passed +
            (it.checks_failed ? " / 未过 " + it.checks_failed : ""));
        }
        var issues = (it.issues || []).map(function (s) {
          return '<div class="up-issue">' + esc(s) + "</div>";
        }).join("");
        return '<div class="up-card ' + cls + '">' +
          '<svg width="17" height="17"><use href="' + icon + '"/></svg>' +
          '<div class="up-main">' +
            '<div class="up-name">' + esc(it.filename) + "</div>" +
            '<div class="up-subject">' + esc(it.subject) + "</div>" +
            '<div class="up-meta">' + esc(meta.join(" · ")) + "</div>" +
            issues +
          "</div>" +
          '<button class="icon-btn up-del" type="button" data-file="' +
            esc(it.filename) + '" title="移出本批（不删除本机文件）">' +
            '<svg width="15" height="15"><use href="#i-close"/></svg></button>' +
          "</div>";
      }).join("");
    }
    var notes = (state.pendingNotes || []);
    $("pending-notes").hidden = !notes.length;
    $("pending-notes").innerHTML = notes.map(function (n) { return "· " + esc(n); }).join("<br>");
    refreshRunState();
  }

  function refreshRunState() {
    if (state.running) return;
    var n = (state.pending || []).length;
    var ok = n > 0 && hasKey();
    $("btn-run").disabled = !ok;
    if (state.runResult) {
      // 上次运行的结果文案要留住：否则周期刷新会把它抹掉，
      // 用户就看不到"跑完了没有"这件事。
      $("run-state").textContent = state.runResult.text;
      $("run-state").className = "tag " + state.runResult.cls;
      return;
    }
    $("run-state").textContent = !n ? "等待上传材料"
      : (!hasKey() ? "请先填写 API Key（右上角）" : "就绪，准备分析 " + n + " 份材料");
    $("run-state").className = "tag" + (ok ? " accent" : "");
  }

  function setRunState(text, cls) {
    state.runResult = { text: text, cls: cls || "" };
    $("run-state").textContent = text;
    $("run-state").className = "tag " + (cls || "");
  }

  function uploadFiles(files) {
    var list = Array.prototype.slice.call(files || []).filter(function (f) {
      return /\.pdf$/i.test(f.name);
    });
    if (!list.length) { pushLog("没有可用的 PDF 文件（只接受 .pdf）", "err"); return; }
    $("dropzone").classList.add("busy");
    $("run-state").textContent = "正在解析 " + list.length + " 份文件…";
    var seq = Promise.resolve();
    list.forEach(function (file) {
      seq = seq.then(function () {
        pushLog("上传 " + file.name + "（" + fmtBytes(file.size) + "）", "sys");
        var fd = new FormData();
        fd.append("file", file, file.name);
        return fetch("/api/upload", { method: "POST", body: fd })
          .then(function (r) { return r.json(); })
          .then(function (j) {
            if (!j.ok && j.error) throw new Error(j.error);
            (j.added || []).forEach(function (it) {
              pushLog("解析完成：" + it.filename + " → " + it.subject + " " +
                (it.report_date || "") + "，科目 " + (it.fields_total || 0) + " 项" +
                (it.checks_failed ? "，校验未过 " + it.checks_failed + " 项" : ""),
                it.confidence === "error" ? "err" : "tool");
            });
            (j.rejected || []).forEach(function (it) {
              pushLog("被拒绝：" + it.filename + " —— " + it.error, "err");
            });
            state.pending = j.items || [];
            state.pendingNotes = j.notes || [];
            state.pendingRun = j.run_id || null;
            state.runResult = null;
            renderPending();
          });
      });
    });
    return seq.then(function () {
      $("dropzone").classList.remove("busy");
      renderPending();
    }).catch(function (e) {
      $("dropzone").classList.remove("busy");
      pushLog("上传失败：" + e.message, "err");
      setRunState("上传失败：" + e.message, "");
    });
  }


  function clearPending() {
    if (state.running) { pushLog("正在运行，无法清空本批材料", "err"); return; }
    apiPost("/api/upload/clear", {}).then(function () {
      state.pending = [];
      state.pendingNotes = [];
      state.pendingRun = null;
      renderPending();
      pushLog("已清空当前批次（本机上的 PDF 原件仍保留在 data/uploads 下）", "sys");
    }).catch(function (e) { pushLog("清空失败：" + e.message, "err"); });
  }

  /* ---------------------------------------------------------------- 运行 */

  function startRun() {
    if (state.running) return;
    if (!(state.pending || []).length) { pushLog("请先上传财报 PDF", "err"); return; }
    var llm = currentLLM();
    if (!llm) { openKeyModal(); return; }

    var params = {
      action: "analyze",
      upload_ids: (state.pending || []).map(function (x) { return x.upload_id; }),
      run_id: state.pendingRun || undefined,
      api_key: llm.api_key,
      model: llm.model,
      question: ($("q").value || "").trim()
    };

    state.running = true;
    $("btn-run").disabled = true;
    $("btn-stop").hidden = false;
    $("panel-run").hidden = false;
    $("run-steps").innerHTML = "";
    $("run-kpis").hidden = true;
    $("run-kpis").innerHTML = "";
    setRunState("运行中…", "accent");
    openDrawer();
    pushLog("开始分析：" + params.upload_ids.length + " 份材料，模型 " + params.model, "sys");

    var started = Date.now();
    var timer = setInterval(function () {
      $("run-clock").textContent = ((Date.now() - started) / 1000).toFixed(1) + "s";
    }, 100);

    var controller = ("AbortController" in window) ? new AbortController() : null;
    state.abort = controller;

    fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(params),
      signal: controller ? controller.signal : undefined
    }).then(function (resp) {
      if (!resp.ok) {
        return resp.json().catch(function () { return {}; }).then(function (j) {
          throw new Error(j.error || ("服务返回 " + resp.status));
        });
      }
      return readStream(resp);
    }).catch(function (e) {
      if (e && e.name === "AbortError") {
        pushLog("已请求停止本次运行", "err");
      } else {
        pushLog("运行失败：" + e.message, "err");
      }
    }).then(function () {
      clearInterval(timer);
      state.running = false;
      state.abort = null;
      $("btn-stop").hidden = true;
      refreshStatus().then(function () { refreshRunState(); });
      if (!state.runResult) { refreshRunState(); }
    });
  }

  function readStream(resp) {
    var reader = resp.body.getReader();
    var decoder = new TextDecoder("utf-8");
    var buffer = "";
    function pump() {
      return reader.read().then(function (res) {
        if (res.done) { return; }
        buffer += decoder.decode(res.value, { stream: true });
        var parts = buffer.split("\n\n");
        buffer = parts.pop();
        parts.forEach(function (part) { handleChunk(part); });
        return pump();
      });
    }
    return pump();
  }

  function handleChunk(chunk) {
    var line = chunk.split("\n").filter(function (l) { return l.indexOf("data:") === 0; })[0];
    if (!line) return;
    var payload;
    try { payload = JSON.parse(line.slice(5).trim()); } catch (e) { return; }

    if (payload.kind === "line") {
      if (payload.text) pushLog(payload.text, classifyLog(payload.text));
    } else if (payload.kind === "notice") {
      pushLog(payload.text, "sys");
    } else if (payload.kind === "error") {
      pushLog(payload.message, "err");
    } else if (payload.kind === "trace") {
      var ev = payload.event;
      var d = describeEvent(ev);
      if (d) {
        pushLog("[" + d.kind + "] " + d.text, d.hit ? "hit" : "tool");
        appendStep(d);
      }
    } else if (payload.kind === "start") {
      pushLog("命令行：" + payload.command, "sys");
    } else if (payload.kind === "done") {
      onDone(payload);
    }
  }

  function appendStep(d) {
    var box = $("run-steps");
    var div = document.createElement("div");
    div.className = "ev" + (d.hit ? " hit" : "");
    div.innerHTML = '<span class="ev-kind">' + esc(d.kind) + "</span>" +
      '<span class="ev-text">' + esc(d.text) + "</span>";
    box.appendChild(div);
    box.scrollTop = box.scrollHeight;
  }

  function describeEvent(e) {
    if (!e || !e.event) return null;
    if (e.event === "file_access" && e.mode === "write") {
      return { kind: "写文件", text: e.note || e.path };
    }
    if (e.event === "tool_call") {
      var name = TOOL_LABEL[e.tool] || e.tool;
      var detail = e.inputs ? shortArgs(e.inputs) : "";
      var out = e.outputs ? shortArgs(e.outputs) : "";
      return { kind: "工具调用", text: name + (detail ? "（" + detail + "）" : "") +
        (out ? " → " + out : "") };
    }
    if (e.event === "compute") {
      return { kind: "指标计算", text: (METRIC_LABEL[e.name] || e.name) +
        (e.output && e.output.latest_period ? " → " + e.output.latest_period : "") };
    }
    if (e.event === "step") {
      return { kind: "阶段", text: (STAGE_LABEL[e.name] || e.name) + " 完成" };
    }
    if (e.event === "finding") {
      return { kind: "结论", text: (e.name || "") + (e.detail ? "：" + e.detail : ""), hit: true };
    }
    if (e.event === "verification_failed") {
      return { kind: "校验未过", text: (e.name || "") + "：" + (e.detail || ""), hit: true };
    }
    if (e.event === "upload_analyzed") {
      return { kind: "财报解析", text: (e.file || "") + " → " + (e.subject || "") +
        "（科目 " + Object.keys(e.fields || {}).length + " 张表）" };
    }
    if (e.event === "agent_start") {
      return { kind: "模型启动", text: "模型 " + (e.model || "—") + "，工具 " +
        ((e.tools || []).length) + " 个，技能 " + ((e.skills_matched || []).join("、") || "—") };
    }
    if (e.event === "agent_end") {
      return { kind: "模型结束", text: "终止原因 " + (e.stop_reason || "") +
        "，工具调用 " + (e.tool_calls || 0) + " 次" };
    }
    if (e.event === "result") {
      return { kind: "结果", text: "结论 " + (e.findings || 0) + " 条，报告已生成" };
    }
    if (e.event === "rule_error") {
      return { kind: "规则异常", text: (e.rule || "") + "：" + (e.error || ""), hit: true };
    }
    return null;
  }

  function shortArgs(args) {
    if (!args || typeof args !== "object") return "";
    var parts = [];
    Object.keys(args).slice(0, 4).forEach(function (k) {
      var v = args[k];
      if (v === null || v === undefined || v === "") return;
      if (typeof v === "object") v = Array.isArray(v) ? v.length + " 项" : "…";
      parts.push(k + "=" + String(v).slice(0, 40));
    });
    return parts.join(", ");
  }

  function onDone(payload) {
    var reports = (payload.reports || []).filter(function (r) { return /\.md$/.test(r.name); });
    if (payload.code !== 0) {
      pushLog("运行结束，退出码 " + payload.code + "（见上方错误信息）", "err");
      setRunState("运行失败，请查看日志", "");
      closeDrawer();
      return;
    }
    pushLog("运行完成，用时 " + payload.seconds + " 秒，产出 " + reports.length + " 份报告", "sys");
    setRunState("完成：产出 " + reports.length + " 份报告", "ok");
    // 自动收起日志抽屉：运行结束后用户要看的是报告，
    // 而抽屉是固定定位的浮层，摊开着会挡住报告页的按钮。
    closeDrawer();

    // 运行结束后的结论卡片：把最新一期关键数字直接摆出来
    var md = reports.filter(function (r) { return r.name.indexOf("跨主体") < 0; })[0] ||
      reports[0];
    if (md) {
      api("/api/report?path=" + encodeURIComponent(md.path)).then(function (r) {
        $("run-kpis").hidden = false;
        $("run-kpis").innerHTML = quickKpis(r.markdown);
      }).catch(function () { });
      openReport(md.path);
      showView("report");
    }
  }



  /* ---------------------------------------------------------------- 报告页 */

  function quickKpis(markdown) {
    var picks = [
      ["营业总收入", "revenue"], ["归母净利润", "parent_net_profit"],
      ["扣非归母", "deduct_parent_net_profit"], ["经营现金流", "netcash_operate"]
    ];
    var lines = String(markdown || "").split("\n");
    var header = null;
    for (var i = 0; i < lines.length; i++) {
      if (lines[i].indexOf("| 报告期 |") === 0 && lines[i].indexOf("营业总收入") > 0) {
        header = lines[i].split("|").map(function (s) { return s.trim(); });
        var last = null;
        for (var j = i + 2; j < lines.length && lines[j].indexOf("|") === 0; j++) { last = lines[j]; }
        if (!last) return "";
        var cells = last.split("|").map(function (s) { return s.trim(); });
        var out = picks.map(function (p) {
          var idx = header.indexOf(p[0]);
          if (idx < 0 || !cells[idx]) return "";
          return '<div class="kpi"><div class="k">' + esc(p[0]) + "</div>" +
            '<div class="v">' + esc(cells[idx]) + " 亿</div></div>";
        }).join("");
        return out;
      }
    }
    return "";
  }

  function loadReports() {
    var reports = ((state.status || {}).reports || []);
    $("count-report").textContent = reports.length;
    $("report-count").textContent = reports.length;
    var box = $("report-list");
    if (!reports.length) {
      box.innerHTML = '<div class="empty">还没有报告。到「分析工作台」上传财报并点开始分析。</div>';
      return;
    }
    box.innerHTML = reports.map(function (r) {
      var active = state.reportPath === r.path ? " active" : "";
      return '<div class="list-item' + active + '" data-path="' + esc(r.path) + '">' +
        '<div class="t">' + esc(r.name.replace(/_财务分析报告\.md$/, "")) + "</div>" +
        '<div class="s">' + fmtTime(r.mtime) + " · " + fmtBytes(r.bytes) + "</div></div>";
    }).join("");
  }

  function openReport(path) {
    state.reportPath = path;
    state.reportJson = path.replace(/_财务分析报告\.md$/, "_结论.json");
    loadReports();
    var item = ((state.status || {}).reports || []).filter(function (x) { return x.path === path; })[0];
    $("report-name").textContent = (item ? item.name : path).replace(/_财务分析报告\.md$/, "");
    $("report-table").hidden = true;
    $("report-json").hidden = true;
    api("/api/report?path=" + encodeURIComponent(path)).then(function (r) {
      $("report-body").innerHTML = FinMD.render(r.markdown);
      $("report-kpis").innerHTML = quickKpis(r.markdown);
      $("report-kpis").hidden = !$("report-kpis").innerHTML;
    }).catch(function (e) {
      $("report-body").innerHTML = '<div class="empty">读取失败：' + esc(e.message) + "</div>";
    });
  }

  function openReportTable() {
    var item = ((state.status || {}).reports || []).filter(function (x) {
      return x.path === state.reportPath;
    })[0];
    if (!item || !item.table) { pushLog("这份报告没有对应的指标宽表", "err"); return; }
    var box = $("report-table");
    $("report-json").hidden = true;
    box.hidden = false;
    box.innerHTML = '<div class="empty">读取中…</div>';
    api("/api/table?path=" + encodeURIComponent(item.table)).then(function (r) {
      box.innerHTML = "<table><thead><tr>" +
        r.header.map(function (h) { return "<th>" + esc(h) + "</th>"; }).join("") +
        "</tr></thead><tbody>" +
        r.rows.map(function (row) {
          return "<tr>" + row.map(function (c, i) {
            var cls = "";
            if (i > 0 && /^-/.test(c)) cls = ' class="down"';
            return "<td" + cls + ">" + esc(c) + "</td>";
          }).join("") + "</tr>";
        }).join("") + "</tbody></table>" +
        (r.truncated ? '<div class="note">仅显示前 ' + r.rows.length + " 行，共 " + r.total + " 行</div>" : "");
    }).catch(function (e) {
      box.innerHTML = '<div class="empty">读取失败：' + esc(e.message) + "</div>";
    });
  }

  function openReportJson() {
    var path = state.reportJson ||
      String(state.reportPath || "").replace(/_财务分析报告\.md$/, "_结论.json");
    var box = $("report-json");
    $("report-table").hidden = true;
    box.hidden = false;
    box.innerHTML = '<div class="empty">读取中…</div>';
    api("/api/report?path=" + encodeURIComponent(path)).then(function (r) {
      box.innerHTML = "<pre>" + esc(r.markdown) + "</pre>";
    }).catch(function (e) {
      box.innerHTML = '<div class="empty">读取失败：' + esc(e.message) +
        "（结构化结论与报告同名，扩展名为 _结论.json）</div>";
    });
  }

  function downloadPDF(path) {
    if (!path) { pushLog("请先选择一份报告", "err"); return; }
    var item = ((state.status || {}).reports || []).filter(function (r) { return r.path === path; })[0];
    var name = (item ? item.name : path).replace(/\.md$/, ".pdf");
    pushLog("正在导出 PDF：" + name + "（需要本机安装 Edge/Chrome 用于渲染）", "sys");
    fetch("/api/report.pdf?path=" + encodeURIComponent(path)).then(function (r) {
      if (!r.ok) {
        return r.json().catch(function () { return {}; }).then(function (j) {
          throw new Error(j.error || ("服务返回 " + r.status));
        });
      }
      var ctype = r.headers.get("Content-Type") || "";
      if (ctype.indexOf("text/html") === 0) {
        pushLog("未找到可用的浏览器，已改为返回可打印页面：在浏览器里按 Ctrl+P 另存为 PDF", "err");
      }
      return r.blob().then(function (blob) {
        var url = URL.createObjectURL(blob);
        var a = document.createElement("a");
        a.href = url;
        a.download = name;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        setTimeout(function () { URL.revokeObjectURL(url); }, 8000);
        pushLog("PDF 已开始下载：" + name, "tool");
      });
    }).catch(function (e) { pushLog("导出失败：" + e.message, "err"); });
  }

  /* ---------------------------------------------------------------- 轨迹页 */

  function loadTraces() {
    var traces = ((state.status || {}).traces || []);
    $("count-trace").textContent = traces.length;
    $("trace-count").textContent = traces.length;
    var box = $("trace-list");
    if (!traces.length) {
      box.innerHTML = '<div class="empty">还没有运行轨迹。</div>';
      return;
    }
    box.innerHTML = traces.map(function (t) {
      var active = state.traceRun === t.run ? " active" : "";
      return '<div class="list-item' + active + '" data-run="' + esc(t.run) + '">' +
        '<div class="t">' + esc(t.run) + "</div>" +
        '<div class="s">' + fmtTime(t.mtime) + " · " + fmtBytes(t.bytes) + "</div></div>";
    }).join("");
  }

  function openTrace(run) {
    state.traceRun = run;
    loadTraces();
    $("trace-title").textContent = "事件明细 · " + run;
    api("/api/trace?run=" + encodeURIComponent(run)).then(function (r) {
      state.traceEvents = r.events || [];
      renderTraceFilters();
      renderTraceBody();
    }).catch(function (e) {
      $("trace-body").innerHTML = '<div class="empty">读取失败：' + esc(e.message) + "</div>";
    });
  }

  function renderTraceFilters() {
    var counts = {};
    state.traceEvents.forEach(function (e) {
      counts[e.event || "其他"] = (counts[e.event || "其他"] || 0) + 1;
    });
    var kinds = Object.keys(counts).sort(function (a, b) { return counts[b] - counts[a]; });
    var html = ['<button class="tag accent" data-kind="全部">全部 ' +
      state.traceEvents.length + "</button>"];
    kinds.forEach(function (k) {
      var cls = state.traceFilter === k ? "accent" : "";
      html.push('<button class="tag ' + cls + '" data-kind="' + esc(k) + '">' +
        esc(TRACE_LABEL[k] || k) + " " + counts[k] + "</button>");
    });
    $("trace-filters").innerHTML = html.join("");
  }

  function renderTraceBody() {
    var evs = state.traceEvents.filter(function (e) {
      return state.traceFilter === "全部" || (e.event || "其他") === state.traceFilter;
    });
    if (!evs.length) {
      $("trace-body").innerHTML = '<div class="empty">这一类没有事件。</div>';
      return;
    }
    $("trace-body").innerHTML = '<table><thead><tr><th>#</th><th>时间</th>' +
      "<th>类型</th><th>内容</th></tr></thead><tbody>" +
      evs.map(function (e, i) {
        var d = describeEvent(e) || { kind: "", text: "" };
        var detail = JSON.stringify(e).slice(0, 400);
        if (d.text) { detail = d.text; }
        return "<tr><td>" + (i + 1) + "</td><td>" + esc(String(e.ts || "").slice(11, 19)) +
          "</td><td>" + esc(TRACE_LABEL[e.event] || e.event || "") + "</td><td>" +
          esc(detail) + "</td></tr>";
      }).join("") + "</tbody></table>";
  }


  /* ---------------------------------------------------------------- 环境页 */

  function loadEnv() {
    var st = state.status || {};
    var items = state.pending || [];
    var groups = (st.pending && st.pending.groups) || [];
    var docs = items.length;
    var bytes = items.reduce(function (a, x) { return a + (x.bytes || 0); }, 0);
    var pages = items.reduce(function (a, x) { return a + (x.pages || 0); }, 0);
    var fields = items.reduce(function (a, x) { return a + (x.fields_total || 0); }, 0);
    var failed = items.reduce(function (a, x) { return a + (x.checks_failed || 0); }, 0);

    $("env-stats").innerHTML = [
      ["本批上传材料", docs + " 份", groups.length + " 个主体"],
      ["材料体积", fmtBytes(bytes), pages + " 页正文"],
      ["抽取科目", fields + " 项", "全部来自上传件正文"],
      ["交叉校验未过", failed + " 项", failed ? "已标注为需人工复核" : "全部通过"],
      ["运行期出网", "仅模型调用", "api.deepseek.com"],
      ["服务监听", "127.0.0.1", "只在本机可访问"]
    ].map(function (x) {
      return '<div class="stat"><div class="k">' + esc(x[0]) + "</div>" +
        '<div class="v">' + esc(x[1]) + "</div>" +
        '<div class="s">' + esc(x[2]) + "</div></div>";
    }).join("");

    $("env-run").textContent = (st.pending && st.pending.run_id) || "—";
    var box = $("env-materials");
    if (!items.length) {
      box.innerHTML = '<div class="empty">尚未上传任何材料。</div>';
    } else {
      box.innerHTML = "<table><thead><tr><th>文件</th><th>识别主体</th><th>报告期</th>" +
        "<th>页数</th><th>科目</th><th>校验</th></tr></thead><tbody>" +
        items.map(function (it) {
          return "<tr><td>" + esc(it.filename) + "</td><td>" + esc(it.subject) +
            "</td><td>" + esc((it.report_kind_label || "") + " " + (it.report_date || "")) +
            "</td><td>" + (it.pages || "—") + "</td><td>" + (it.fields_total || 0) +
            "</td><td>" + (it.checks_passed === null || it.checks_passed === undefined
              ? "—" : it.checks_passed + (it.checks_failed ? " / 未过 " + it.checks_failed : "")) +
            "</td></tr>";
        }).join("") + "</tbody></table>";
    }

    var vbox = $("env-verify");
    var suspects = [];
    items.forEach(function (it) {
      (it.suspect || []).forEach(function (s) { suspects.push(it.filename + " · " + s); });
    });
    vbox.innerHTML = suspects.length
      ? "<table><thead><tr><th>文件</th><th>需人工复核的科目</th></tr></thead><tbody>" +
        suspects.map(function (s) {
          var p = s.split(" · ");
          return "<tr><td>" + esc(p[0]) + "</td><td>" + esc(p.slice(1).join(" · ")) + "</td></tr>";
        }).join("") + "</tbody></table>"
      : '<div class="note">本批材料目前没有被降级的科目。</div>';

    var thresholds = st.thresholds || {};
    $("env-thresholds").innerHTML = Object.keys(thresholds).map(function (k) {
      return "<tr><td>" + esc(k) + "</td><td>" + esc(String(thresholds[k])) + "</td></tr>";
    }).join("") ? "<table><tbody>" + Object.keys(thresholds).map(function (k) {
      return "<tr><td>" + esc(k) + "</td><td>" + esc(String(thresholds[k])) + "</td></tr>";
    }).join("") + "</tbody></table>" : '<div class="empty">—</div>';

    $("env-legend").innerHTML = [
      ["异常信号", "anomaly", "数据偏离常态，需要解释。例如减值突增、亏损期仍确认大额所得税。"],
      ["结构性特征", "structural", "看似异常，实为资产结构或行业属性使然。" +
        "例如重资产企业折旧摊销大，经营现金流远高于净利润属于正常现象。"],
      ["口径提示", "caliber", "计算口径本身存在限制，提醒不要误读。" +
        "例如金融业现金流量表结构与工商业不同，现金含量不可直接比较。"]
    ].map(function (x) {
      return '<p><span class="tag ' + x[1] + '">' + x[0] + "</span> " +
        '<span style="font-size:13px;color:var(--text-soft)">' + esc(x[2]) + "</span></p>";
    }).join("");
  }

  /* ---------------------------------------------------------------- 状态刷新 */

  function refreshStatus() {
    return api("/api/status").then(function (r) {
      state.status = r.status || {};
      var st = state.status;
      state.pending = (st.pending && st.pending.items) || [];
      state.pendingNotes = (st.pending && st.pending.notes) || [];
      state.pendingRun = (st.pending && st.pending.run_id) || null;
      $("model-name").textContent = st.model || "deepseek-chat";
      $("model-chip").title = "接口地址：" + (st.base_url || "") + "（固定）";
      renderPending();
      loadReports();
      loadTraces();
      if (state.view === "env") loadEnv();
      refreshKeyUI();
      if (st.busy) { pushLog("服务端报告：已有一次运行正在进行", "sys"); }
    }).catch(function (e) {
      pushLog("读取状态失败：" + e.message, "err");
    });
  }

  /* ---------------------------------------------------------------- 事件绑定 */

  function bind() {
    document.querySelectorAll(".nav-item").forEach(function (b) {
      b.addEventListener("click", function () { showView(b.getAttribute("data-view")); });
    });

    $("btn-menu").addEventListener("click", function () {
      if (window.innerWidth <= 900) { $("app").classList.toggle("rail-open"); }
      else { $("app").classList.toggle("rail-hidden"); }
    });

    $("btn-new").addEventListener("click", function () {
      showView("chat");
      if (!state.running) { $("file-input").click(); }
    });

    $("btn-theme").addEventListener("click", function () {
      var now = document.documentElement.getAttribute("data-theme");
      applyTheme(now === "dark" ? "light" : "dark", true);
    });

    $("fab-log").addEventListener("click", openDrawer);
    $("btn-log-close").addEventListener("click", closeDrawer);
    $("btn-log-clear").addEventListener("click", function () { $("logbox").innerHTML = ""; });

    $("btn-key").addEventListener("click", openKeyModal);
    $("btn-key-2").addEventListener("click", openKeyModal);
    $("btn-key-cancel").addEventListener("click", closeKeyModal);
    $("btn-key-test").addEventListener("click", testKey);
    $("btn-key-save").addEventListener("click", saveKey);
    $("mask").addEventListener("click", closeKeyModal);

    // 上传：点击 + 拖拽
    $("btn-pick").addEventListener("click", function () { $("file-input").click(); });
    $("file-input").addEventListener("change", function (ev) {
      uploadFiles(ev.target.files);
      ev.target.value = "";
    });
    $("dropzone").addEventListener("click", function (ev) {
      if (ev.target.id !== "btn-pick") $("file-input").click();
    });
    ["dragenter", "dragover"].forEach(function (t) {
      $("dropzone").addEventListener(t, function (ev) {
        ev.preventDefault();
        $("dropzone").classList.add("over");
      });
    });
    ["dragleave", "drop"].forEach(function (t) {
      $("dropzone").addEventListener(t, function (ev) {
        ev.preventDefault();
        $("dropzone").classList.remove("over");
      });
    });
    $("dropzone").addEventListener("drop", function (ev) {
      var dt = ev.dataTransfer;
      if (dt && dt.files && dt.files.length) uploadFiles(dt.files);
    });
    window.addEventListener("dragover", function (ev) { ev.preventDefault(); });
    window.addEventListener("drop", function (ev) { ev.preventDefault(); });

    $("btn-clear").addEventListener("click", clearPending);
    $("up-list").addEventListener("click", function (ev) {
      var btn = ev.target.closest(".up-del");
      if (btn) { clearPending(); }
    });

    $("btn-run").addEventListener("click", startRun);
    $("btn-stop").addEventListener("click", function () {
      if (state.abort) state.abort.abort();
    });
    $("q").addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) startRun();
    });

    $("report-list").addEventListener("click", function (ev) {
      var item = ev.target.closest(".list-item");
      if (item) openReport(item.getAttribute("data-path"));
    });
    $("btn-report-pdf").addEventListener("click", function () { downloadPDF(state.reportPath); });
    $("btn-report-table").addEventListener("click", openReportTable);
    $("btn-report-json").addEventListener("click", openReportJson);

    $("trace-list").addEventListener("click", function (ev) {
      var item = ev.target.closest(".list-item");
      if (item) openTrace(item.getAttribute("data-run"));
    });
    $("trace-filters").addEventListener("click", function (ev) {
      var b = ev.target.closest("button");
      if (!b) return;
      state.traceFilter = b.getAttribute("data-kind");
      renderTraceFilters();
      renderTraceBody();
    });

    document.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") { closeDrawer(); closeKeyModal(); }
    });
  }

  /* ---------------------------------------------------------------- 启动 */

  function boot() {
    initTheme();
    bind();
    pushLog("FinAgent 界面已就绪。所有数据在本机处理，运行期不联网取财务数据。", "sys");
    pushLog("唯一出网请求：发往 api.deepseek.com 的模型调用（使用你自己的 Key）", "sys");
    refreshStatus();
    setInterval(function () {
      if (!state.running) refreshStatus();
    }, 30000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();

