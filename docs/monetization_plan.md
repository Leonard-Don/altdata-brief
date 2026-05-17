# Monetization Plan · 90-Day Roadmap

## Phase 1 (Day 0-30) · Free 日报 → 建立读者基线

- 每个交易日 09:00 UTC+8 自动生成简报
- 推送渠道：GitHub Pages + RSS + 个人 Substack (free tier)
- 目标 KPI：每周日报阅读人次 ≥ 200，订阅 ≥ 30
- 反馈循环：每 5 期回看一次 PostHog / Substack analytics，调整段落长度与图表

## Phase 2 (Day 31-60) · 引入 LLM 改写 + 周度纵深

- v0.2 上线：用 LLM 把「本日观察」段从 rule-based 转 narrative，但保留 rule-based 作 ground truth
- 启动 **Weekly Deep-Dive** ¥39/月：每周一篇行业纵深 + 敏感性分析 + chart pack
- 目标 KPI：付费转化率 ≥ 3%（30 订阅 → ≥1 付费），写够 4 篇 Weekly

## Phase 3 (Day 61-90) · 季度报告 + 渠道分发

- v1.0：上线 Quarterly Report ¥199/季，第一份是「HS300 指数纳入效应实证」(脱胎自 `index-inclusion-research`)
- 接入：知乎专栏同步发布 + 个人公众号 + 雪球
- 目标 KPI：3 位 Quarterly 付费用户；至少 1 单 alt-data dashboard 外包成单

## 价值锚

| 档位 | 卖什么 | 谁会买 |
|---|---|---|
| Free Daily | 复现/速读的 alt-data 视角 | 量化爱好者、行研实习生、PM 助理 |
| Paid Weekly | 行业纵深 + 可下载 chart pack | 中小私募研究员、独立交易员 |
| Paid Quarterly | 一手实证 + 因果识别 | 学术圈、对冲基金博士、监管部门 |

## 风险与缓解

- **数据合规**：所有数据均来自公开 API + 自建 alt-data，简报每段附 source label，无未授权第三方数据。
- **可持续性**：v0.1 完全 deterministic、零外部依赖（除 matplotlib + Jinja2），单人运行成本接近 0。
- **冷启动**：靠 6 个项目的 README 互链 + Boss/Upwork 简介挂展示链接，先到 200 读者再谈付费。
