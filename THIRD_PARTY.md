# 第三方依赖、模型与数据来源说明

本文件依据竞赛要求编制：**使用第三方开源项目、模型、数据或代码的，应列明名称、版本、
来源、许可证及具体使用范围，不得将第三方成果冒充为自主开发成果。**

## 自主开发范围声明

以下模块**全部为本团队自主开发**，不含第三方代码：

- 智能体编排框架（ReAct 循环、终止条件、轨迹记录）
- 工具集（Tool）与技能（Skill）定义
- 财报 PDF 版面抽取、主体识别、报表装配与交叉校验闸门
- canonical 字段契约与科目同义词表
- 指标计算层、勾稽校验层、异常规则引擎
- 报告生成、结构化结论导出、执行轨迹记录
- 本地 Web 界面（服务端 + 前端）

第三方内容**仅限**下表所列，且均用于通用基础能力（数值计算、HTTP 通信、PDF 解析、协议实现），
**不涉及本项目的金融分析逻辑**。

---

## 一、运行依赖（Python 包）

| 名称 | 版本 | 来源 | 许可证 | 使用范围 |
|---|---|---|---|---|
| pandas | 2.3.3 | PyPI | BSD-3-Clause | 财务数据表结构、指标宽表、报告期序列对齐 |
| numpy | 2.4.1 | PyPI | BSD-3-Clause | 数值计算与类型转换（作为 pandas 依赖间接使用） |
| pypdf | 6.11.0 | PyPI | BSD-3-Clause | 解析用户上传的财报 PDF，按 layout 模式抽取文本与表格 |
| httpx | 0.28.1 | PyPI | BSD-3-Clause | 调用 DeepSeek 的 OpenAI 兼容接口（含 Function Calling） |
| requests | 2.32.5 | PyPI | Apache-2.0 | 仅在「测试连接」按钮中请求 DeepSeek 的 `/models` 校验密钥 |
| PyYAML | 6.0.3 | PyPI | MIT | `config.yaml` 配置解析 |
| mcp | 1.27.1 | PyPI | MIT | Model Context Protocol 服务端的协议实现 |

版本以 `requirements.txt` 为准（全部固定版本）；实际安装版本可用
`python tests/check_env.py` 核对。

### 图形界面

界面（`app/`）**不引入任何第三方依赖**：服务端基于 Python 标准库 `http.server`，
前端为原生 HTML / CSS / JavaScript，**不加载任何 CDN 资源**。
界面只负责上传转发与结果渲染，不参与任何计算，也不改变核心流程的产物与轨迹。

导出的 PDF 由本机已安装的 Edge / Chrome 以无头模式打印生成（`app/pdfexport.py`），
**不引入 PDF 排版库，也不打包字体文件**。

### 版式设计参考

界面版式参考了下列开源设计规范（**仅参考其设计准则，未复制任何代码**）：

| 名称 | 版本 | 来源 | 许可证 | 使用范围 |
|---|---|---|---|---|
| bexa-dashboard | 1.2.0 | https://github.com/BekhruzTursunboev/Bexa-professional-frontend-design-skills-for-ai-agents 的 `skills/bexa-dashboard` | MIT | 数据密集型工作台的版式准则：侧边导航 + 主区骨架、面板高度跟随视口、高密度界面用分隔线而非成排卡片承载 KPI、数值统一等宽并开启 `tabular-nums`、正负向数值使用非荧光色 |

该规范为提示词形态的设计指引，不含可执行代码，因此没有产生任何代码级依赖；
`app/static/style.css` 中的全部样式均为自主编写。

---

## 二、第三方大语言模型

竞赛要求「至少使用一个大语言模型作为核心推理引擎，鼓励采用国产模型」。
本项目选用**国产模型 DeepSeek**，且**不包含任何模型权重**，只通过 HTTP 接口调用。

| 项目 | 说明 |
|---|---|
| 名称 | DeepSeek（深度求索） |
| 模型版本 | `deepseek-chat`（默认）/ `deepseek-reasoner`，由使用者在界面中选择 |
| 调用方式 | HTTP POST `https://api.deepseek.com/v1/chat/completions`，OpenAI 兼容协议，使用 Function Calling |
| 密钥 | 使用者自行申请并填写，费用自付；密钥只存本机 `config.local.yaml`（已 gitignore） |
| 使用范围 | **仅用于归因推理**：提出假设、解释信号含义、组织结论表达、决定下一步取什么证据 |
| 明确排除 | **不参与任何数值计算**。所有数字均由确定性代码产生并通过交叉校验闸门；模型输出的数字一律不被采信 |
| 是否提交权重 | 否。本项目不打包、不修改、不再分发 DeepSeek 的模型权重 |
| 服务条款 | 使用者须自行遵守 DeepSeek 的服务条款与数据政策 |

调用边界见 `finagent/agent/loop.py`、`finagent/agent/llm.py` 与
`finagent/agent/prompts/system_agent.md`：模型输出与「事实」在数据结构上分区呈现
（事实 / 推论 / 观点），每条推论标注置信度与验证路径。

**接口地址在代码中固定**（`finagent/config.py` 的 `ALLOWED_HOST`，界面不提供修改入口）。
允许使用者改地址等于允许把财报正文发往任意服务器，这条不放开。
这也是运行期**唯一**允许的对外网络请求，`tests/test_network.py` 用审计钩子实测验证。

---

## 三、数据来源

| 来源 | 内容 | 性质 |
|---|---|---|
| 使用者自行提供的财报 PDF | A 股上市公司依法公开披露的定期报告（年报 / 半年报 / 一季报 / 三季报） | **唯一数据入口**。由使用者在界面上传，运行期不联网补充任何数据 |

说明：

- 本作品**不随仓库分发任何第三方数据**。财报 PDF 由使用者自行从上市公司信息披露平台下载，
  推荐材料清单（公告标题、下载链接、公告编号与 SHA256）见 `docs/复现清单.md`。
- 运行期不访问任何外部数据库、行情接口或数据服务；财务数字一律从上传的 PDF 原文中抽取，
  每个数值可回溯到具体页码。
- 本作品**未购买也未使用**任何商业金融数据库（Wind、Choice、聚宽等）的授权数据。

---

## 四、开发期使用的工具（不属于作品组成部分）

开发过程中使用了下列工具，**评审复现不需要它们**，运行期也不依赖：

| 工具 | 用途 | 说明 |
|---|---|---|
| Codex（OpenAI） | 代码编写与重构辅助 | 开发辅助工具，运行期不依赖 |
| Playwright / Microsoft Edge 无头模式 | 界面流程验证与版式截图核对 | 仅开发期使用，交付代码不调用 |
| openai/skills 的 `skill-installer` | 安装上述版式设计规范 | 开发期工具链，不进入交付物 |

---

## 五、明确未使用的第三方内容

为避免混淆，特此说明本项目**未**使用：

- 任何预训练的金融领域专用模型权重
- 任何商业金融数据库（Wind、Choice、聚宽等）的授权数据
- 任何第三方智能体框架（LangChain、AutoGen、LlamaIndex 等）——编排循环为自主实现
- 任何第三方财务分析指标库——全部指标公式在 `finagent/metrics/` 中自主实现并在报告中公开口径
- 任何 Web 框架或前端库（Flask、Streamlit、React、Vue 等）——界面只用标准库与原生 JavaScript
- 任何 CDN 资源（字体、图标库、图表库等）——界面离线可用，不发起第三方请求
- 任何第三方 PDF 解析或 OCR 库（pypdf 之外的）——扫描件不做识别，直接拒绝并提示
- 任何内网穿透或公网部署组件（cloudflared / ngrok / frp 等）——本作品只在
  `127.0.0.1` 上本地运行，服务不暴露到局域网或公网

---

*依赖版本以 `requirements.txt` 为准；实际安装版本可用 `python tests/check_env.py` 核对。*
