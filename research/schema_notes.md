# 特征与标签 — 操作化定义（feature_schema v0）

本文件与 **`scripts/export_features_v0.py`** 输出列一致。核心建模仍属 **feature_schema v0**（`train_baseline_v0` 仅用 `title_len`、`body_len`、`log1p_like`）；下列 **Feed v1 扩展列**已随归一写入 CSV，供后续 v1 实验与描述统计。

## 数据来源

- 默认输入：`openclaw/data/xhs-feed/samples.json`（JSON 数组），或由 `--samples` 指定路径。  
- 每条记录经 `_normalize_external_sample` 对齐后的字段为主；若你从原始爬虫 JSON 导出，请先走 `scripts/export_to_xhs_feed.py` 或保证字段可映射。

## 标签契约 `labels_spec`（阶段 0：与实验报告对齐）

- **模板（可提交）**：`research/labels_spec.example.json`  
- **本地副本（勿提交）**：复制为 `research/labels_spec.json`（已在 `.gitignore`），按批次改 `viral_like_threshold` 与 `notes`。  
- **导出特征**：`scripts/export_features_v0.py --labels-spec research/labels_spec.json` 会读取 `viral_like_threshold`（或兼容键 `viral_threshold`）；命令行 `--viral-threshold` 若同时传入则**覆盖** JSON。  
- **次阈值（可选）**：`labels_spec.example.json` 中可设 `viral_like_threshold_alt`与可选 `label_name_alt`；导出 CSV 增加列 **`y_rule_alt`**（无次阈值时该列存在但可为空）。校验脚本仅在 spec 含次阈值时要求 `y_rule_alt` 与 `like_proxy` 一致。  
- **训练基线**：`research/train_baseline_v0.py --labels-spec ...` 将同一 JSON 对象原样写入 `research/artifacts/*.json` 的 `labels_spec` 字段，并记录 `input_features_path` 与 `input_features_sha256`（JSON 文件建议 UTF-8；带 BOM 亦可读）。**`--target-column y_rule|y_rule_alt`** 选择训练标签；产物含 **`target_column`**字段。

## 列说明

| 列名 | 类型 | 定义 | 备注 |
|------|------|------|------|
| `row_index` | int | 行号（0-based） | 追溯用 |
| `title_len` | int | `len(title_hint.strip())` | |
| `body_len` | int | `len(body_hint.strip())` | |
| `like_proxy` | int | 归一后的点赞代理，≥1 | 合并时 `openclaw/feed_like_parse.py` 解析 `liked_count`（含 **`万` / `w`** 简写）；仍无法解析或缺字段时用默认正整数（见 `kb/评估与晋升基线.md`1.1） |
| `log1p_like` | float | `log(1 + like_proxy)` | 减弱极值影响 |
| `sop_tag` | str | 结构标签 | 缺省映射见工厂；**非**客观真理 |
| `emotion_tag` | str | 情绪标签 | 同上 |
| `published_at` | str | Feed 归一后的 UTC 时间（ISO8601，**Z** 后缀） | 上游无时间则为空字符串 |
| `comment_proxy` | str | 评论数代理（CSV 存十进制字符串，空表示缺失） | 与 `export_to_xhs_feed` / `_normalize_external_sample` 一致 |
| `collect_proxy` | str | 收藏数代理 | 同上 |
| `share_proxy` | str | 分享数代理 | 同上 |
| `y_rule` | int | 见下 | 由 `viral_like_threshold`（或 CLI覆盖）决定 |
| `y_rule_alt` | int | 同 `y_rule` 规则但使用 `viral_like_threshold_alt` | 仅当 labels_spec 含次阈值时非空；与主标签对照做稳健性分析 |
| `batch_id` | str | 批次标识 | 可选；`--batch-id` → `EXPORT_FEATURES_BATCH_ID` → `--feed-digest` 内 `batch_id`；无则空字符串 |
| `feed_digest_sha256` | str | 合并侧车 digest 中的 **sha256** | 可选；仅当传入 `--feed-digest` 且文件为 `xhs_feed_digest_v1`；否则空字符串；**`train_baseline_v0` 不使用此列** |

**稳定与安全（推荐正式跑实验时）**

- **`export_features_v0.py --verify-samples-digest`**（须同时 **`--feed-digest`**）：对 `--samples` 文件计算 sha256，**必须与** digest 内 `sha256` 一致，否则脚本失败，避免「digest 与样本文件错配」。默认可写入 **`research/runtime/features_export_provenance.json`**（`--no-provenance` 可关）。若 digest 含 `output_path` 与当前 `--samples` 解析路径不一致，会打印**警告**（仍可能内容相同；最终以 `--verify-samples-digest` 的哈希为准）。  
- **`compute_feed_metrics_v0.py`**：产出 **`warnings`**（如单 `batch_id`/单 digest快照、`like_proxy` 大量为 100、时间列可解析率低），供对外结论自检。  
- **`train_baseline_v0.py`**：若表中出现**多个**非空 `batch_id` 或多个非空 `feed_digest_sha256`，**默认拒绝训练**；显式 **`--allow-mixed-batch`** 方可继续，并在产物中写入 **`features_provenance`**（含 `unique_*` 与 conflict 标记）。

## 标签 `y_rule` / `y_rule_alt`（操作化，非平台真值）

- **定义**（主阈值）：`y_rule = 1 if like_proxy >= T else 0`（`T` 来自 labels_spec 的 `viral_like_threshold` 或 CLI `--viral-threshold`）。  
- **次阈值**：若 spec 含 `viral_like_threshold_alt`，则 `y_rule_alt = 1 if like_proxy >= T_alt else 0`。  
- **局限**：点赞受发布时间、账号粉丝、推荐流量影响；**不是**「内容质量」的纯净度量；跨批次比较需谨慎。  
- **建议**：正式实验至少报告 **按批次分位** 的相对标签敏感性分析；可用 **`y_rule_alt`** 与 **`--target-column`** 做对照训练。

## 评估与时间外粗检

- **`research/evaluate_baseline_weights.py`**：默认从 artifact 读取 **`target_column`** 构建设计矩阵；**`--time-holdout-fraction F`**（`F` 需严格介于 0 与 1 之间）在可解析 **`published_at`** 的行上按时间升序划分：较早部分训练、最后 `F` 比例测试，结果写入 JSON 的 **`time_ordered_holdout`**（与 artifact 内随机分层 hold-out 互补；非分层，小测试集 AUC 波动大）。  
- **`scripts/compute_feed_metrics_v0.py`**：从特征 CSV 生成 **`research/runtime/feed_quality_metrics.json`**（行数、时间列覆盖率、`like_proxy` 摘要、`y_rule`/`y_rule_alt` 阳性率、batch/digest 基数）；持续数分脚本在 verify 后默认调用，可用 **`-SkipFeedMetrics`** 关闭。

## 本版本刻意未包含（避免假装已测）

- 曝光、CTR、搜索量、千瓜指数  
- 真实完播、漏斗转化  
- BERT 向量（可在 v1 增加独立实验）

---

## feature_schema v1（训练与 artifact版本）

**已实现**：Feed 归一层与 CSV 已含 **`published_at`、`comment_proxy`、`collect_proxy`、`share_proxy`**（见上表）；别名与解析规则见 **`kb/Feed归一层扩展字段设计.md`**（与代码同步维护）。

**训练 v1（已实现）**：

```text
python research/train_baseline_v0.py --features research/features_v0.csv --out research/artifacts/baseline_v1.json --labels-spec research/labels_spec.json --feature-schema v1 --cv-folds 5
```

- 产物 **`schema`: `feature_schema_v1`**，`feature_names` 为 `title_len, body_len, log1p_like, log1p_comment, log1p_collect, log1p_share, age_days`（`age_days` 以批内最大 `published_at` 为参考，缺失行用中位数填充）。  
- **`xhs_factory`** 可加载 v1 artifact；线上仅文本时 v1 互动/稿龄来自 **环境变量或** `/process` **params**（见 `.env.example`）。

**仍可扩展**：时间衰减、比率、文本相似度等额外列；独立实验编号与报告记录。
