# Architecture · v0.3

## 总览

```
┌────────────────────────────────────────────────────────┐
│        4 个上游量化项目（各自的 git 仓库）             │
│  ┌────────────────┐   ┌────────────────┐               │
│  │ cache/...json  │   │data/public/    │               │
│  │ (内部缓存)     │   │ *_summary.json │  ←─── v0.3 优先 │
│  └────────────────┘   └────────────────┘               │
└────────────┬────────────────────────────┬──────────────┘
             │ filesystem (本机)          │ git checkout (CI 可读)
             ▼                            ▼
   ┌──────────────────────────────────────────┐
   │  adapters/                               │ 每个项目一个适配器
   │  resolution: live → public → cache       │ (可被 --source-mode 覆盖)
   │  base.py 抽象 IO                         │
   └────────┬─────────────────────────────────┘
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
   output/feed.xml
```

## 设计原则

1. **public-summary preferred**：v0.3 起，默认顺序是 `live → data/public/*_summary.json → cache`。public summary 是上游项目主动提交到 git 的「脱敏摘要」，是 GitHub Actions 唯一能读到的路径。`--source-mode {auto,public,cache,live}` 可显式切换。
2. **graceful degradation**：任一适配器抛 `AdapterUnavailable`，对应段落显示「数据缺失」而非整体崩溃。
3. **deterministic synthesis**：v0.3 仍不引入 LLM。所有「观察」句都来自 `synthesis/observation.py` 的候选信号排序与模板函数，全部可单测。
4. **per-section sources footer**：每段必须列出上游项目 + cache 文件名 + 时间戳，便于独立核查。
5. **pre-publish validation**：`cn-altdata-brief validate` 在发布前检查 cache 是否缺失、陈旧或结构不完整。新增 `public_summary_freshness` 检查 24 小时内是否有 public summary 产出。

## v0.1 接入的 4 个源

| Adapter | 默认路径 | 关键字段 |
|---|---|---|
| `SuperPricingAdapter` | `~/PycharmProjects/super-pricing-system/cache/alt_data/providers/{policy_radar,macro_hf}.json` | `signal.industry_signals`, `records[].raw_value` |
| `QuantTradingAdapter` | `~/PycharmProjects/quant-trading-system/cache/alt_data/providers/policy_radar.json` | 从 `industry_signals` 派生 heat 排行（fallback） |
| `IndexResearchAdapter` | `~/index-inclusion-research/results/real_tables/cma_hypothesis_verdicts.csv` + `pap_deviation_report.csv` | `verdict`, `pap_changes` |
| `ETF512400Adapter` | `~/ETF 512400/src/data/liveSnapshot.json` | `quote`, `nav`, `meta.sourceHealth` |

## 部署形态

- **本地**：`scripts/generate_daily.sh` + macOS `launchd` 或 `cron`。读 cache 或 public summary 均可。
- **GitHub Actions** (v0.3)：当上游项目提交了 `data/public/<source>_summary.json` 后，`.github/workflows/daily.yml` 可以 `actions/checkout` 几个上游仓库（仅 public 目录），然后 `uv run cn-altdata-brief generate --source-mode public` 跑通整个流水线。目前 super-pricing 已提交 alt_data_summary.json，index-inclusion-research 的 summary 还在补全。
