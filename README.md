# 一级市场私募基金管理人竞争情报周报自动化

自动生成并发送「一级市场私募基金管理人竞争情报周报」。当前 GitHub Actions 部署将在北京时间 2026-07-21 08:00 生成报告，并在 09:00 自动发送；RSS/RSSHub 采集统一由 AI_web 完成，本脚本直接读取 AI_web 私募数据包，豆包补充检索当前已关闭。

## 数据来源

当前 `config.json` 共配置 **619 家**唯一机构；GitHub Actions 会在每次运行时动态读取并校验该数量，不使用手写固定值。德同资本固定排在第一位。

| 档位 | 标准 | 数量 |
|------|------|------|
| 核心机构 | 德同资本（固定优先） | 1 家 |
| 活跃机构 | 当前配置其余机构 | 618 家 |

## 情报维度框架

报告聚焦 4 类业务与交易维度：

| 优先级 | 维度 | 说明 |
|--------|------|------|
| ⭐⭐⭐ | 基金募集动态 | 募资进展、基金架构、资金动向 |
| ⭐⭐⭐ | 投资组合与交易动态 | 新增投资、项目退出、投资节奏 |
| ⭐⭐ | 已投项目投后管理 | 经营指标、治理参与、风险事件 |
| ⭐ | 战略动向与合作关系 | 战略合作、区域布局、新赛道 |

## 数据流

- **AI_web 数据包**：唯一 RSS/RSSHub 采集端，提供已清洗、去重的私募 JSON 文件；automation 不再自行跑 RSS。
- **豆包 Web Search（已停用）**：相关实现暂时保留，但默认流程不再调用。
- **来源反馈**：新域名保存到 `source_candidates.json` 并可提交 AI_web；直接 RSS 会记录 feed，无 RSS 的来源进入 RSSHub 路由评估队列。
- **Agnes 2.0 Flash**：负责非搜索读取、PDF 摘要与趋势归纳，结果按内容哈希缓存

纯董监高变动、股东会/董事会例行召开、换届与述职不再收录；仅在公告同时包含真实业务、融资或并购事项时保留。

## 文件

| 文件 | 说明 |
|------|------|
| `pe_vc_weekly_report.py` | 主脚本：采集 → 去重 → 分类 → 生成邮件 JSON/HTML → SMTP 发送 |
| `config.json` | 目标公司名单（当前 619 家）、数据包地址、收件人配置 |
| `.env.example` | SMTP、豆包搜索与 Agnes 读取环境变量模板 |
| `pe_vc_weekly_last_report.json` | 最近一次生成的完整邮件数据 |
| `pe_vc_weekly_last_report.html` | 最近一次生成的 HTML 邮件正文预览 |

## 首次配置

复制 `.env.example` 为 `.env`，填入发件邮箱 SMTP 信息；生产密钥建议放在工作区根目录的忽略文件 `.search.env` 和模块的 `.agnes.env`：

```bash
cp .env.example .env
```

163 邮箱通常需要开启 SMTP 服务，并使用"授权码"而不是登录密码。

豆包 2.1 Turbo 仅负责联网搜索：

```bash
ARK_SEARCH_API_KEY=your-ark-key
ARK_SEARCH_MODE=responses
ARK_RESPONSES_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
ARK_SEARCH_MODEL=doubao-seed-2-1-turbo-260628
```

Agnes 负责正文/PDF 读取、摘要与趋势判断：

```bash
AGNES_API_KEY=your-agnes-key
AGNES_BASE_URL=https://apihub.agnes-ai.com/v1
AGNES_MODEL=agnes-2.0-flash
```

## 手动测试

生成 JSON 和 HTML，不发送邮件：

```bash
python3 pe_vc_weekly_report.py --dry-run
```

只用豆包补充某一家机构，并把通过日期、公司和链接校验的结果合并到现有报告：

```bash
DOUBAO_SEARCH_TIMEOUT=90 python3 pe_vc_weekly_report.py \
  --dry-run \
  --supplement-existing-company 德同资本
```

该模式不会重新检索完整名单；运行时会显示 4 个阶段的即时进度。`--force-doubao-company` 则用于全量任务中强制补搜指定机构，两者含义不同。

本地冒烟测试（仅检索前 2 家公司）：

```bash
AI_SEARCH_COMPANY_LIMIT=2 TARGETED_RSS_COMPANY_LIMIT=2 python3 pe_vc_weekly_report.py --dry-run
```

真实发送：

```bash
python3 pe_vc_weekly_report.py --send
```

发送最近一次已生成的报告，不重新采集：

```bash
python3 pe_vc_weekly_report.py --send-existing
```

## GitHub Actions 部署

已在 `.github/workflows/pe-vc-weekly.yml` 配置：北京时间 2026-07-21 08:00 自动触发采集和报告生成，并在 09:00 发送。工作流保留手动触发入口；一次性日期门控会阻止同一 cron 在以后年份重复执行。

需要在 GitHub 仓库 `Settings → Secrets and variables → Actions` 添加以下 Secrets：

| Secret | 说明 |
|--------|------|
| `SMTP_USER` | 163 发件邮箱地址 |
| `SMTP_PASSWORD` | 163 SMTP 授权码 |
| `SMTP_FROM` | 发件邮箱地址，通常与 `SMTP_USER` 一致 |
| `ARK_SEARCH_API_KEY` | 豆包 / Ark Responses API Key |
| `AGNES_API_KEY` | Agnes 非搜索分析与 PDF 读取 Key |

可选变量：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ARK_RESPONSES_BASE_URL` | `https://ark.cn-beijing.volces.com/api/v3` | 豆包 Responses 搜索端点 |
| `ARK_SEARCH_MODEL` | `doubao-seed-2-1-turbo-260628` | 豆包 Web Search 模型 |
| `AGNES_BASE_URL` | `https://apihub.agnes-ai.com/v1` | Agnes OpenAI 兼容端点 |
| `AGNES_MODEL` | `agnes-2.0-flash` | 非搜索读取模型 |
| `DOUBAO_PE_COMPANY_LIMIT` | `100` | 豆包检索的核心机构上限 |
| `DOUBAO_SEARCH_BATCH_SIZE` | `20` | 每次搜索合并的公司数 |
| `DOUBAO_SEARCH_WORKERS` | `2` | 搜索并发上限 |
| `DOUBAO_SEARCH_STREAM` | `1` | 使用 SSE 流式响应 |
| `DOUBAO_SEARCH_TIMEOUT` | `300` | 流中断等待秒数 |
| `DOUBAO_SEARCH_MAX_RETRIES` | `1` | 避免付费请求重复执行 |
| `SEARCH_DAYS` | `30` | 检索窗口天数 |
| `RSSHUB_BASE_URL` | `http://127.0.0.1:1200` | 本地 RSSHub 实例 |
| `EMAIL_CHUNK_SIZE` | `25` | 邮件拆分条目数 |
| `AI_SEARCH_COMPANY_LIMIT` | `0` | 本地冒烟测试用（0=全量） |
| `TARGETED_RSS_COMPANY_LIMIT` | `0` | 本地冒烟测试用（0=全量） |

推荐从工作区根目录运行 `run_intelligence_system.py`，由统一完成门控负责与传媒报告一起同步到 `ai_web` 并发信。脚本还支持可重复的 `--crawler-input` JSON/JSONL 投递路径。
