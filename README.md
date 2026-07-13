# 一级市场私募基金管理人竞争情报周报自动化

每周一自动生成并发送「一级市场私募基金管理人竞争情报周报」的本地工作流，覆盖**投中网各榜单并集 421 家** PE/VC 管理人。

## 数据来源

目标公司名单来自投中网历年榜单并集（2022-2025 年 VC/PE/国资 TOP100、数字化与信息化/AI 与大数据/新消费/企业服务/互联网产业等子榜单），共 **421 家** 管理人，按榜单出现频次分 3 档：

| 档位 | 标准 | 数量 |
|------|------|------|
| 核心机构 | ≥5 个榜单 | 88 家 |
| 活跃机构 | 2-4 个榜单 | 235 家 |
| 观察名单 | 1 个榜单 | 98 家 |

## 情报维度框架

按 7 大维度 · 3 级优先级归类：

| 优先级 | 维度 | 说明 |
|--------|------|------|
| ⭐⭐⭐ | 基金募集动态 | 募资进展、基金架构、资金动向 |
| ⭐⭐⭐ | 投资组合与交易动态 | 新增投资、项目退出、投资节奏 |
| ⭐⭐ | 已投项目投后管理 | 经营指标、治理参与、风险事件 |
| ⭐⭐ | 组织与团队建设 | 核心人事、团队扩张、组织架构 |
| ⭐ | 品牌与行业影响力 | 排名奖项、公开活动、媒体输出 |
| ⭐ | 战略动向与合作关系 | 战略合作、区域布局、新赛道 |
| ⭐ | 合规与监管动态 | 监管检查、备案情况、合规事件 |

## 采集源

- **RSSHub 财经媒体**：投资界、36氪、财联社、证券时报、新浪财经、21 财经、人民网、新华网
- **定向搜索（可选）**：按公司名调用 RSSHub EastMoney；统一模式为保证截止时间默认关闭
- **豆包 2.1 Turbo Web Search**：TOP100 机构默认按 20 家合并搜索，SSE 流式返回；豆包不读取正文
- **Agnes 2.0 Flash**：负责非搜索读取、PDF 摘要与趋势归纳，结果按内容哈希缓存
- **中基协公示系统**（降级备用）：公开接口查询备案/变更/处分记录

## 文件

| 文件 | 说明 |
|------|------|
| `pe_vc_weekly_report.py` | 主脚本：采集 → 去重 → 分类 → 生成邮件 JSON/HTML → SMTP 发送 |
| `config.json` | 目标公司名单（421 家）、RSS 源、收件人配置 |
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

只生成邮件 JSON，不发送：

```bash
python3 pe_vc_weekly_report.py --dry-run
```

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

已在 `.github/workflows/pe-vc-weekly.yml` 配置，默认每周一北京时间 08:00 触发采集和报告生成，并在 09:00 发送。

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
