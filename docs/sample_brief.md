# CN AltData Brief — 2026-05-17

> 由 `cn-altdata-brief` 在 2026-05-17T01:48:52Z 自动生成，合成自 4 个公开摘要/快照数据源。
> 本简报是公开 alt-data pipeline 的展示窗口，详见 [项目说明](../README.md)。

---

## 1. 政策动向

![政策动向 chart](sample_charts/policy_impact.png)

- **新能源汽车**：avg_impact=-0.388 (负向) · mentions=94 · 信号=利空
- **电网**：avg_impact=+0.100 (正向) · mentions=8 · 信号=中性
- **风电**：avg_impact=+0.000 (中性) · mentions=1 · 信号=中性

> 本批次 policy_radar 累计 20 条政策记录，confidence=0.633。

**Sources:** super-pricing-system::policy_radar.json · cache ts=`2026-05-05T11:00:55.113895`
---
## 2. 库存信号

![库存信号 chart](sample_charts/inventory_change.png)

- **铜**：周价格变化 -0.86% · 波动率 28.9 · 趋势=stable · 标签=去库 · conf=0.09
- **铝**：周价格变化 -1.15% · 波动率 28.3 · 趋势=stable · 标签=去库 · conf=0.11

> 全球港口拥堵指数 50.0 (正常)·tracked=10

**Sources:** super-pricing-system::macro_hf.json · cache ts=`2026-05-05T11:02:15.485791`
---
## 3. ETF 资金流

![ETF NAV chart](sample_charts/etf_nav.png)

- **有色金属ETF南方** (512400) · 现价 2.207 · 涨跌 +0.36% · 成交额 16.59 亿 · 换手 5.70%
- NAV (2026-05-06) · 单位净值 2.1985 · 日收益 +3.85%
- 数据源 4/4 required OK · fallback=0 · 评级=良好
- 商品驱动子源 5/5 OK
- 邻近行业热度：新能源汽车 (0.73) · 电网 (0.03)

**Sources:** ETF-512400::liveSnapshot.json · quant-trading-system::policy_radar.json

---
## 4. 行业温度

![行业温度 chart](sample_charts/industry_heat.png)

- **新能源汽车**：heat=0.728 · 政策叠加 signal=bearish (impact=-0.320) · mentions=119
- **电网**：heat=0.030 · 政策叠加 signal=neutral (impact=+0.000) · mentions=6
- **风电**：heat=0.015 · 政策叠加 signal=neutral (impact=+0.000) · mentions=3

**Sources:** quant-trading-system::policy_radar.json

---
## 5. 本日观察

> 今日核心信号是 ETF 512400 日内 NAV 上涨 3.85%，数据源评级 **良好**。
> 对比近 7 日波动均值 ≈0.60%，今日波幅显著走强；商品驱动子源 5/5 OK。
> 若该信号延续 3 日，可重点观察有色金属现货成交，收盘后复核折溢价、申赎与数据源一致性。

**Sources:** super-pricing-system::policy_radar.json · ETF-512400::liveSnapshot.json · quant-trading-system::policy_radar.json · index-inclusion-research::cma_hypothesis_verdicts.csv

---
---

## 元信息

- 生成时间：`2026-05-17T01:48:52Z`
- 数据日期：`2026-05-17`
- 上游项目：super-pricing-system · quant-trading-system · index-inclusion-research · ETF 512400
- 合成方式：rule-based deterministic synthesis（v0.2 暂不接入 LLM）

> Disclaimer：本简报不构成投资建议。所有信号来自公开数据源 + 自建 alt-data pipeline，仅供研究与教学讨论。
