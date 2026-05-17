# CN AltData Brief · 中国另类数据日报

[![CI](https://github.com/Leonard-Don/cn-altdata-brief/actions/workflows/ci.yml/badge.svg)](https://github.com/Leonard-Don/cn-altdata-brief/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-3776AB)
![License](https://img.shields.io/badge/license-MIT-blue)
![Version](https://img.shields.io/badge/version-v0.1.0-1f6feb)
![Sources](https://img.shields.io/badge/sources-4%20projects-6f42c1)
![Cadence](https://img.shields.io/badge/cadence-T%2B0%20daily-2da44e)

`cn-altdata-brief` 是一个每个交易日自动生成的另类数据研究简报，
合成自我自己运行的 6 个量化项目的真实信号缓存。它是一份**可订阅、可引用、可复现**的中国 A 股 alt-data 视角。

`cn-altdata-brief` produces a daily, deterministic research brief over China-equity alt-data — synthesized from 4 of the 6 quant projects I run locally. The goal is content-as-distribution: my own tools, made visible.

---

## 1. 这是什么 / What this is

> 一句话：每个交易日 09:00 (UTC+8) 自动生成一份 5 段式 alt-data 简报，免费、版本化、有源可查。

每份简报包含：

| # | 段落 | 信号来源 | 数据形态 |
|---|---|---|---|
| 1 | **政策动向** | super-pricing-system / policy_radar | top-3 行业 avg_impact + mentions |
| 2 | **库存信号** | super-pricing-system / macro_hf | LME/SHFE 金属周价格变化 |
| 3 | **ETF 资金流** | ETF 512400 liveSnapshot | 行情 / NAV / source-health 评级 |
| 4 | **行业温度** | quant-trading-system | 行业 heat 排行 + 政策叠加 |
| 5 | **本日观察** | 全源跨切 | 2-3 句确定性归纳，无 LLM |

合成全部基于**确定性规则**——v0.1 不接入 LLM，刻意"无聊但可靠"。这样新读者能复现，老读者能引用。

## 2. 为什么有它 / Why it exists

中文金融内容里，**主观评论是过剩的、可复现的数据视角是稀缺的**。我自己跑了 6 套量化工具，每个项目都自带 cache JSON 和评级面板，但它们只服务于我一个人——这是浪费。

`cn-altdata-brief` 把这 6 套工具拼成一份每日刊：

- 对**读者**：免费拿到一份来源可查的 alt-data 速报，不必再听"专家说"
- 对**我**：把分散的项目串成一个内容产品线，是付费订阅的 v0，也是外包询盘的展示作品

## 3. 一份简报里有什么 / What's inside a daily brief

[点开示例 →](docs/sample_brief.md)

简报使用 Markdown 输出，可直接发布到 GitHub Pages / Substack / 微信公众号。每段末尾附 `**Sources:**` 标注上游项目 + cache 文件名 + 时间戳，便于核查。

样例片段（来自 `docs/sample_brief.md`）：

```markdown
## 1. 政策动向

- **新能源汽车**：avg_impact=-0.388 (负向) · mentions=94 · 信号=利空
- **电网**：avg_impact=+0.100 (正向) · mentions=8 · 信号=中性
- **风电**：avg_impact=+0.050 (正向) · mentions=1 · 信号=中性

**Sources:** super-pricing-system::policy_radar.json · cache ts=`2026-05-05T11:00:55.113895`
```

每段配 1 张 matplotlib 图（与 [index-inclusion-research forest plot](https://github.com/Leonard-Don/index-inclusion-research) 同款风格）。

## 4. 数据源 = 我的项目组合 / The data sources are my portfolio

`cn-altdata-brief` **不持有任何金融数据**——它只读取我已有 6 个项目的 cache。这等同于把整套工具栈展开给读者看：

| 项目 | 角色 | 我的另一个 GitHub repo |
|---|---|---|
| `super-pricing-system` | 政策雷达 + 宏观高频 | `Leonard-Don/super-pricing-system` |
| `quant-trading-system` | 行业热度 + 政策叠加 | `Leonard-Don/quant-trading-system` |
| `index-inclusion-research` | CMA 7 条假说裁决 + PAP guard | `Leonard-Don/index-inclusion-research` |
| `ETF 512400` | 非铁金属 ETF 实时快照 | `Leonard-Don/ETF-512400` |
| `Yieldwise` | 利率曲线 (v0.2 接入) | `Leonard-Don/yieldwise` |
| `TianXianQuant Android` | 移动端实验 (v0.1 跳过) | `Leonard-Don/TianXianQuant` |

读者从一份简报顺藤摸瓜，能看到我所有项目的当前状态——这是"内容即分发"。

## 5. 示例简报 / Sample brief

[`docs/sample_brief.md`](docs/sample_brief.md) — 项目初始化时由真实缓存生成的一份。

## 6. 商业化分层（仍在试探）/ Monetization tiers (tentative)

| 档位 | 频次 | 渠道 | 价格 |
|---|---|---|---|
| **Free Daily Brief** | 每个交易日 | RSS + GitHub Pages + Substack | ¥0 |
| **Paid Weekly Deep-Dive** | 每周 | Substack 付费墙 | ¥39 / 月 |
| **Paid Quarterly Report** | 每季 | PDF 邮件交付 | ¥199 / 季 |

- **Weekly Deep-Dive** 包含：行业纵深 + 敏感性分析 + 可下载的 chart pack
- **Quarterly Report** 含：选定行业的 HS300 RDD 风格实证分析（脱胎自 `index-inclusion-research`）

定价仅供参考，会在 v1.0 上线前再校准。

## 7. 路线图 / Roadmap

| 版本 | 关键能力 | 状态 |
|---|---|---|
| **v0.1** | 4 个 cache 源接入 + 5 段式日报 + 4 张图 + GitHub Actions 模板 | ✅ 当前 |
| **v0.2** | LLM 改写「本日观察」段，rule-based 输出作 ground truth | 计划中 |
| **v0.5** | Substack 自动发布 + 邮件订阅入口 + RSS Feed | 计划中 |
| **v1.0** | 付费墙上线 + Weekly Deep-Dive 实战 | 计划中 |

## 8. 快速开始 / Quickstart

```bash
git clone https://github.com/Leonard-Don/cn-altdata-brief.git
cd cn-altdata-brief
uv sync
uv run cn-altdata-brief generate
```

生成结果落在 `output/briefs/YYYY-MM-DD.md` 与 `output/charts/YYYY-MM-DD/*.png`。

若上游 4 个 cache 不在默认路径，可在 v0.2 之前**手动复制**到对应位置；之后会暴露 CLI flag。

更深入：[docs/architecture.md](docs/architecture.md) · [docs/monetization_plan.md](docs/monetization_plan.md)

## 9. License + Contact

MIT.

* Author: Tang Zihan ([Leonard-Don](https://github.com/Leonard-Don))
* For outsourcing / collaboration inquiries: this repo IS my portfolio — open an issue or DM on Boss/Upwork.
