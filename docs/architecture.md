# Architecture · v0.1

## 总览

```
┌─────────────────────────┐
│   4 个上游量化项目      │
│  (各自的 cache JSON)    │
└────────────┬────────────┘
             │ 文件读取 (cache-first)
             ▼
   ┌─────────────────────┐
   │  adapters/          │ 每个项目一个适配器
   │  base.py 抽象 IO    │
   └────────┬────────────┘
            │ AdapterPayload
            ▼
   ┌─────────────────────┐
   │  synthesis/         │ 确定性规则合成 5 个段落
   │  policy / inventory │
   │  etf_flow / industry│
   │  observation        │
   └────────┬────────────┘
            │ section context dicts
            ▼
   ┌─────────────────────┐
   │  render/            │
   │  markdown (Jinja2)  │
   │  charts (matplotlib)│
   │  site (index.md)    │
   └────────┬────────────┘
            │
            ▼
   output/briefs/YYYY-MM-DD.md
   output/charts/YYYY-MM-DD/*.png
```

## 设计原则

1. **cache-first**：默认读上游项目的 on-disk JSON / CSV，不依赖任何上游服务运行。`CN_ALTDATA_BRIEF_LIVE=1` 才尝试 HTTP。
2. **graceful degradation**：任一适配器抛 `AdapterUnavailable`，对应段落显示「数据缺失」而非整体崩溃。
3. **deterministic synthesis**：v0.1 不引入 LLM。所有「观察」句都来自 `synthesis/observation.py` 的 predicate 函数，全部可单测。
4. **per-section sources footer**：每段必须列出上游项目 + cache 文件名 + 时间戳，便于独立核查。

## v0.1 接入的 4 个源

| Adapter | 默认路径 | 关键字段 |
|---|---|---|
| `SuperPricingAdapter` | `~/PycharmProjects/super-pricing-system/cache/alt_data/providers/{policy_radar,macro_hf}.json` | `signal.industry_signals`, `records[].raw_value` |
| `QuantTradingAdapter` | `~/PycharmProjects/quant-trading-system/cache/alt_data/providers/policy_radar.json` | 从 `industry_signals` 派生 heat 排行（fallback） |
| `IndexResearchAdapter` | `~/index-inclusion-research/results/real_tables/cma_hypothesis_verdicts.csv` + `pap_deviation_report.csv` | `verdict`, `pap_changes` |
| `ETF512400Adapter` | `~/ETF 512400/src/data/liveSnapshot.json` | `quote`, `nav`, `meta.sourceHealth` |

## 部署形态

- **本地**：`scripts/generate_daily.sh` + macOS `launchd` 或 `cron`。
- **GitHub Actions** (v0.5)：`.github/workflows/daily.yml` 是模板，需要先把上游 cache 推到云端（S3 / R2 / Gist），workflow 才能落地。
