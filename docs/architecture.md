# Architecture · v0.4

## 总览

```
┌────────────────────────────────────────────────────────┐
│        4 个上游量化项目（各自的 git 仓库）             │
│  ┌────────────────┐   ┌────────────────┐               │
│  │ cache/...json  │   │data/public/    │               │
│  │ (内部缓存)     │   │ public artifact│  ←─── v0.4 4/4 覆盖 │
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

1. **public-summary preferred**：默认顺序是 `live → public artifact → cache`。v0.4 起 4/4 adapters 都支持 public artifact（ETF 512400 使用已提交的 `src/data/liveSnapshot.json` 作为 public-by-default artifact）。`--source-mode {auto,public,cache,live}` 可显式切换。
2. **graceful degradation**：任一适配器抛 `AdapterUnavailable`，对应段落显示「数据缺失」而非整体崩溃。
3. **deterministic synthesis**：v0.4 仍不引入 LLM。所有「观察」句都来自 `synthesis/observation.py` 的候选信号排序与模板函数，全部可单测。
4. **per-section sources footer**：每段必须列出上游项目 + cache 文件名 + 时间戳，便于独立核查。
5. **pre-publish validation**：`altdata-brief validate` 在发布前检查 source 是否缺失、陈旧或结构不完整；`public_summary_freshness` 覆盖 4 个 public artifacts，并在输出末尾打印每个 adapter 的实际解析路径和 mtime。

## v0.4 接入的 4 个源

| Adapter | Public artifact | Cache/live fallback | 关键字段 |
|---|---|---|
| `SuperPricingAdapter` | `~/PycharmProjects/super-pricing-system/data/public/alt_data_summary.json` | `cache/alt_data/providers/{policy_radar,macro_hf}.json` | `policy_radar.industry_signals`, `macro_hf.metals` |
| `QuantTradingAdapter` | `~/PycharmProjects/quant-trading-system/data/public/quant_summary.json` | `cache/alt_data/providers/policy_radar.json` + industry cache | `policy_radar.top_industries`, `industry_heat.top_industries_by_score`, `etf_rotation`, `paper_trading` profile names |
| `IndexResearchAdapter` | `~/index-inclusion-research/data/public/index_research_summary.json` | `results/real_tables/cma_hypothesis_verdicts.csv` + `pap_deviation_report.csv` | `verdicts`, `pap_changes` |
| `ETF512400Adapter` | `~/ETF 512400/src/data/liveSnapshot.json` | same public-by-default file | `quote`, `nav`, `meta.sourceHealth` |

## 部署形态

- **本地**：`scripts/generate_daily.sh` + macOS `launchd` 或 `cron`。默认 `auto` 可读 live/public/cache；`scripts/smoke_e2e.sh` 会把 4 个上游 public artifacts 复制到临时 scratch dir 后跑 `validate + generate`。
- **GitHub Actions** (v0.4)：`.github/workflows/daily.yml` checkout 4 个上游仓库，只读 public artifacts，然后执行 `validate --source-mode public` 与 `generate --source-mode public`。缺 public artifact 是 hard fail；陈旧 public artifact 是 WARN，避免单个 stale snapshot 阻断结构健康的简报发布。
