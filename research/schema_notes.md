# 特征与标签 — 操作化定义（feature_schema v0）

本文件与 **`scripts/export_features_v0.py`** 输出列一致。任何列语义变更须 bump 版本（v1, v2…）并在实验报告中记录。

## 数据来源

- 默认输入：`openclaw/data/xhs-feed/samples.json`（JSON 数组），或由 `--samples` 指定路径。  
- 每条记录经 `_normalize_external_sample` 对齐后的字段为主；若你从原始爬虫 JSON 导出，请先走 `scripts/export_to_xhs_feed.py` 或保证字段可映射。

## 标签契约 `labels_spec`（阶段 0：与实验报告对齐）

- **模板（可提交）**：`research/labels_spec.example.json`  
- **本地副本（勿提交）**：复制为 `research/labels_spec.json`（已在 `.gitignore`），按批次改 `viral_like_threshold` 与 `notes`。  
- **导出特征**：`scripts/export_features_v0.py --labels-spec research/labels_spec.json` 会读取 `viral_like_threshold`（或兼容键 `viral_threshold`）；命令行 `--viral-threshold` 若同时传入则**覆盖** JSON。  
- **训练基线**：`research/train_baseline_v0.py --labels-spec ...` 将同一 JSON 对象原样写入 `research/artifacts/*.json` 的 `labels_spec` 字段，并记录 `input_features_path` 与 `input_features_sha256`（JSON 文件建议 UTF-8；带 BOM 亦可读）。

## 列说明

| 列名 | 类型 | 定义 | 备注 |
|------|------|------|------|
| `row_index` | int | 行号（0-based） | 追溯用 |
| `title_len` | int | `len(title_hint.strip())` | |
| `body_len` | int | `len(body_hint.strip())` | |
| `like_proxy` | int | 归一后的点赞代理，≥1 | 无则导出脚本内为 0时已在上游处理 |
| `log1p_like` | float | `log(1 + like_proxy)` | 减弱极值影响 |
| `sop_tag` | str | 结构标签 | 缺省映射见工厂；**非**客观真理 |
| `emotion_tag` | str | 情绪标签 | 同上 |
| `y_rule` | int | 见下 | 仅当使用 `--viral-threshold` 时有效 |
| `batch_id` | str | 批次标识 | 可选；`--batch-id` → `EXPORT_FEATURES_BATCH_ID` → `--feed-digest` 内 `batch_id`；无则空字符串 |
| `feed_digest_sha256` | str | 合并侧车 digest 中的 **sha256** | 可选；仅当传入 `--feed-digest` 且文件为 `xhs_feed_digest_v1`；否则空字符串；**`train_baseline_v0` 不使用此列** |

**稳定与安全（推荐正式跑实验时）**

- **`export_features_v0.py --verify-samples-digest`**（须同时 **`--feed-digest`**）：对 `--samples` 文件计算 sha256，**必须与** digest 内 `sha256` 一致，否则脚本失败，避免「digest 与样本文件错配」。- 若 digest 含 `output_path` 与当前 `--samples` 解析路径不一致，会打印**警告**（仍可能内容相同；最终以 `--verify-samples-digest` 的哈希为准）。  
- **`train_baseline_v0.py`**：若表中出现**多个**非空 `batch_id` 或多个非空 `feed_digest_sha256`，**默认拒绝训练**；显式 **`--allow-mixed-batch`** 方可继续，并在产物中写入 **`features_provenance`**（含 `unique_*` 与 conflict 标记）。

## 标签 `y_rule`（操作化，非平台真值）

- **定义**（启用 `--viral-threshold T` 时）：`y_rule = 1 if like_proxy >= T else 0`。  
- **局限**：点赞受发布时间、账号粉丝、推荐流量影响；**不是**「内容质量」的纯净度量；跨批次比较需谨慎。  
- **建议**：正式实验至少报告 **按批次分位** 的相对标签敏感性分析（后续脚本可扩展）。

## 本版本刻意未包含（避免假装已测）

- 曝光、CTR、搜索量、千瓜指数  
- 真实完播、漏斗转化  
- BERT 向量（可在 v1 增加独立实验）
