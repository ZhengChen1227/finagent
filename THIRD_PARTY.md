# 第三方依赖与数据来源说明

本文件依据竞赛要求编制：使用第三方开源项目、模型、数据或代码的，
应列明名称、版本、来源、许可证及具体使用范围，不得将第三方成果冒充为自主开发成果。

**自主开发范围声明**：本项目的智能体编排框架、工具集、指标计算层、勾稽校验层、
异常规则引擎、报告生成与日志记录模块均为自主开发。第三方内容仅限下表所列，
且均用于通用基础能力（数值计算、HTTP 通信、PDF 解析、协议实现），
不涉及本项目的金融分析逻辑。

---

## 一、运行依赖（Python 包）

| 名称 | 版本 | 来源 | 许可证 | 使用范围 |
|---|---|---|---|---|
| pandas | 2.3.3 | PyPI | BSD-3-Clause | 财务数据表结构、指标宽表、报告期序列对齐 |
| numpy | 2.4.1 | PyPI | BSD-3-Clause | 数值计算与类型转换（作为 pandas 依赖间接使用） |
| requests | 2.32.5 | PyPI | Apache-2.0 | 巨潮资讯网、东方财富接口的数据采集 |
| PyYAML | 6.0.3 | PyPI | MIT | `config.yaml` 配置解析 |
| httpx | 0.28.1 | PyPI | BSD-3-Clause | 大语言模型 OpenAI 兼容接口调用 |
| pypdf | 6.11.0 | PyPI | BSD-3-Clause | 公告 PDF 文本抽取（构建本地全文索引） |
| mcp | 1.27.1 | PyPI | MIT | Model Context Protocol 服务端的协议实现 |

### 图形界面

图形界面（`app/`）**不引入任何第三方依赖**：服务端基于 Python 标准库 `http.server`
实现，前端为原生 HTML / CSS / JavaScript。界面只负责参数转发与结果渲染，
不参与任何计算，也不改变核心流程的产物与轨迹。

界面的**版式设计参考**了下列开源设计规范（仅参考其设计准则，未复制任何代码）：

| 名称 | 版本 | 来源 | 许可证 | 使用范围 |
|---|---|---|---|---|
| bexa-dashboard | 1.2.0 | https://github.com/BekhruzTursunboev/Bexa-professional-frontend-design-skills-for-ai-agents（`skills/bexa-dashboard`） | MIT | 数据密集型工作台的版式准则：侧边导航 + 主区骨架、面板高度跟随视口、高密度界面用分隔线而非成排卡片承载 KPI、数值统一等宽并开启 `tabular-nums`、正负向数值使用非荧光色。 |

该规范为提示词形态的设计指引，不包含可执行代码，因此没有产生任何代码级依赖；
`app/static/style.css` 中的全部样式均为自主编写。

## 二、大语言模型（可选，未内置权重）

本项目**不包含任何模型权重**，通过 OpenAI 兼容 HTTP 接口调用外部模型服务。
未配置模型时系统自动降级为确定性离线模式，全部计算、校验与报告生成能力不受影响。

| 项目 | 说明 |
|---|---|
| 调用方式 | HTTP POST `{base_url}/chat/completions`，OpenAI 兼容协议，支持 Function Calling |
| 配置位置 | `config.yaml` 的 `llm` 段，或环境变量 `FINAGENT_BASE_URL` / `FINAGENT_API_KEY` / `FINAGENT_MODEL` |
| 使用范围 | 仅用于**归因推理**（提出假设、判断信号含义、指出验证路径） |
| 明确排除 | 不参与任何数值计算。所有数字均由确定性代码产生并经勾稽校验 |

支持接入的国产模型（由使用方自行选择并承担相应服务条款）：

| 模型 | base_url | 示例 model |
|---|---|---|
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-plus` |
| Kimi | `https://api.moonshot.cn/v1` | `moonshot-v1-32k` |

模型输出的使用边界见 `finagent/agent/attributor.py` 与 `finagent/agent/prompts/system_agent.md`：
模型输出的每条推论均标注置信度与验证路径，且报告中与"事实"分区呈现。

## 三、数据来源

| 来源 | 内容 | 性质 |
|---|---|---|
| 巨潮资讯网（www.cninfo.com.cn） | 上市公司定期报告 PDF 原文（年报、半年报、一季报、三季报） | **权威出处**。经 `python run.py fetch` 落盘至 `data/corpus/`，构成封闭数据环境 |
| 东方财富数据中心（datacenter.eastmoney.com） | 结构化财务数据（利润表、现金流量表、资产负债表） | **第三方核验源**。用于快速取数与交叉核对，最终以公告原文为准 |

数据均为上市公司依法公开披露的信息，不涉及非公开信息。

关于数据来源的进一步说明：

- 两家网站的数据**不随作品分发**。`data/corpus/` 中的公告原文由使用方运行
  `python run.py fetch` 自行抓取，故未将第三方数据打包进作品。
- 结构化财务数据仅作为**交叉核对与快速取数**使用，报告中凡涉及结论的数值
  均标注证据 ID，可回溯到公告原文页码。
- 本作品未购买也未使用任何商业金融数据库（Wind、Choice、聚宽等）的授权数据。

## 三·补、开发期使用的工具（不进入交付物）

开发过程中使用了若干 AI 辅助工具，特此说明其**不属于作品组成部分**，
评审复现不需要它们：

| 工具 | 用途 | 说明 |
|---|---|---|
| Codex（OpenAI） | 代码编写与重构辅助 | 仅作为开发辅助工具，运行期不依赖 |
| Playwright / Microsoft Edge 无头模式 | 界面版式的截图核对 | 仅用于开发期视觉检查，不在交付代码中调用 |
| openai/skills 的 `skill-installer` | 安装上述设计规范 | 开发期工具链，不进入交付物 |
| Cloudflare Pages | 托管只读展示站 | 仅在发布静态展示站时使用，运行期不依赖 |

## 四、未使用的第三方内容

为避免混淆，特此说明本项目**未**使用：

- 任何预训练的金融领域专用模型权重
- 任何商业财务数据库（Wind、Choice、聚宽等）的授权数据
- 任何第三方智能体框架（LangChain、AutoGen、LlamaIndex 等）——编排循环为自主实现
- 任何第三方财务分析指标库——全部指标公式在 `finagent/metrics/` 中自主实现并在报告中公开口径
- 任何 Web 框架或前端库（Flask、Streamlit、React、Vue 等）——图形界面只用标准库与原生 JavaScript
- 任何内网穿透客户端（cloudflared / Cloudflare Tunnel、ngrok、frp 等）——公网展示走
  纯静态托管，不需要常驻隧道进程，也不把本地服务暴露到公网

---

*依赖版本以 `requirements.txt` 为准；实际安装版本可用 `python tests/check_env.py` 核对。*
