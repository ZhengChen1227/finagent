# FinAgent —— 上市公司财务报告分析智能体

面向 **2026 年北京市大学生金融人工智能竞赛**（主题：金融投研智能体构建）
选题 2「上市公司财务报告分析」的参赛作品。

系统能够在受控数据环境中完成**信息检索、数据处理、逻辑核验、量化计算与报告生成**
五类任务，全部过程可核验、可追溯、可复现。

---

## 一、核心设计原则

### 算数交给代码，大模型只做归因

这不是一句口号，而是系统能够通过「数据与计算准确性」「可追溯」「可复现」三项评审的
结构性前提。只要模型有机会参与任何一个数字的产生，结果的可复现性就不再成立。

因此本系统把职责做成了**硬边界**：

| 层次 | 负责方 | 产出 | 可复现性 |
|---|---|---|---|
| 取数、口径还原、指标计算、规则判定、勾稽校验 | 确定性代码 | 事实、数值、证据 ID | 完全可复现 |
| 归因假设、验证路径、证据强度评估 | 大语言模型 | 推论（强制标注置信度） | 依赖模型，故与事实分区呈现 |

这条边界写在提示词的第一条铁律里，也体现在数据模型上：
`Finding` 对象把 `statement`（事实）与 `interpretation`（推论）分成两个字段，
在代码层面就不允许两者混写。

### 决策权归属决定"是不是智能体"

固定流水线的执行路径由代码写死；本系统的执行路径由模型在每一步现场决定：

```
规则引擎报出「资产减值损失同比 +15,347.9%」
      ↓
模型自主判断：需要回原文核实才能定性
      ↓
模型调用 search_disclosure(query="资产减值损失 存货跌价准备")
      ↓
读到附注原文后，模型决定是否继续追查、还是证据已足
      ↓
输出结论，或继续下一步
```

---

## 二、快速开始

两种入口，任选其一：

- **图形界面**：双击 `启动FinAgent应用.bat`，浏览器自动打开操作界面，点按钮即可跑完整流程
- **命令行**：按下面四步走

界面只是 `run.py` 的外壳——它把参数翻译成命令行并转发输出，不参与任何计算。
同一组参数在界面与终端运行结果完全一致，轨迹照常落在 `output/traces/`。

给队友用有四种方式（详见 `docs/使用指南.md` 第十三节）：

- **公网展示站**（队友零安装、跨网络）：`python scripts/构建展示站.py --zip`，
  把 `dist/FinAgent-展示站.zip` 传到 Cloudflare Pages 或 GitHub Pages，得到一个只读网址
- **Git 克隆**（长期协作，推荐）
- 局域网共享（队友零安装，需在同一网络）
- 拷贝文件夹 + `一键安装依赖.bat`（各人独立环境）

界面右上角填自己的模型 API Key 即可开始分析，密钥不落盘；报告可一键导出 PDF。

### 0. 从仓库开始（推荐）

```bash
git clone https://github.com/ZhengChen1227/finagent.git finagent
cd finagent
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

生成给队友的分发包：

```bash
python scripts/打包分发.py                # 仅代码
python scripts/打包分发.py --with-corpus  # 连公告原文一起（约 74MB）
```

打包脚本在写 zip 前会扫描每个待入包文件，命中疑似密钥即中止，
确保 `config.local.yaml` 里的个人密钥不会被误发出去。

### 1. 安装依赖

```bash
pip install -r requirements.txt
python tests/check_env.py     # 核对依赖版本
```

### 2. 构建封闭数据环境

```bash
python run.py fetch     # 从巨潮资讯网抓取公告原文，落盘到 data/corpus/
python run.py index     # 抽取文本并建立全文索引
```

完成后系统运行期**不再访问外网**，检索与引用全部基于本机文件。

### 3. 执行分析

```bash
python run.py analyze
```

也可以带一个具体问题，让归因部分优先回答它：

```bash
python run.py analyze --code 002714 --question "为什么上半年由盈转亏"
```

### 4. （可选）接入大模型

未配置模型时系统自动降级为确定性离线模式，计算、校验、报告生成能力不受影响。
接入任意 OpenAI 兼容接口（鼓励国产模型）：

```bash
set FINAGENT_API_KEY=sk-xxx
set FINAGENT_BASE_URL=https://api.deepseek.com/v1
set FINAGENT_MODEL=deepseek-chat
python run.py analyze
```

| 模型 | base_url | 示例 model |
|---|---|---|
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-plus` |
| Kimi | `https://api.moonshot.cn/v1` | `moonshot-v1-32k` |

### 5. 以 MCP 协议对外暴露工具

```bash
python run.py mcp
```

任何支持 MCP 的宿主均可调用同一套工具，得到与系统内部完全一致的结果。

### 6. 导出 PDF 报告

报告页与对话里的报告卡片都带 PDF 按钮。服务端把 Markdown 渲染成打印级 HTML，
再调用本机已装的 Edge / Chrome 无头模式打印，**不引入额外 Python 依赖**；
找不到浏览器时自动退化为浏览器打印对话框。

命令行导出（供静态站点固化产物时使用）：

```bash
python scripts/构建展示站.py --zip     # 同时预生成全部报告的 PDF
python scripts/构建展示站.py --no-pdf  # 跳过 PDF，构建更快
```

---

## 三、命令一览

| 命令 | 说明 |
|---|---|
| `python run.py fetch [--code CODE] [--limit N]` | 抓取公告原文，构建封闭数据环境 |
| `python run.py index [--force]` | 抽取 PDF 文本并建立全文索引 |
| `python run.py analyze [--code CODE] [--since-year N] [--periods N] [--quiet]` | 执行完整分析 |
| `python run.py search 茅台 牧原` | 在全市场名录中检索公司代码（支持简称） |
| `python run.py mcp` | 以 MCP 协议暴露工具集 |

`--code` 参数三种写法都接受：纯代码 `002714`、带后缀 `002714.SZ`、公司简称 `牧原股份`。
输入简称时会先在 6259 只证券的名录里解析出唯一主体，再据此取数。

---

## 三·补、全市场覆盖

选题要求"从年度报告、半年度报告和季度报告等材料中提取相关财务指标"，
因此系统**不针对特定公司定制**：名录内的任意主体都能直接分析。

| 覆盖维度 | 说明 |
|---|---|
| 上市地 | 沪市主板、科创板、沪市 B 股、深市主板、创业板、深市 B 股、北交所 |
| 报表格式 | 一般工商业、银行、保险、证券四套报表族，按资产负债表实际命中的族自动路由 |
| 科目口径 | 同一科目在不同报表族下名称不同（如营业总收入 `TOTAL_OPERATE_INCOME` / `OPERATE_INCOME`），逐项建立回退链 |

两个已修复的隐蔽缺陷，说明"能分析所有公司"不是自动成立的：

1. **金融业三张表全空**。东方财富对银行、保险、证券使用专用报表名
   （`BINCOME` / `IINCOME` / `SINCOME`），沿用工商业报表名会取回空表，
   而系统不会报错。现已按报表族路由，并以资产负债表作为族的判别依据。

2. **北交所数据分裂在两个代码下**。部分由新三板平移而来的公司，
   资产负债表挂在 `.BJ` 上，利润表与现金流量表却只挂在 `.NQ` 上
   （如 830964 润农节水、830809 安达科技）。若只用资产负债表确定主体代码，
   这些公司会被读成"没有利润表"且不报错。现已改为**按报表逐个确认取数代码**。

覆盖度由 `tests/test_market_coverage.py`（全离线）与联网抽样巡检共同保证。
抽样覆盖沪深北与 B 股共 132 只证券，三大报表取数通过率 132/132。

---

## 四、目录结构

```
finagent/
  trace.py                    全链路可追溯日志（JSONL，seq 单调，可逐条重放）
  config.py                   配置加载（阈值集中管理，便于复现与调参）
  util.py                     JSON 序列化工具

  datasource/
    schema.py                 字段口径映射表（规范名 → 报表 / 源字段 / 中文标签）
    cninfo.py                 巨潮资讯网：权威公告原文的获取与本地化
    eastmoney.py              东方财富：第三方核验源

  corpus/
    index.py                  本地全文索引（字符二元组 + BM25）

  metrics/
    periods.py                口径还原：累计值 → 单季度值、同比、环比
    indicators.py             衍生指标（含"调整后现金含量"）

  validate/
    models.py                 Evidence / Finding 数据模型（事实与推论强制分离）
    articulation.py           勾稽校验（资产负债表恒等式、间接法反推）
    anomaly.py                异常信号规则引擎（7 条规则）

  agent/                      ← 智能体核心
    llm.py                    OpenAI 兼容客户端（支持 Function Calling）
    tools.py                  工具注册表（11 个工具）
    loop.py                   编排循环（ReAct + 三重终止条件 + 离线降级）
    attributor.py             单轮归因（轻量路径）
    prompts/
      __init__.py             Prompt 加载与版本哈希
      system_agent.md         智能体系统提示
      attribution.md          归因提示

  skills/                     ← 技能库
    __init__.py               技能注册表（front-matter 解析、任务匹配、版本哈希）
    cash_flow_quality.md      现金流质量分析
    earnings_attribution.md   业绩变动归因
    nonrecurring_items.md     非经常性损益与利润质量识别
    accounting_change.md      会计口径变化识别

  mcp_server.py               MCP 服务端（stdio）

  report/
    builder.py                结构化报告生成

app/                          ← 图形界面（纯标准库，无新增第三方依赖）
  server.py                   本地服务端：启动子进程、转发日志、实时跟随轨迹、
                              校验用户密钥、导出 PDF、自然语言识别公司
  pdfexport.py                Markdown → 打印级 HTML → 无头浏览器打印 PDF
  static/
    index.html                页面结构（对话 / 报告 / 轨迹 / 环境）
    style.css                 样式（含深色主题与响应式）
    md.js                     极简 Markdown 渲染
    app.js                    交互逻辑、会话管理、轨迹回放

启动FinAgent应用.bat           双击启动图形界面
启动FinAgent应用（局域网共享）.bat  以 0.0.0.0 启动，队友在同一局域网内用浏览器访问
一键安装依赖.bat               创建 .venv 隔离环境并安装依赖（队友首次使用跑这个）
run.py                        CLI 入口
config.yaml                   配置文件
.gitignore                    版本库忽略规则（排除 .venv、公告原文、索引与缓存）
scripts/构建展示站.py           把真实产物固化成只读静态站（含 PDF），用于公网展示
docs/使用指南.md               完整操作手册（环境搭建、命令、配置、排错）
docs/团队协作.md               分发与分工说明
THIRD_PARTY.md                第三方依赖与数据来源说明
requirements.txt              依赖清单（版本固定）
tests/                        测试与自检
```

---

## 五、十一个工具（Tool）

工具描述本身即 Prompt——`description` 字段直接进入模型的决策上下文。

| 工具 | 用途 |
|---|---|
| `list_corpus` | 查看本地已落盘的公告原文清单 |
| `list_periods` | 查看某公司可用的报告期 |
| `get_indicator` | 读取指标值（附证据 ID、计算公式、来源） |
| `compute_growth` | 计算同比与环比 |
| `run_anomaly_rules` | 运行异常信号规则引擎 |
| `check_articulation` | 执行勾稽校验 |
| `search_disclosure` | 公告全文检索（返回原文片段与页码） |
| `read_page` | 精确读取指定公告的指定页 |
| `compare_companies` | 同口径跨公司对照 |
| `list_skills` | 列出可用的分析方法论（技能库） |
| `load_skill` | 加载指定技能的完整步骤与判读标准 |

---

## 六、技能库（Skill）

技能与工具分工不同：**工具回答"能做什么"，技能回答"该怎么做"**。

技能以 Markdown + YAML front-matter 外置存放，包含适用场景、执行步骤、
判读规则和**常见误判提醒**。系统按任务关键词自动匹配相关技能并注入模型上下文——
只加载本次任务真正相关的方法论，而不是把所有规则一股脑塞进提示词。

| 技能 | 适用场景 | 关键判读标准 |
|---|---|---|
| `cash_flow_quality` | 利润与现金流出现差距 | 必须先看折旧摊销占收入比重，避免把重资产结构性特征误报为异常 |
| `earnings_attribution` | 业绩显著变动 | 累计同比与单季度同比背离意味着拐点，比单看累计同比更重要 |
| `nonrecurring_items` | 评估利润可持续性 | 归母净利为负时占比失去意义，不得机械套用阈值 |
| `accounting_change` | 同比出现无法解释的跳变 | 折旧年限变更一年可释放数十亿利润，且不改变任何实际经营 |

技能内容哈希同样写入执行轨迹，使**方法论本身的演进也可追溯**。

## 七、关键口径约定

这些约定是本系统区别于普通财务分析的地方，也是准确性的来源：

1. **环比必须用单季度数**。A 股定期报告披露年初至今累计数，用累计数直接相除得到的
   "环比"是错的。系统统一先将累计数还原为单季度值（`Qn = 累计n − 累计n−1`）。
   例：牧原 2026 中报累计收入 594.10 亿、一季报 298.94 亿，还原出 Q2 单季 295.17 亿，
   环比 −1.26%；若直接相除会得出"环比 +98.7%"的错误结论。

2. **调整后现金含量**。朴素口径 `经营现金流 / 净利润` 对重资产企业会严重失真——
   折旧摊销一年数百亿，会把比值推到 8 倍以上并被误读为异常。系统改用
   `经营现金流 / (净利润 + 折旧摊销 + 减值计提)`（EBITDA 近似口径），
   重资产与轻资产企业才具备横向可比性。
   例：京东方 2025 年报朴素口径 9.71 倍，调整后 1.02 倍。

3. **季报口径缺失不臆造**。季报不披露现金流量表补充资料，折旧摊销不可得。
   此时调整后现金含量**留空**，而不是用近似值替代——否则公式会静默退化为朴素口径，
   给出一个看起来正常的假值。

4. **分母非正时比率不输出**。亏损期间的现金含量等比率不具经济含义，
   系统置空并打标，绝不输出误导性数字。

5. **损失类科目按绝对额比较**。减值损失等科目的同比若直接用负值计算会得出
   无意义的比率，系统按绝对额增幅判断。

---

## 八、结论分类

规则引擎把结论严格分为三类，混为一谈是财务分析中最典型的误判：

- **异常信号**：数据偏离常态，需要解释（如减值暴增、亏损期确认大额所得税）
- **结构性特征**：看似异常，实为资产或行业结构使然（如重资产企业现金含量虚高）
- **口径提示**：计算口径本身存在限制，提醒读者不要误读

---

## 九、可追溯与可复现机制

| 机制 | 实现 |
|---|---|
| 数值可回溯 | 每个数值携带证据 ID（如 `EM-09B2C930D1`），可定位到数据源字段与计算公式 |
| 原文可定位 | 报告结论引用的原文标注公告名称与页码，可凭 `read_page` 逐字核对 |
| 过程可重放 | 全部动作写入 `output/traces/*.jsonl`，`seq` 单调递增，可逐条重放 |
| 输入可冻结 | 取数与语料均有本地缓存，同一次运行的输入不随数据源更新漂移 |
| 提示词可版本化 | 每次运行的提示词哈希写入轨迹，"这份输出由哪版提示词产生"可验证 |
| 计算可隔离 | 模型不参与任何数值计算，计算路径完全确定 |
| 环境可核对 | `tests/check_env.py` 核对依赖版本 |
| 无密钥可复现 | 未配置模型时自动降级为确定性离线模式，任务流程与输出结构不变 |

---

## 十、测试

```bash
python tests/check_env.py        # 环境与依赖版本自检
python tests/smoke.py            # 主链路冒烟测试（含勾稽校验）
python tests/test_rules.py       # 规则引擎输出检查
python tests/test_tools.py       # 工具层调用检查
python tests/test_agent.py       # 编排循环检查
python tests/test_reproducibility.py  # 端到端复现性验证
python tests/test_mcp.py         # MCP 协议握手与工具调用
python tests/test_app.py         # 应用界面与接口（静态资源、报告/宽表/轨迹、参数校验、越权防护）
python tests/test_market_coverage.py  # 全市场覆盖（代码归一化、报表族路由、分表代码、金融业定性）
```

一键跑完全部 10 套：

```bash
python tests/run_all.py
```

---

## 十一、第三方依赖

见 `THIRD_PARTY.md`。本项目不使用任何第三方智能体框架、金融指标库或商业数据库，
编排框架、工具集、指标公式、校验规则与报告生成均为自主实现。

图形界面同样只用 Python 标准库（`http.server`）与原生 JavaScript 实现，
未引入 Web 框架或前端库，因此新增界面没有给评审复现增加任何依赖。
