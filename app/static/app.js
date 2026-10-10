/* ==========================================================================
   FinAgent 界面控制器
   --------------------------------------------------------------------------
   这个文件只做三件事：
       1. 把用户的一句话解析成一次明确的动作（哪个公司、做什么、附带什么问题）
       2. 把 run.py 的子进程输出流渲染成对话
       3. 把产物（报告 / 指标表 / 轨迹）读回来展示

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
    session: null,
    sessions: [],
    running: false,
    abort: null,
    logLines: 0,
    traceRun: null,
    traceEvents: [],
    traceFilter: "全部",
    reportPath: null,
    reportTable: null
  };

  /* 同一套前端跑两种形态：
       实测模式（本地 app/server.py）：一切数据来自 /api/*，可以发起新分析；
       展示模式（Cloudflare Pages）：一切数据来自 demo/ 下的静态文件，只读。
     两者的数据结构刻意保持一致，因此下面只在"取数"这一层分叉。 */
  var STATIC = document.documentElement.hasAttribute("data-static");
  var DEMO = { manifest: null, traces: {} };

  var TRACE_LABEL = {
    run_start: "开始", run_end: "结束", target: "分析对象", question: "用户追问",
    step: "阶段", compute: "指标计算", tool_call: "工具调用",
    file_access: "文件访问", finding: "结论", agent_start: "模型启动",
    agent_end: "模型结束", result: "结果", report: "报告生成",
    rule_scan: "规则引擎", articulation: "勾稽校验", llm_call: "模型推理",
    corpus_missing: "原文缺失", skip: "跳过", finish: "结束"
  };

  /* 内部标识 -> 中文名。轨迹里记的是程序用的键名，
     直接展示给用户看会像在念代码，所以在此统一翻译。 */
  var METRIC_LABEL = {
    cash_conversion_naive: "现金含量(朴素)", cash_conversion_adjusted: "现金含量(调整后)",
    gross_margin: "毛利率", net_margin: "净利率", deduct_net_margin: "扣非净利率",
    nonrecurring_ratio: "非经常性损益占比", effective_tax_rate: "实际税率",
    dep_to_revenue: "折旧摊销占收入", free_cash_flow: "自由现金流",
    minority_ratio: "少数股东损益占比", collect_ratio: "收现比"
  };
  var STAGE_LABEL = {
    fetch_data: "取数", compute_metrics: "算指标", articulation_check: "勾稽校验",
    anomaly_rules: "跑规则", agent_reasoning: "模型推理", attribution: "归因",
    load_universe: "加载名录", fetch_corpus: "抓公告"
  };
  var TOOL_LABEL = {
    normalize: "规整报表", get_indicator: "取指标", search_disclosure: "检索公告",
    anomaly_rules: "异常规则", run_anomaly_rules: "异常规则",
    articulation_check: "勾稽校验", check_articulation: "勾稽校验",
    list_corpus: "列出原文", list_periods: "列出报告期", compare_companies: "跨公司对比",
    compute_growth: "算同比环比", read_page: "读原文页", load_skill: "载入技能",
    list_skills: "列出技能", build_index: "建全文索引", resolve_profile: "取主体画像",
    load_universe: "加载名录", fetch_corpus: "抓取公告", resolve_report_family: "识别报表族",
    resolve_secucode: "解析代码", llm_attribution: "模型归因", http_get: "取数请求"
  };

  /* 过程块只展示"对理解这件事有意义"的事件。
     记账类事件（run_start / run_end / agent_start / step 之外的内部步骤）
     留在轨迹页里逐条可查，但不该挤占对话的注意力。 */
  var PROC_HIDE = { run_start: 1, run_end: 1, agent_start: 1, report: 1 };

  /* ---------------------------------------------------------------- 工具函数 */

  function fmtBytes(n) {
    n = Number(n) || 0;
    if (n < 1024) return n + " B";
    if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
    if (n < 1073741824) return (n / 1048576).toFixed(1) + " MB";
    return (n / 1073741824).toFixed(2) + " GB";
  }

  function fmtTime(ts) {
    var d = new Date((Number(ts) || 0) * 1000);
    var p = function (v) { return (v < 10 ? "0" : "") + v; };
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) +
      " " + p(d.getHours()) + ":" + p(d.getMinutes());
  }

  function fmtClock() {
    var d = new Date();
    var p = function (v) { return (v < 10 ? "0" : "") + v; };
    return p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
  }

  /* 报告里的金额单位已经是亿元，这里只做格式化。
     再除一次 1e8 会把 594.10 亿显示成 0.00 亿。 */
  function fmtYi(v) {
    var n = Number(v);
    if (!isFinite(n)) return "—";
    // 银行等巨额用万亿更好读，否则 13690 亿这种写法要数零。
    if (Math.abs(n) >= 10000) return (n / 10000).toFixed(2) + " 万亿";
    var s = Math.abs(n) >= 1000 ? n.toFixed(0)
          : (Math.abs(n) >= 100 ? n.toFixed(1) : n.toFixed(2));
    return s + " 亿";
  }

  /* 百分比小于 1 时多留一位小数，否则 +0.04% 会被四舍五入成 +0.0%，
     看上去像没变化，但方向箭头又往上，自相矛盾。 */
  function pctDigits(v) {
    return Math.abs(Number(v)) < 1 ? 2 : 1;
  }

  function pct(v, digits) {
    var n = Number(v);
    if (!isFinite(n)) return "—";
    var d = digits === undefined ? pctDigits(n) : digits;
    return (n >= 0 ? "+" : "") + n.toFixed(d) + "%";
  }

  /* 百分点变动不能带 % 号：“-22.10 个百分点”不是“-22.10%”。 */
  function signed(v, digits) {
    var n = Number(v);
    if (!isFinite(n)) return "—";
    return (n >= 0 ? "+" : "") + n.toFixed(digits === undefined ? 2 : digits);
  }

  function num(text) {
    if (text === null || text === undefined) return NaN;
    var t = String(text).replace(/[,%+]/g, "").trim();
    if (!t || t === "—" || t === "-") return NaN;
    return Number(t);
  }

  /* 把 "2026Q2" 映射到 "2025Q2"，用于计算同比 */
  function lastYear(period) {
    var m = String(period || "").match(/^(\d{4})(Q\d)$/i);
    if (!m) return null;
    return (Number(m[1]) - 1) + m[2].toUpperCase();
  }

  /* ---------------------------------------------------------------- 主题 */

  function applyTheme(theme, persist) {
    document.documentElement.setAttribute("data-theme", theme);
    var label = $("theme-label");
    var icon = $("btn-theme").querySelector("use");
    if (label) label.textContent = theme === "dark" ? "浅色模式" : "深色模式";
    if (icon) icon.setAttribute("href", theme === "dark" ? "#i-sun" : "#i-moon");
    if (persist) Store.set("theme", theme);
  }

  function initTheme() {
    var forced = (location.search.match(/[?&]theme=(dark|light)/) || [])[1];
    applyTheme(forced || Store.get("theme", "light"), false);
  }

  /* ---------------------------------------------------------------- 视图切换 */

  function showView(name) {
    state.view = name;
    var views = document.querySelectorAll(".view");
    for (var i = 0; i < views.length; i++) views[i].classList.remove("active");
    var target = $("view-" + name);
    if (target) target.classList.add("active");

    var navs = document.querySelectorAll(".nav-item");
    for (var j = 0; j < navs.length; j++) {
      var on = navs[j].getAttribute("data-view") === name;
      navs[j].classList.toggle("active", on);
      navs[j].setAttribute("aria-selected", on ? "true" : "false");
    }
    closeRailOnMobile();
    if (name === "report") loadReports();
    if (name === "trace") loadTraces();
    if (name === "env") loadEnv();
  }

  function closeRailOnMobile() { $("app").classList.remove("rail-open"); }

  /* ---------------------------------------------------------------- 后端调用 */

  function api(path, options) {
    return fetch(path, options).then(function (r) {
      return r.json().then(function (j) {
        if (!r.ok || j.ok === false) throw new Error(j.error || ("请求失败 " + r.status));
        return j;
      });
    });
  }

  /* 展示模式下读取本地静态文件，实测模式下走接口。返回结构一致。 */
  function fetchReport(ref) {
    if (STATIC) {
      return fetch(ref).then(function (r) {
        if (!r.ok) throw new Error("报告不存在");
        return r.text();
      }).then(function (t) { return { ok: true, path: ref, markdown: t }; });
    }
    return api("/api/report?path=" + encodeURIComponent(ref));
  }

  function fetchTable(ref) {
    if (STATIC) {
      return fetch(ref).then(function (r) {
        if (!r.ok) throw new Error("指标表不存在");
        return r.json();
      });
    }
    return api("/api/table?path=" + encodeURIComponent(ref));
  }

  function fetchTrace(run, ref) {
    if (STATIC) {
      if (DEMO.traces[ref]) return Promise.resolve(DEMO.traces[ref]);
      return fetch(ref).then(function (r) { return r.json(); });
    }
    return api("/api/trace?run=" + encodeURIComponent(run));
  }

  function loadManifest() {
    if (DEMO.manifest) return Promise.resolve(DEMO.manifest);
    return fetch("demo/manifest.json").then(function (r) { return r.json(); })
      .then(function (j) { DEMO.manifest = j; return j; });
  }

  function apiPost(path, body) {
    return api(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    });
  }

  /* ---------------------------------------------------------------- API Key */

  function currentLLM() {
    // 优先级：本次会话在界面上填写的 -> 服务端已配置的
    var mine = Store.get("llm", null) || state.mine || null;
    if (mine && mine.api_key) return mine;
    var st = state.status || {};
    if (st.api_key_set) return { api_key: "__server__", base_url: st.base_url, model: st.model };
    return null;
  }

  function hasKey() { return !!currentLLM(); }

  function refreshKeyUI() {
    var mine = Store.get("llm", null) || state.mine || {};
    var st = state.status || {};
    var ok = hasKey();
    var text;
    if (mine.api_key) text = "已填写 " + (mine.api_key.slice(0, 6)) + "…";
    else if (st.api_key_set) text = "服务端已配置";
    else text = "未配置密钥";

    $("key-label").textContent = text;
    $("key-dot").className = "dot " + (ok ? "on" : "off");
    $("key-chip").textContent = ok ? "密钥已就绪" : "填写 API Key";
    $("btn-key-2").classList.toggle("warn", !ok);
    $("model-dot").className = "dot " + (ok ? "" : "off");
    $("model-name").textContent = mine.model || st.model || "deepseek-chat";

    $("composer-hint").textContent = ok
      ? "数据来自巨潮资讯网公告原文与东方财富公开接口，全部计算可回溯"
      : "请先填写你自己的大模型 API Key，然后就可以开始分析任意上市公司";
  }

  function openKeyModal() {
    var mine = Store.get("llm", null) || state.mine || {};
    var st = state.status || {};
    $("in-key").value = mine.api_key || "";
    $("in-base").value = mine.base_url || st.base_url || "https://api.deepseek.com/v1";
    $("in-model").value = mine.model || st.model || "deepseek-chat";
    $("in-remember").checked = !!mine.api_key;
    $("key-err").hidden = true;
    $("key-ok").hidden = true;
    $("modal-key").classList.add("open");
    setTimeout(function () { $("in-key").focus(); }, 60);
  }

  function closeKeyModal() { $("modal-key").classList.remove("open"); }

  function readKeyForm() {
    return {
      api_key: $("in-key").value.trim(),
      base_url: $("in-base").value.trim() || "https://api.deepseek.com/v1",
      model: $("in-model").value.trim() || "deepseek-chat"
    };
  }

  function testKey() {
    var v = readKeyForm();
    var err = $("key-err"), ok = $("key-ok");
    err.hidden = true; ok.hidden = true;
    if (!v.api_key) { err.textContent = "请先填写 API Key"; err.hidden = false; return; }
    $("btn-key-test").disabled = true;
    $("btn-key-test").textContent = "测试中…";
    apiPost("/api/key/verify", v).then(function (r) {
      ok.textContent = "连接成功" + (r.models && r.models.length
        ? "，可用模型 " + r.models.length + " 个（含 " + r.models.slice(0, 3).join("、") + "）" : "");
      ok.hidden = false;
    }).catch(function (e) {
      err.textContent = e.message;
      err.hidden = false;
    }).then(function () {
      $("btn-key-test").disabled = false;
      $("btn-key-test").textContent = "测试连接";
    });
  }

  function saveKey() {
    var v = readKeyForm();
    if (!v.api_key) { testKey(); return; }
    if ($("in-remember").checked) Store.set("llm", v);
    else Store.del("llm");
    state.mine = v;
    refreshKeyUI();
    closeKeyModal();
    pushLog("已填写大模型密钥（" + v.model + " @ " + v.base_url + "），只在本次运行中使用", "sys");
  }

  /* ---------------------------------------------------------------- 日志抽屉 */

  function pushLog(text, cls) {
    var box = $("logbox");
    if (!box) return;
    var div = document.createElement("div");
    div.className = "l " + (cls || "");
    div.textContent = "[" + fmtClock() + "] " + text;
    box.appendChild(div);
    state.logLines++;
    if (state.logLines > 3000) { box.removeChild(box.firstChild); state.logLines--; }
    var parent = box.parentElement;
    if (parent.scrollHeight - parent.scrollTop < parent.clientHeight + 300) {
      parent.scrollTop = parent.scrollHeight;
    }
  }

  function classifyLog(line) {
    if (/错误|失败|异常|Traceback|Error/.test(line)) return "err";
    if (/命中|异常信号|规则/.test(line)) return "hit";
    if (/工具|调用|tool/.test(line)) return "tool";
    return "";
  }

  function openDrawer() {
    $("drawer").classList.add("open");
    $("mask").classList.add("on");
    $("drawer").setAttribute("aria-hidden", "false");
    var body = $("logbox").parentElement;
    body.scrollTop = body.scrollHeight;
  }
  function closeDrawer() {
    $("drawer").classList.remove("open");
    $("mask").classList.remove("on");
    $("drawer").setAttribute("aria-hidden", "true");
  }

  /* ---------------------------------------------------------------- 会话模型 */

  function newSession(keepMessages) {
    var s = {
      id: "s" + Date.now().toString(36),
      title: "新的分析",
      ts: Date.now() / 1000,
      messages: keepMessages ? state.session.messages.slice() : []
    };
    state.session = s;
    if (!keepMessages) {
      state.sessions.unshift(s);
      state.sessions = state.sessions.slice(0, 30);
    }
    persistSessions();
    return s;
  }

  function persistSessions() {
    // 只保存渲染所需的数据，绝不保存任何密钥
    var clean = state.sessions.slice(0, 30).map(function (s) {
      return {
        id: s.id, title: s.title, ts: s.ts,
        messages: s.messages.map(function (m) {
          var c = JSON.parse(JSON.stringify(m));
          if (c.events) c.events = c.events.slice(0, 260);
          if (c.resultCard && c.resultCard.reports) {
            c.resultCard.reports = c.resultCard.reports.slice(0, 8);
          }
          delete c.streaming;
          return c;
        })
      };
    });
    Store.set("sessions", clean);
  }

  function loadSessions() {
    state.sessions = Store.get("sessions", []) || [];
    state.session = state.sessions.length ? state.sessions[0] : newSession(false);
  }

  /* ---------------------------------------------------------------- 意图解析 */

  /* 把用户的一句话翻译成一次明确的动作。
     规则表从上到下匹配，第一条命中即生效——顺序本身就是优先级。
     写死在这里是为了让"系统当时是怎么理解我的"完全可解释、可复核。 */
  var ANALYZE_HINT = /分析|看看|看一下|看下|查一下|查查|研究|诊断|体检|为什么|为何|怎么|如何|怎么样|是否|有没有|对比|比较|估值|风险|质量/;
  var FETCH_HINT = /抓取|抓下|下载|获取|拉取|更新/;
  var INDEX_HINT = /索引|检索库/;

  function parseAction(text) {
    if (ANALYZE_HINT.test(text)) return "analyze";
    if (INDEX_HINT.test(text) && /建|重建|更新|做|生成/.test(text)) return "index";
    if (FETCH_HINT.test(text) && /公告|原文|财报|年报|季报|报告|数据/.test(text)) return "fetch";
    if (INDEX_HINT.test(text)) return "index";
    if (FETCH_HINT.test(text)) return "fetch";
    return "analyze";
  }

  function stripActionWords(text) {
    // 追问内容里去掉纯操作动词，让交给模型的问题更聚焦
    return text.replace(/^\s*(请|帮我|帮忙|麻烦|我想|我要|现在)?\s*(分析一下|分析|看看|看一下|查一下|研究一下|诊断一下)\s*/, "").trim() || text.trim();
  }

  /* ---------------------------------------------------------------- 对话渲染 */

  var SUGGESTS = [
    { t: "牧原股份 2026 中报为什么亏损", d: "亏损期确认所得税、减值激增等信号归因", q: "分析牧原股份 2026 年中报为什么亏损" },
    { t: "京东方利润和现金流差这么多", d: "账面利润与经营现金流背离的成因", q: "京东方账面利润和经营现金流差这么多是什么原因" },
    { t: "工商银行这个半年报怎么样", d: "金融业报表口径下的异常与口径提示区分", q: "分析工商银行 2026 年半年报的业绩表现" },
    { t: "贵州茅台的关键指标", d: "稳健样本，用来对照亏损与背离", q: "分析贵州茅台最近几期的关键财务指标" }
  ];

  function renderSuggests() {
    var box = $("suggest-grid");
    if (STATIC && !DEMO.manifest) {
      box.innerHTML = '<div class="empty" style="text-align:center">正在读取实录数据…</div>';
      return;
    }
    var cards = SUGGESTS;
    if (STATIC && DEMO.manifest && DEMO.manifest.demos.length) {
      cards = DEMO.manifest.demos.map(function (d) {
        return {
          t: d.name + "（" + d.secucode + "）",
          d: "实录回放 · " + d.findings + " 条结论，其中高优先级 " + d.high + " 条",
          q: d.question, demo: d
        };
      });
    }
    box.innerHTML = cards.map(function (s) {
      return '<button class="suggest-card" type="button" data-q="' + esc(s.q) + '">' +
        "<b>" + esc(s.t) + "</b><span>" + esc(s.d) + "</span></button>";
    }).join("");
    box.onclick = function (ev) {
      var btn = ev.target.closest(".suggest-card");
      if (!btn) return;
      var q = btn.getAttribute("data-q");
      if (STATIC) {
        var hit = (DEMO.manifest.demos || []).filter(function (d) { return d.question === q; })[0];
        if (hit) { replayDemo(hit); return; }
      }
      $("q").value = q;
      autoGrow();
      send();
    };
  }

  /* 展示站的对话不是实时计算，而是把一次真实运行的过程与产物放一遍。
     这一点在界面上必须说清楚——把回放包装成"正在分析"是不诚实的。 */
  function replayDemo(demo, askedText) {
    if (state.running) return;
    // 找这次运行产出的报告：优先用清单里的 slug 定位，回退到公司名匹配
    var report = ((state.status || {}).reports || []).filter(function (r) {
      if (demo.reportSlug) return r.path.indexOf("/" + demo.reportSlug + ".md") >= 0;
      return r.name.indexOf(demo.name) === 0;
    })[0];

    state.session.messages.push({
      role: "user", text: askedText || demo.question,
      actionLabel: "动作：财务分析（实录回放）",
      targets: [{ code: demo.secucode, name: demo.name }]
    });
    if (state.session.messages.length === 1) {
      state.session.title = "回放 · " + demo.name;
      state.session.ts = Date.now() / 1000;
      renderThreads();
    }

    var a = {
      role: "assistant", events: [], text: "", streaming: true,
      startedAt: Date.now(), resultCard: null, error: null
    };
    state.session.messages.push(a);
    setRunning(true);
    renderChat();

    var entry = ((state.status || {}).traces || []).filter(function (t) {
      return t.run === demo.run;
    })[0];

    fetchTrace(demo.run, entry && entry.file).then(function (events) {
      var list = Array.isArray(events) ? events : (events.events || []);
      var i = 0;
      var step = Math.max(1, Math.ceil(list.length / 60));
      var timer = setInterval(function () {
        for (var n = 0; n < step && i < list.length; n++, i++) {
          a.events.push(list[i]);
        }
        a.elapsed = ((Date.now() - a.startedAt) / 1000).toFixed(0) + "s";
        if (i >= list.length) {
          clearInterval(timer);
          a.streaming = false;
          a.text = "以上是一次真实运行的完整过程记录。下面是这次运行产出的结论概要，" +
            "完整报告可点开查看，也可以下载 PDF。";
          a.resultCard = {
            ok: true, seconds: (a.elapsed || "").replace("s", ""), run_id: demo.run,
            reports: report ? [{ name: report.name, path: report.path }] : [],
            kpis: []
          };
          setRunning(false);
          persistSessions();
          renderChat();
          if (report) {
            fetchReport(report.path).then(function (r) {
              var kpis = quickKpis(r.markdown);
              if (kpis.length) { a.resultCard.kpis = kpis; renderChat(); persistSessions(); }
            }).catch(function () { });
          }
          return;
        }
        var proc = document.querySelector(".proc[data-live]");
        if (proc) {
          var holder = document.createElement("div");
          holder.innerHTML = renderProcess(a, state.session.messages.indexOf(a));
          var fresh = holder.firstChild;
          fresh.classList.add("open");
          proc.parentNode.replaceChild(fresh, proc);
        }
      }, 90);
    }).catch(function (e) {
      setRunning(false);
      finishAssistant(a, { error: "回放数据读取失败：" + e.message });
    });
  }

  function renderChat() {
    var wrap = $("messages");
    var msgs = state.session ? state.session.messages : [];
    $("welcome").hidden = msgs.length > 0;
    wrap.innerHTML = msgs.map(renderMessage).join("");
    var last = wrap.querySelector(".proc[data-live='1']");
    if (last) last.classList.add("open");
    scrollToEnd();
  }

  function renderMessage(m, index) {
    return m.role === "user" ? renderUser(m, index) : renderAssistant(m, index);
  }

  function renderUser(m, index) {
    var targets = "";
    if (m.targets && m.targets.length) {
      targets = '<div class="targets">' + m.targets.map(function (t, i) {
        return '<span class="target">' + esc(t.name) + " <small>" + esc(t.code) + "</small>" +
          '<button type="button" data-drop="' + index + ":" + i + '" title="移除该对象">×</button></span>';
      }).join("") + "</div>";
    }
    return '<div class="msg user"><div style="min-width:0;max-width:82%">' +
      '<div class="bubble">' + esc(m.text) + "</div>" + targets +
      (m.actionLabel ? '<div class="msg-note" style="text-align:right">' + esc(m.actionLabel) + "</div>" : "") +
      "</div></div>";
  }

  function renderAssistant(m, index) {
    var body = "";
    if (m.events && m.events.length) body += renderProcess(m, index);

    if (m.text) {
      body += '<div class="answer">' + FinMD.render(m.text) +
        (m.streaming ? '<span class="caret-blink"></span>' : "") + "</div>";
    } else if (m.streaming && (!m.events || !m.events.length)) {
      body += '<div class="answer"><span style="color:var(--muted)">正在准备…</span>' +
        '<span class="caret-blink"></span></div>';
    }

    if (m.error) body += '<div class="msg-error">' + esc(m.error) + "</div>";
    if (m.resultCard) body += renderResultCard(m);

    return '<div class="msg assistant"><div class="avatar">智</div><div class="body">' +
      body + "</div></div>";
  }

  function renderProcess(m, index) {
    var evs = m.events;
    var tools = 0, files = 0, findings = 0;
    evs.forEach(function (e) {
      if (e.event === "tool_call") tools++;
      else if (e.event === "file_access") files++;
      else if (e.event === "finding") findings++;
    });
    var bits = [];
    if (tools) bits.push(tools + " 条执行事件");
    if (files) bits.push(files + " 次文件访问");
    if (findings) bits.push(findings + " 条结论");
    var summary = bits.length ? bits.join(" · ") : (m.streaming ? "正在执行…" : "执行完成");

    var rows = summariseEvents(evs).map(function (r) {
      return '<div class="ev' + (r.hit ? " hit" : "") + '">' +
        '<span class="ev-icon">' + (r.hit ? "●" : "·") + "</span>" +
        '<span class="ev-kind">' + esc(r.kind) + "</span>" +
        '<span class="ev-text">' + esc(r.text) + "</span></div>";
    }).join("") +
      '<div class="ev"><span class="ev-icon">→</span><span class="ev-kind">完整记录</span>' +
      '<span class="ev-text">上面是执行摘要；逐条的文件访问、工具出入参与计算明细，' +
      '在左侧「执行轨迹」里可以按类型筛选核对。</span></div>';

    return '<div class="proc"' + (m.streaming ? ' data-live="1"' : "") + ">" +
      '<button class="proc-head" type="button" data-proc="' + index + '">' +
      '<span class="caret"><svg width="13" height="13"><use href="#i-caret"/></svg></span>' +
      (m.streaming ? '<span class="spinner"></span>' : "") +
      '<span class="summary">' + esc(summary) + "</span>" +
      '<span class="tick">' + esc(m.elapsed || "") + "</span>" +
      "</button>" +
      '<div class="proc-body">' + rows + "</div></div>";
  }

  /* 把一长串事件压成几行可读的摘要。
     依据是：用户想知道"它做了什么、依据是什么"，
     而不是"它调了多少次 API"。 */
  function summariseEvents(evs) {
    var out = [];
    var byTool = {}, computes = [], reads = 0, writes = 0;
    var steps = [], findings = [], llm = null, result = null, question = null;

    evs.forEach(function (e) {
      var k = e.event;
      if (PROC_HIDE[k]) return;
      if (k === "target") {
        out.push({ kind: "分析对象", text: (e.name || "") + "（" + (e.secucode || "") + "）" });
      } else if (k === "question") {
        question = e.text || "";
      } else if (k === "step") {
        var ms = Math.round(e.duration_ms || 0);
        steps.push((e.name || "") + (ms > 0 ? " " + ms + "ms" : ""));
      } else if (k === "compute") {
        computes.push(e.name || "");
      } else if (k === "tool_call") {
        byTool[e.tool || "?"] = (byTool[e.tool || "?"] || 0) + 1;
      } else if (k === "file_access") {
        if (e.mode === "write") writes++; else reads++;
      } else if (k === "finding") {
        findings.push("[" + (e.level || "") + "] " + (e.title || ""));
      } else if (k === "agent_end") {
        llm = e;
      } else if (k === "result") {
        result = e;
      }
    });

    if (question) out.push({ kind: "追问", text: question });

    if (steps.length) {
      out.push({ kind: "阶段", text: steps.map(function (x) {
        var parts = x.split(" ");
        return (STAGE_LABEL[parts[0]] || parts[0]) + (parts[1] ? " " + parts[1] : "");
      }).join(" → ") });
    }

    var tools = Object.keys(byTool);
    if (tools.length) {
      out.push({ kind: "工具调用", text: tools.map(function (t) {
        return (TOOL_LABEL[t] || t) + " ×" + byTool[t];
      }).join("、") });
    }

    if (computes.length) {
      var uniq = [];
      computes.forEach(function (n) { if (uniq.indexOf(n) < 0) uniq.push(n); });
      out.push({ kind: "指标计算", text: "共 " + computes.length + " 项：" +
        uniq.slice(0, 11).map(function (n) { return METRIC_LABEL[n] || n; }).join("、") +
        (uniq.length > 11 ? " 等" : "") });
    }

    if (reads || writes) {
      var bits = [];
      if (reads) bits.push("读取 " + reads + " 次");
      if (writes) bits.push("写出 " + writes + " 次");
      out.push({ kind: "文件访问", text: bits.join("，") + "（本地缓存与原文，逐条见轨迹）" });
    }

    if (llm) {
      var usage = llm.llm_usage || {};
      out.push({ kind: "模型推理", text: (llm.mode || "llm") + " 模式 · 推理 " + (llm.steps || 0) +
        " 步 · 模型发起工具调用 " + (llm.tool_calls || 0) + " 次" +
        (usage.prompt_tokens ? " · 输入 " + usage.prompt_tokens.toLocaleString() +
          " / 输出 " + (usage.completion_tokens || 0).toLocaleString() + " tokens" : "") });
    }

    findings.forEach(function (f) { out.push({ kind: "结论", text: f, hit: true }); });

    if (result && result.findings !== undefined && !findings.length) {
      out.push({ kind: "结论", text: "共 " + result.findings + " 条（高优先级 " +
        (result.high || 0) + " 条），明细见报告正文" });
    }
    return out;
  }

  function describeEvent(e) {
    var kind = e.event || "";
    if (kind === "compute") {
      var val = e.output && e.output.latest_value;
      var per = e.output && e.output.latest_period;
      return (e.name || "指标") + "　" + (e.formula || "") +
        (val === undefined ? "" : "　→ " + (Number(val).toFixed(4)) + (per ? "（" + per + "）" : ""));
    }
    if (kind === "step") return (e.name || "") +
      (e.duration_ms ? "　" + Math.round(e.duration_ms) + " ms" : "") +
      (e.ok === false ? "　未通过" : "");
    if (kind === "agent_start") return "目标：" + (e.objective || "").slice(0, 90) +
      "　模型 " + (e.model || "");
    if (kind === "agent_end") return "模式 " + (e.mode || "") + " · 步数 " +
      (e.steps || 0) + " · 工具 " + (e.tool_calls || 0) + " 次 · 停止原因 " +
      (e.stop_reason || "");
    if (kind === "result") return "结论 " + (e.findings || 0) + " 条（高 " + (e.high || 0) +
      "）· " + (e.report || "");
    if (kind === "run_end") return "共 " + (e.total_events || 0) + " 个事件";
    if (kind === "target") return "锁定对象 " + (e.name || "") + "（" + (e.secucode || "") + "）";
    if (kind === "question") return "用户追问：" + (e.text || "");
    if (kind === "fetch_data") return "取数完成 " + (e.secucode || "");
    if (kind === "rule_scan") {
      var bits = [];
      if (e.findings !== undefined) bits.push("结论 " + e.findings + " 条");
      if (e.anomaly !== undefined) bits.push("异常 " + e.anomaly);
      if (e.structural !== undefined) bits.push("结构 " + e.structural);
      if (e.caliber !== undefined) bits.push("口径 " + e.caliber);
      return bits.join(" · ");
    }
    if (kind === "articulation") return "勾稽校验 " + (e.ok === false ? "未通过" : "通过") +
      (e.checked !== undefined ? "（核对 " + e.checked + " 项）" : "");
    if (kind === "tool_call") return (e.tool || "工具") + " " + shortArgs(e.inputs) +
      (e.duration_ms ? "　" + Math.round(e.duration_ms) + " ms" : "") +
      (e.ok === false ? "　失败" : "");
    if (kind === "file_access") {
      var op = { read: "读取", write: "写出", search: "检索" }[e.mode] || (e.mode || "访问");
      return op + " " + (e.path || "") + (e.note ? "（" + e.note + "）" : "");
    }
    if (kind === "finding") return "[" + (e.level || "") + "] " + (e.title || "") +
      (e.rule ? "  规则 " + e.rule : "");
    if (kind === "llm_call") return "调用 " + (e.model || "模型") + "（第 " + (e.step || 1) + " 步）";
    if (kind === "corpus_missing") return "本地尚无该公司公告原文，归因暂无法回溯";
    if (kind === "skip") return "跳过：" + (e.reason || "");
    if (kind === "report") return "已写出报告";
    if (kind === "finish") return "结束（" + (e.stop_reason || "") + "）";
    try { return JSON.stringify(e).slice(0, 180); } catch (x) { return kind; }
  }

  function shortArgs(args) {
    if (!args) return "";
    try {
      var s = typeof args === "string" ? args : JSON.stringify(args);
      return s.length > 90 ? s.slice(0, 90) + "…" : s;
    } catch (e) { return ""; }
  }

  function renderResultCard(m) {
    var r = m.resultCard;
    var kpis = (r.kpis || []).map(function (k) {
      return '<div class="kpi"><div class="k">' + esc(k.k) + "</div>" +
        '<div class="v">' + esc(k.v) + "</div>" +
        '<div class="d ' + esc(k.dir || "flat") + '">' + esc(k.d || "") + "</div></div>";
    }).join("");

    var links = (r.reports || []).map(function (rp) {
      return '<button class="btn" type="button" data-open-report="' + esc(rp.path) + '">' +
        '<svg width="14" height="14"><use href="#i-report"/></svg> ' +
        esc(rp.name.replace(/\.md$/, "")) + "</button>";
    }).join("");

    var pdfs = (r.reports || []).map(function (rp) {
      return '<button class="btn" type="button" data-pdf="' + esc(rp.path) + '">' +
        '<svg width="14" height="14"><use href="#i-download"/></svg> PDF</button>';
    }).join("");

    return '<div class="card-block">' +
      '<div class="card-block-head">' + esc(r.ok ? "分析完成" : "运行结束（有异常）") +
      (r.seconds ? '<span class="tag accent" style="margin-left:auto">' + esc(r.seconds) + " 秒</span>" : "") +
      "</div>" +
      (kpis ? '<div class="card-block-body"><div class="kpis">' + kpis + "</div></div>" : "") +
      (links ? '<div class="card-block-body" style="border-top:1px solid var(--border)">' +
        '<div class="act-row">' + links + pdfs + "</div>" +
        (r.run_id ? '<div class="msg-note">运行编号 <code>' + esc(r.run_id) +
          "</code> · 可到「执行轨迹」逐条复核</div>" : "") + "</div>" : "") +
      "</div>";
  }

  function scrollToEnd() {
    var s = $("stream");
    if (s) s.scrollTop = s.scrollHeight;
  }

  /* ---------------------------------------------------------------- 发送与执行 */

  function autoGrow() {
    var t = $("q");
    t.style.height = "auto";
    t.style.height = Math.min(t.scrollHeight, 190) + "px";
    $("btn-send").disabled = state.running || !t.value.trim();
  }

  function setRunning(on) {
    state.running = on;
    $("btn-stop").hidden = !on;
    $("btn-send").disabled = on || !$("q").value.trim();
    $("fab-log").classList.toggle("live", on);
  }

  function addAssistantMessage(patch) {
    var m = Object.assign({
      role: "assistant", events: [], text: "", streaming: true,
      startedAt: Date.now(), resultCard: null, error: null
    }, patch || {});
    state.session.messages.push(m);
    return m;
  }

  function finishAssistant(m, patch) {
    m.streaming = false;
    m.elapsed = ((Date.now() - m.startedAt) / 1000).toFixed(1) + "s";
    for (var k in patch) if (Object.prototype.hasOwnProperty.call(patch, k)) m[k] = patch[k];
    persistSessions();
    renderChat();
  }

  function send() {
    var text = $("q").value.trim();
    if (!text || state.running) return;

    if (STATIC) {
      $("q").value = "";
      autoGrow();
      var demos = (DEMO.manifest && DEMO.manifest.demos) || [];
      var hit = demos.filter(function (d) {
        return text.indexOf(d.name) >= 0 || text.indexOf(d.secucode.split(".")[0]) >= 0;
      })[0];
      if (hit) { replayDemo(hit, text); return; }
      state.session.messages.push({ role: "user", text: text });
      state.session.messages.push({
        role: "assistant",
        text: "这里是**静态展示站**，只能浏览已经跑出来的结果，不能发起新的分析。\n\n" +
          "原因很直接：完整分析需要在封闭数据环境里读公告原文、跑规则引擎并调用大模型，" +
          "这些都不适合放在一个公开的静态站点上，本站也不持有任何模型密钥。\n\n" +
          "想分析这家公司，两条路：\n\n" +
          "1. 在本地运行 `启动FinAgent应用.bat`，输入你自己的 DeepSeek 密钥即可；\n" +
          "2. 直接看下面这些**实录回放**，它们与实时运行的过程、产物完全一致。"
      });
      persistSessions();
      renderChat();
      return;
    }

    if (!hasKey()) {
      openKeyModal();
      $("key-err").textContent = "请先填写你自己的大模型 API Key，再开始分析";
      $("key-err").hidden = false;
      return;
    }
    $("q").value = "";
    autoGrow();

    var msg = { role: "user", text: text, action: parseAction(text), targets: [] };
    state.session.messages.push(msg);
    if (state.session.messages.length === 1) {
      state.session.title = text.length > 24 ? text.slice(0, 24) + "…" : text;
      state.session.ts = Date.now() / 1000;
      renderThreads();
    }
    renderChat();

    // 先解析出公司，再决定下一步：解析不到就不启动一次注定失败的运行。
    api("/api/resolve?text=" + encodeURIComponent(text)).then(function (r) {
      msg.targets = (r.items || []).map(function (x) {
        return { code: x.secucode, name: x.name, bare: x.code };
      });

      if (msg.action === "index") {
        msg.actionLabel = "动作：建立全文索引";
        persistSessions();
        renderChat();
        return runIndex();
      }
      if (!msg.targets.length) {
        msg.actionLabel = "未识别到上市公司";
        state.session.messages.push({
          role: "assistant",
          text: "我没从这句话里认出上市公司。可以这样写：\n\n" +
            "- 分析牧原股份 2026 年中报的亏损原因\n" +
            "- 600519 最近几期的毛利率\n\n" +
            "也可以直接点下面的推荐问题试一下。"
        });
        setRunning(false);
        persistSessions();
        renderChat();
        return;
      }
      msg.actionLabel = "动作：" + (msg.action === "fetch" ? "抓取公告原文" : "财务分析");
      persistSessions();
      renderChat();
      if (msg.action === "fetch") return runFetch(msg);
      return runAnalyze(msg);
    }).catch(function (e) {
      pushLog("解析分析对象失败：" + e.message, "err");
      state.session.messages.push({ role: "assistant", error: "解析失败：" + e.message });
      renderChat();
    });
  }

  /* 统一的一次运行：POST /api/run，读取 SSE 流 */
  function execute(params, msg) {
    setRunning(true);
    var shown = {};
    Object.keys(params).forEach(function (k) { if (k !== "api_key") shown[k] = params[k]; });
    pushLog("执行 " + JSON.stringify(shown), "sys");

    var ctl = new AbortController();
    state.abort = ctl;
    var llm = currentLLM() || {};
    var body = Object.assign({}, params);
    if (llm.api_key && llm.api_key !== "__server__") {
      body.api_key = llm.api_key;
      body.base_url = llm.base_url;
      body.model = llm.model;
    }

    return fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: ctl.signal
    }).then(function (resp) {
      if (!resp.ok) {
        return resp.json().catch(function () { return {}; }).then(function (j) {
          throw new Error(j.error || ("启动失败 " + resp.status));
        });
      }
      return readStream(resp, msg);
    }).catch(function (e) {
      if (e.name === "AbortError") {
        pushLog("用户中止了本次运行", "sys");
        finishAssistant(msg, { text: (msg.text || "") + "\n\n> 本次运行已被手动停止。" });
        return;
      }
      finishAssistant(msg, { error: e.message });
      pushLog("运行失败：" + e.message, "err");
    }).then(function () {
      setRunning(false);
      state.abort = null;
      refreshStatus();
    });
  }

  function readStream(resp, msg) {
    var reader = resp.body.getReader();
    var decoder = new TextDecoder("utf-8");
    var buffer = "";
    var tick = setInterval(function () {
      if (!msg.streaming) { clearInterval(tick); return; }
      msg.elapsed = ((Date.now() - msg.startedAt) / 1000).toFixed(0) + "s";
      var el = document.querySelector(".proc[data-live] .tick");
      if (el) el.textContent = msg.elapsed;
    }, 700);

    function pump() {
      return reader.read().then(function (res) {
        if (res.done) { clearInterval(tick); return; }
        buffer += decoder.decode(res.value, { stream: true });
        var parts = buffer.split("\n\n");
        buffer = parts.pop();
        parts.forEach(function (part) { handleChunk(part, msg); });
        return pump();
      });
    }
    return pump();
  }

  function handleChunk(chunk, msg) {
    var line = chunk.split("\n").filter(function (l) { return l.indexOf("data:") === 0; })[0];
    if (!line) return;
    var payload;
    try { payload = JSON.parse(line.slice(5).trim()); } catch (e) { return; }

    if (payload.kind === "line") { pushLog(payload.text, classifyLog(payload.text)); return; }
    if (payload.kind === "notice") { pushLog(payload.text, "sys"); return; }
    if (payload.kind === "trace") { appendTrace(msg, payload.event); return; }
    if (payload.kind === "start") { pushLog("命令：" + payload.command, "sys"); return; }
    if (payload.kind === "error") {
      pushLog(payload.message, "err");
      finishAssistant(msg, { error: payload.message });
      return;
    }
    if (payload.kind === "done") onDone(payload, msg);
  }

  function appendTrace(msg, ev) {
    if (!ev) return;
    msg.events.push(ev);
    // 只重绘过程块，避免整个对话重排导致滚动跳动
    var proc = document.querySelector(".proc[data-live]");
    if (proc) {
      var holder = document.createElement("div");
      holder.innerHTML = renderProcess(msg, state.session.messages.indexOf(msg));
      var fresh = holder.firstChild;
      fresh.classList.add("open");
      proc.parentNode.replaceChild(fresh, proc);
    } else {
      renderChat();
    }
    if (ev.event === "finding") {
      pushLog("结论命中 [" + (ev.level || "") + "] " + (ev.title || "") +
        (ev.rule ? "  规则 " + ev.rule : ""), "hit");
    }
  }

  function onDone(payload, msg) {
    var reports = payload.reports || [];
    var card = {
      ok: payload.code === 0,
      seconds: payload.seconds,
      run_id: payload.run_id,
      reports: reports.filter(function (r) { return /\.md$/.test(r.name); }),
      kpis: []
    };
    var next = { resultCard: card };
    if (payload.code !== 0) {
      next.error = "子进程返回码 " + payload.code + "，请展开运行日志查看原因。";
    }
    finishAssistant(msg, next);

    var mdReport = card.reports.filter(function (r) { return r.name.indexOf("跨公司") < 0; })[0]
      || card.reports[0];
    if (mdReport) {
      api("/api/report?path=" + encodeURIComponent(mdReport.path)).then(function (r) {
        var kpis = quickKpis(r.markdown);
        if (kpis.length) {
          card.kpis = kpis;
          renderChat();
          persistSessions();
        }
      }).catch(function () { });
    }
    pushLog("运行结束，返回码 " + payload.code + "，用时 " + payload.seconds + " 秒", "sys");
  }

  /* 从报告正文里抽关键指标，做对话里的速览卡片。
     取数刻意分两处，因为两处的口径并不一样：
       - 「同比与环比」小节同时给了累计值和还原后的单季度值，
         营业总收入与归母净利润优先取它，卡片上就能直接给出
         单季度同比与环比——这正是本题最看重的口径处理；
       - 其余指标只有「关键财务指标」表，那里是累计数，
         卡片上标注“累计”，不与单季数混淆。 */
  function quickKpis(markdown) {
    var lines = String(markdown || "").split("\n");

    function cells(line) {
      var t = String(line).trim();
      if (t.charAt(0) === "|") t = t.slice(1);
      if (t.charAt(t.length - 1) === "|") t = t.slice(0, -1);
      return t.split("|").map(function (s) { return s.trim(); });
    }
    /* 取从 from 开始的第一个 Markdown 表格；表格前的说明文字会被跳过。 */
    function tableAfter(from, stop) {
      var rows = [];
      for (var j = from; j < stop; j++) {
        if (/^\s*\|/.test(lines[j])) rows.push(lines[j]);
        else if (rows.length) break;
      }
      return rows.length >= 3 ? rows : null;
    }
    function col(head, name) {
      for (var h = 0; h < head.length; h++) {
        if (head[h].indexOf(name) === 0) return h;
      }
      return -1;
    }

    var secA = -1, secB = lines.length;
    for (var i = 0; i < lines.length; i++) {
      if (secA < 0 && /^##\s*二、/.test(lines[i])) secA = i;
      else if (secA >= 0 && /^##\s*三、/.test(lines[i])) { secB = i; break; }
    }

    function seasonal(name) {
      if (secA < 0) return null;
      for (var k = secA; k < secB; k++) {
        if (lines[k].indexOf("**" + name + "**") !== 0) continue;
        var rows = tableAfter(k + 1, secB);
        if (!rows) return null;
        var head = cells(rows[0]);
        var body = rows.slice(2).map(cells);
        var last = body[body.length - 1];
        if (!last || last.length !== head.length) return null;
        var iQ = col(head, "单季度值"), iY = col(head, "单季度同比"),
            iM = col(head, "单季度环比");
        if (iQ < 0) return null;
        return { period: last[0], q: num(last[iQ]),
                 yoy: iY < 0 ? NaN : num(last[iY]),
                 qoq: iM < 0 ? NaN : num(last[iM]) };
      }
      return null;
    }

    var cumHead = null, cumRows = [];
    var cumStop = secA < 0 ? lines.length : secA;
    for (var c = 0; c < cumStop; c++) {
      if (lines[c].indexOf("关键财务指标") < 0) continue;
      var rows2 = tableAfter(c + 1, lines.length);
      if (!rows2) break;
      cumHead = cells(rows2[0]);
      cumRows = rows2.slice(2).map(cells).filter(function (r) {
        return r.length === cumHead.length;
      });
      break;
    }

    function cumulative(name) {
      if (!cumHead || !cumRows.length) return null;
      var idx = col(cumHead, name);
      if (idx < 0) return null;
      var cur = cumRows[cumRows.length - 1];
      var prev = cumRows.filter(function (r) { return r[0] === lastYear(cur[0]); })[0];
      var v = num(cur[idx]), pv = prev ? num(prev[idx]) : NaN;
      var yoy = (isFinite(v) && isFinite(pv) && pv !== 0)
        ? (v - pv) / Math.abs(pv) * 100 : NaN;
      // 比率类指标的变动真实含义是百分点，不是百分比：
      // 毛利率从 20.52% 到 -1.58% 写成“-107.7%”会被误读。
      var pp = (isFinite(v) && isFinite(pv)) ? v - pv : NaN;
      return { period: cur[0], v: v, yoy: yoy, pp: pp };
    }

    /* 方向按“四舍五入后的得数”判定，保证箭头与文字不打架：
       显示 +0.00% 就不能画上涨。 */
    function dirOf(v) {
      var n = Number(v);
      if (!isFinite(n)) return "flat";
      var r = Number(n.toFixed(pctDigits(n)));
      return r > 0 ? "up" : (r < 0 ? "down" : "flat");
    }

    var PICKS = ["营业总收入", "归母净利润", "经营现金流净额", "毛利率"];
    var out = [];
    PICKS.forEach(function (name) {
      var ratio = (name === "毛利率");
      var s = seasonal(name);
      if (s && isFinite(s.q)) {
        var bits = [];
        if (isFinite(s.yoy)) bits.push("单季同比 " + pct(s.yoy));
        if (isFinite(s.qoq)) bits.push("环比 " + pct(s.qoq));
        out.push({
          k: name + "（" + s.period + " 单季）",
          v: fmtYi(s.q),
          d: bits.join(" · ") || "单季",
          dir: dirOf(s.yoy)
        });
        return;
      }
      var cu = cumulative(name);
      if (!cu || !isFinite(cu.v)) return;
      if (ratio) {
        out.push({
          k: name + "（" + cu.period + " 累计）",
          v: pct(cu.v, 2),
          d: isFinite(cu.pp) ? "较上年同期 " + signed(cu.pp, 2) + " 个百分点" : "累计",
          dir: dirOf(cu.pp)
        });
        return;
      }
      out.push({
        k: name + "（" + cu.period + " 累计）",
        v: fmtYi(cu.v),
        d: isFinite(cu.yoy) ? "累计同比 " + pct(cu.yoy) : "累计",
        dir: dirOf(cu.yoy)
      });
    });
    return out;
  }

  /* ---------------------------------------------------------------- 三个动作 */

  function runAnalyze(msg) {
    var a = addAssistantMessage({
      text: "好的，我来分析 **" + msg.targets.map(function (t) { return t.name; }).join("、") +
        "**。流程是：取财报数据 → 算同比环比 → 跑规则引擎找异常 → 勾稽校验 → 回到公告原文逐个核实。" +
        "\n\n完成后会写出带证据编号的报告，可直接下载 PDF。"
    });
    renderChat();
    var q = stripActionWords(msg.text);
    return execute({
      action: "analyze",
      codes: msg.targets.map(function (t) { return t.bare || t.code; }),
      question: q
    }, a);
  }

  function runFetch(msg) {
    var a = addAssistantMessage({
      text: "我先抓取 **" + msg.targets.map(function (t) { return t.name; }).join("、") +
        "** 的公告原文，存进本地封闭数据环境，然后重建全文索引。"
    });
    renderChat();
    var chain = Promise.resolve();
    msg.targets.forEach(function (t) {
      chain = chain
        .then(function () { return execute({ action: "fetch", code: t.bare || t.code, limit: 3 }, a); })
        .then(function () { return execute({ action: "index" }, a); });
    });
    return chain;
  }

  function runIndex() {
    var a = addAssistantMessage({
      text: "正在把已落盘的公告原文切成文本块并建立全文索引，供证据检索使用。"
    });
    renderChat();
    return execute({ action: "index" }, a);
  }

  /* ---------------------------------------------------------------- 报告页 */

  function loadReports() {
    var reports = (state.status || {}).reports || [];
    $("report-count").textContent = reports.length;
    var box = $("report-list");
    if (!reports.length) {
      box.innerHTML = '<div class="empty">还没有报告。<br>去「智能分析」里跑一次就有了。</div>';
      return;
    }
    box.innerHTML = reports.map(function (r) {
      var active = r.path === state.reportPath ? " active" : "";
      return '<button class="list-item' + active + '" type="button" data-report="' + esc(r.path) + '">' +
        '<div class="t">' + esc(r.name.replace(/\.md$/, "")) + "</div>" +
        '<div class="s">' + fmtTime(r.mtime) + " · " + fmtBytes(r.bytes) +
        (r.table ? " · 含指标宽表" : "") + (r.pdf ? " · 可下载 PDF" : "") + "</div></button>";
    }).join("");
  }

  function openReport(path) {
    state.reportPath = path;
    showView("report");
    loadReports();
    $("report-body").hidden = false;
    $("report-body").innerHTML = '<div class="empty">读取中…</div>';
    $("report-table").hidden = true;
    $("report-kpis").hidden = true;
    $("btn-report-table").hidden = true;
    $("report-name").textContent = path.split("/").pop().replace(/\.md$/, "");

    fetchReport(path).then(function (r) {
      // 报告第一行是一级标题，面板头已经显示了同名标题，正文里去掉避免重复
      var body = String(r.markdown || "").replace(/^#\s+.*\n/, "");
      $("report-body").innerHTML = FinMD.render(body);
      var kpis = quickKpis(r.markdown);
      if (kpis.length) {
        $("report-kpis").innerHTML = kpis.map(function (k) {
          return '<div class="kpi"><div class="k">' + esc(k.k) + '</div><div class="v">' +
            esc(k.v) + '</div><div class="d ' + esc(k.dir) + '">' + esc(k.d) + "</div></div>";
        }).join("");
        $("report-kpis").hidden = false;
      }
      var entry = ((state.status || {}).reports || []).filter(function (x) {
        return x.path === path;
      })[0];
      state.reportTable = entry && entry.table ? entry.table : null;
      $("btn-report-table").hidden = !state.reportTable;
      $("btn-report-table").textContent = "指标宽表";
    }).catch(function (e) {
      $("report-body").innerHTML = '<div class="msg-error">' + esc(e.message) + "</div>";
    });
  }

  function openReportTable() {
    if (!state.reportTable) return;
    var body = $("report-body"), table = $("report-table");
    if (!table.hidden) {
      table.hidden = true; body.hidden = false;
      $("btn-report-table").textContent = "指标宽表";
      return;
    }
    $("btn-report-table").textContent = "返回正文";
    fetchTable(state.reportTable).then(function (r) {
      table.innerHTML = '<table class="tbl"><thead><tr>' +
        r.header.map(function (h) { return "<th>" + esc(h) + "</th>"; }).join("") +
        "</tr></thead><tbody>" +
        r.rows.map(function (row) {
          return "<tr>" + row.map(function (c, i) {
            var isNum = i > 0 && String(c).trim() !== "" && !isNaN(num(c));
            return "<td" + (isNum ? ' class="num"' : "") + ">" + esc(c) + "</td>";
          }).join("") + "</tr>";
        }).join("") + "</tbody></table>" +
        (r.truncated ? '<div class="msg-note">共 ' + r.total + " 行，界面显示前 " +
          r.rows.length + " 行</div>" : "");
      table.hidden = false;
      body.hidden = true;
    }).catch(function (e) {
      table.innerHTML = '<div class="msg-error">' + esc(e.message) + "</div>";
      table.hidden = false;
      body.hidden = true;
    });
  }

  function downloadPDF(path) {
    var url = "/api/report.pdf?path=" + encodeURIComponent(path);
    if (STATIC) {
      // 展示站里 PDF 是构建时预生成的静态文件，直接给出即可。
      var item = ((state.status || {}).reports || []).filter(function (r) {
        return r.path === path;
      })[0];
      if (item && item.pdf) url = item.pdf;
      else { window.print(); return; }
    } else {
      pushLog("正在生成 PDF，首次生成需要几秒钟…", "sys");
    }
    var a = document.createElement("a");
    a.href = url;
    a.download = "";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  }

  /* ---------------------------------------------------------------- 轨迹页 */

  function loadTraces() {
    var traces = (state.status || {}).traces || [];
    $("count-report").textContent = ((state.status || {}).reports || []).length;
    $("count-trace").textContent = traces.length;
    $("trace-count").textContent = traces.length;
    var box = $("trace-list");
    if (!traces.length) {
      box.innerHTML = '<div class="empty">还没有运行记录。</div>';
      return;
    }
    box.innerHTML = traces.slice(0, 60).map(function (t) {
      var active = t.run === state.traceRun ? " active" : "";
      return '<button class="list-item' + active + '" type="button" data-trace="' + esc(t.run) + '">' +
        '<div class="t">' + esc(t.run) + "</div>" +
        '<div class="s">' + fmtTime(t.mtime) + " · " + fmtBytes(t.bytes) + "</div></button>";
    }).join("");
  }

  function openTrace(run) {
    state.traceRun = run;
    state.traceFilter = "全部";
    loadTraces();
    $("trace-title").textContent = run;
    $("trace-body").innerHTML = '<div class="empty">读取中…</div>';
    var entry = ((state.status || {}).traces || []).filter(function (t) {
      return t.run === run;
    })[0];
    fetchTrace(run, entry && entry.file).then(function (r) {
      state.traceEvents = Array.isArray(r) ? r : (r.events || []);
      renderTraceFilters();
      renderTraceBody();
    }).catch(function (e) {
      $("trace-body").innerHTML = '<div class="msg-error">' + esc(e.message) + "</div>";
    });
  }

  function renderTraceFilters() {
    var counts = {};
    state.traceEvents.forEach(function (e) {
      var k = e.event || "其他";
      counts[k] = (counts[k] || 0) + 1;
    });
    var kinds = Object.keys(counts).sort(function (a, b) { return counts[b] - counts[a]; });
    var html = '<button class="chip-btn' + (state.traceFilter === "全部" ? " warn" : "") +
      '" type="button" data-filter="全部">全部 ' + state.traceEvents.length + "</button>";
    kinds.forEach(function (k) {
      html += '<button class="chip-btn' + (state.traceFilter === k ? " warn" : "") +
        '" type="button" data-filter="' + esc(k) + '">' +
        esc(TRACE_LABEL[k] || k) + " " + counts[k] + "</button>";
    });
    $("trace-filters").innerHTML = html;
  }

  function renderTraceBody() {
    var evs = state.traceEvents.filter(function (e) {
      return state.traceFilter === "全部" || (e.event || "其他") === state.traceFilter;
    });
    if (!evs.length) {
      $("trace-body").innerHTML = '<div class="empty">该类型下没有事件。</div>';
      return;
    }
    $("trace-body").innerHTML = '<table class="tbl"><thead><tr>' +
      "<th>#</th><th>时间</th><th>事件</th><th>内容</th><th>原始字段</th>" +
      "</tr></thead><tbody>" + evs.map(function (e, i) {
        var raw = {};
        for (var k in e) if (["event", "ts", "time"].indexOf(k) < 0) raw[k] = e[k];
        return '<tr><td class="num">' + (i + 1) + "</td>" +
          '<td class="num">' + esc(e.ts ? String(e.ts).slice(11, 19) : "") + "</td>" +
          '<td><span class="tag ' + tagOf(e.event) + '">' +
          esc(TRACE_LABEL[e.event] || e.event || "") + "</span></td>" +
          "<td>" + esc(describeEvent(e)) + "</td>" +
          '<td style="font-family:var(--mono);font-size:11px;color:var(--muted)">' +
          esc(JSON.stringify(raw).slice(0, 200)) + "</td></tr>";
      }).join("") + "</tbody></table>";
  }

  function tagOf(kind) {
    if (kind === "finding") return "anomaly";
    if (kind === "file_access" || kind === "tool_call") return "accent";
    if (kind === "skip" || kind === "corpus_missing") return "structural";
    return "caliber";
  }

  /* ---------------------------------------------------------------- 环境页 */

  function loadEnv() {
    var st = state.status || {};
    var corpus = st.corpus || [];
    var docs = corpus.reduce(function (a, c) { return a + (c.documents || 0); }, 0);
    var bytes = corpus.reduce(function (a, c) { return a + (c.bytes || 0); }, 0);

    $("env-stats").innerHTML = [
      { k: "全市场可分析主体", v: (st.universe_total || 0).toLocaleString(),
        s: "沪深主板 / 科创 / 创业 / 北交所 / B 股" },
      { k: "已落盘公告原文", v: docs + " 份",
        s: fmtBytes(bytes) + " · " + corpus.length + " 家公司" },
      { k: "全文索引文本块", v: ((st.index && st.index.chunks) || 0).toLocaleString(),
        s: "用于证据检索" },
      { k: "推理模型", v: st.model || "—",
        s: st.api_key_set ? "服务端密钥已配置" : "由使用者在界面填写" }
    ].map(function (x) {
      return '<div class="stat"><div class="k">' + esc(x.k) + '</div><div class="v">' +
        esc(x.v) + '</div><div class="s">' + esc(x.s) + "</div></div>";
    }).join("");

    $("env-corpus").innerHTML = corpus.length
      ? '<table class="tbl"><thead><tr><th>代码</th><th>公司</th><th>公告</th><th>占用</th>' +
        "<th>报告类型</th></tr></thead><tbody>" +
        corpus.map(function (c) {
          var entry = (st.companies || []).filter(function (x) {
            return String(x.code).indexOf(c.code) === 0;
          })[0];
          var kinds = (c.kinds || []).map(function (k) {
            return { annual: "年报", semi: "半年报", q1: "一季报", q3: "三季报" }[k] || k;
          }).join("、");
          return "<tr><td>" + esc(c.code) + "</td><td>" + esc(entry ? entry.name : "—") +
            '</td><td class="num">' + c.documents + '</td><td class="num">' + fmtBytes(c.bytes) +
            "</td><td>" + esc(kinds) + "</td></tr>";
        }).join("") + "</tbody></table>"
      : '<div class="empty">尚未构建数据环境。运行 <code>python run.py fetch</code> 后这里会列出已落盘的公告。</div>';

    $("env-universe-total").textContent = (st.universe_total || 0).toLocaleString() + " 只";

    var samples = st.coverage_samples || [];
    $("env-samples").innerHTML = samples.length
      ? '<table class="tbl"><thead><tr><th>代码</th><th>名称</th><th>板块 / 报表族</th></tr></thead><tbody>' +
        samples.map(function (s) {
          return "<tr><td>" + esc(s.code) + "</td><td>" + esc(s.name) + "</td><td>" +
            esc(s.segment) + "</td></tr>";
        }).join("") + "</tbody></table>"
      : '<div class="empty">暂无抽样数据。</div>';

    var thresholds = st.thresholds || {};
    var TH_LABEL = {
      impairment_yoy_pct: ["减值同比增幅阈值", "%", "超过即判为异常信号 R1"],
      impairment_min_amount: ["减值绝对额门槛", "元", "金额太小不判异常，避免噪声"],
      nonrecurring_ratio_pct: ["非经常性损益占比阈值", "%", "超过则提示利润质量"],
      adjusted_cash_conv_low: ["调整后现金含量下限", "倍", "低于则利润与现金流背离"],
      adjusted_cash_conv_high: ["调整后现金含量上限", "倍", "高于可能是结构性问题"],
      qoq_swing_pct: ["单季度环比波动阈值", "%", "超过提示季节性以外的大幅波动"],
      tax_rate_high_pct: ["实际税率异常阈值", "%", "亏损期仍有税负等情形"],
      collect_ratio_low: ["收现比下限", "倍", "低于则收入回款质量存疑"],
      articulation_tol_pct: ["勾稽校验容差", "%", "表间差异超过即报告"],
      depr_to_revenue_high_pct: ["折旧摊销占收入比", "%", "判断重资产结构性特征"]
    };
    var rows = Object.keys(thresholds).map(function (k) {
      var meta = TH_LABEL[k] || [k, "", ""];
      var amount = thresholds[k];
      if (meta[1] === "元" && Math.abs(Number(amount)) >= 1e8) {
        amount = (amount / 1e8) + " 亿";
      } else {
        amount = amount + " " + meta[1];
      }
      return "<tr><td>" + esc(meta[0]) + '</td><td class="num">' + esc(amount) +
        "</td><td>" + esc(meta[2]) + "</td></tr>";
    }).join("");
    $("env-thresholds").innerHTML =
      '<table class="tbl"><thead><tr><th>阈值</th><th>取值</th><th>作用</th></tr></thead><tbody>' +
      rows + "</tbody></table>";

    $("env-legend").innerHTML = [
      ["anomaly", "异常信号", "数据偏离常态，需要解释。例如减值激增、亏损期确认所得税。"],
      ["structural", "结构性特征", "看似异常，实为资产结构或行业规律使然。例如重资产企业的低现金含量。"],
      ["caliber", "口径提示", "计算口径本身有局限，比率不具可比性。例如银行业的现金含量。"]
    ].map(function (x) {
      return '<div style="margin-bottom:11px"><span class="tag ' + x[0] + '">' + x[1] +
        '</span><div style="font-size:12.5px;color:var(--muted);margin-top:5px">' +
        esc(x[2]) + "</div></div>";
    }).join("");
  }

  /* ---------------------------------------------------------------- 侧栏与状态 */

  function renderThreads() {
    var box = $("threads");
    if (!state.sessions.length) {
      box.innerHTML = '<div class="empty" style="padding:16px 6px;text-align:left">暂无记录</div>';
      return;
    }
    box.innerHTML = state.sessions.slice(0, 24).map(function (s) {
      var active = state.session && s.id === state.session.id ? " active" : "";
      return '<button class="thread' + active + '" type="button" data-session="' + esc(s.id) + '">' +
        esc(s.title) + '<span class="thread-meta">' + fmtTime(s.ts) + "</span></button>";
    }).join("");
  }

  function refreshStatus() {
    if (STATIC) {
      return loadManifest().then(function (m) {
        state.status = m.status;
        // 统一成与实测模式相同的字段名，后面的渲染逻辑无需分叉
        state.status.reports = (m.reports || []).map(function (r) {
          return {
            name: r.name, path: "demo/reports/" + r.slug + ".md",
            mtime: r.mtime, bytes: r.bytes,
            table: r.table, pdf: r.pdf, csv: r.csv
          };
        });
        state.status.traces = m.traces || [];
        $("count-report").textContent = m.reports.length;
        $("count-trace").textContent = m.traces.length;
        if (state.view === "report") loadReports();
        if (state.view === "trace") loadTraces();
        if (state.view === "env") loadEnv();
      }).catch(function (e) { pushLog("读取展示数据失败：" + e.message, "err"); });
    }
    return api("/api/status").then(function (r) {
      state.status = r.status;
      refreshKeyUI();
      $("count-report").textContent = (r.status.reports || []).length;
      $("count-trace").textContent = (r.status.traces || []).length;
      if (state.view === "report") loadReports();
      if (state.view === "trace") loadTraces();
      if (state.view === "env") loadEnv();
    }).catch(function (e) { pushLog("读取状态失败：" + e.message, "err"); });
  }

  /* ---------------------------------------------------------------- 事件绑定 */

  function bind() {
    document.querySelectorAll(".nav-item").forEach(function (b) {
      b.addEventListener("click", function () { showView(b.getAttribute("data-view")); });
    });

    $("btn-menu").addEventListener("click", function () {
      var app = $("app");
      if (window.innerWidth <= 860) app.classList.toggle("rail-open");
      else app.classList.toggle("rail-hidden");
    });
    $("mask").addEventListener("click", closeDrawer);

    $("btn-new").addEventListener("click", function () {
      if (state.running) return;
      newSession(false);
      renderThreads();
      renderChat();
      showView("chat");
      $("q").focus();
    });

    $("threads").addEventListener("click", function (ev) {
      var b = ev.target.closest("[data-session]");
      if (!b || state.running) return;
      var found = state.sessions.filter(function (s) {
        return s.id === b.getAttribute("data-session");
      })[0];
      if (!found) return;
      state.session = found;
      renderThreads();
      renderChat();
      showView("chat");
    });

    var q = $("q");
    q.addEventListener("input", autoGrow);
    q.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) {
        ev.preventDefault();
        send();
      }
    });
    $("btn-send").addEventListener("click", send);
    $("btn-stop").addEventListener("click", function () {
      if (state.abort) state.abort.abort();
    });

    $("messages").addEventListener("click", function (ev) {
      var proc = ev.target.closest("[data-proc]");
      if (proc) { proc.parentElement.classList.toggle("open"); return; }

      var drop = ev.target.closest("[data-drop]");
      if (drop) {
        var parts = drop.getAttribute("data-drop").split(":");
        var m = state.session.messages[Number(parts[0])];
        if (m && m.targets) m.targets.splice(Number(parts[1]), 1);
        renderChat();
        persistSessions();
        return;
      }
      var rep = ev.target.closest("[data-open-report]");
      if (rep) { openReport(rep.getAttribute("data-open-report")); return; }
      var pdf = ev.target.closest("[data-pdf]");
      if (pdf) { downloadPDF(pdf.getAttribute("data-pdf")); return; }
    });

    $("btn-key").addEventListener("click", openKeyModal);
    $("btn-key-2").addEventListener("click", openKeyModal);
    $("btn-key-cancel").addEventListener("click", closeKeyModal);
    $("btn-key-test").addEventListener("click", testKey);
    $("btn-key-save").addEventListener("click", saveKey);
    $("in-key").addEventListener("keydown", function (ev) {
      if (ev.key === "Enter") saveKey();
    });

    $("btn-theme").addEventListener("click", function () {
      var now = document.documentElement.getAttribute("data-theme");
      applyTheme(now === "dark" ? "light" : "dark", true);
    });

    $("fab-log").addEventListener("click", openDrawer);
    $("btn-log-close").addEventListener("click", closeDrawer);
    $("btn-log-clear").addEventListener("click", function () {
      $("logbox").innerHTML = "";
      state.logLines = 0;
    });

    $("report-list").addEventListener("click", function (ev) {
      var b = ev.target.closest("[data-report]");
      if (b) openReport(b.getAttribute("data-report"));
    });
    $("btn-report-table").addEventListener("click", openReportTable);
    $("btn-report-pdf").addEventListener("click", function () {
      if (state.reportPath) downloadPDF(state.reportPath);
    });

    $("trace-list").addEventListener("click", function (ev) {
      var b = ev.target.closest("[data-trace]");
      if (b) openTrace(b.getAttribute("data-trace"));
    });
    $("trace-filters").addEventListener("click", function (ev) {
      var b = ev.target.closest("[data-filter]");
      if (!b) return;
      state.traceFilter = b.getAttribute("data-filter");
      renderTraceFilters();
      renderTraceBody();
    });

    document.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") { closeKeyModal(); closeDrawer(); }
    });

    window.addEventListener("resize", function () {
      if (window.innerWidth > 860) $("app").classList.remove("rail-open");
    });
  }

  /* ---------------------------------------------------------------- 展示模式 */

  function applyStaticMode() {
    if (!STATIC) return;
    if ($("btn-key")) $("btn-key").hidden = true;
    if ($("btn-key-2")) $("btn-key-2").hidden = true;
    $("model-chip").innerHTML = '<span class="dot"></span><span>静态展示站 · 只读</span>';
    $("composer-hint").textContent =
      "本站为只读展示，内容由本地真实运行产生；发起新分析请在本地启动 FinAgent";
    $("welcome").querySelector("h1").textContent = "看一遍真实的分析过程";
    $("welcome").querySelector("p").textContent =
      "下面是几次真实运行的实录回放：点开后可以看到系统调了哪些工具、读了哪些文件、" +
      "算出了什么，以及最终产出的报告。全部内容都可在本地用同一份源码复现。";
    if ($("btn-new")) $("btn-new").textContent = "清空当前对话";
    pushLog("当前为静态展示模式：数据来自 demo/manifest.json，不会发起任何后台调用。", "sys");
  }

  /* ---------------------------------------------------------------- 启动 */

  function boot() {
    initTheme();
    loadSessions();
    renderSuggests();
    renderThreads();
    renderChat();
    bind();
    autoGrow();

    applyStaticMode();
    pushLog("界面已就绪。全部结论都由 run.py 产出，本界面不做任何金融计算。", "sys");
    refreshStatus().then(function () {
      if (STATIC) renderSuggests();
      else if (!hasKey()) pushLog("尚未配置大模型密钥，首次分析前需要在界面填写。", "sys");
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }

  window.FinAgent = { state: state, showView: showView, openReport: openReport };
})();
