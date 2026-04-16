# Feed 归一层扩展字段设计（v1 字段 · 归一已实现）

> **状态**：**已实现** — `published_at`、`comment_proxy`、`collect_proxy`、`share_proxy` 已写入 **`export_to_xhs_feed._normalize_external_sample`** 与 **`openclaw/xhs_factory._normalize_external_sample`**（须保持同步）；**`export_features_v0.py`** 已增加对应 CSV 列。**尚未实现**：`train_baseline_v0` 用新列训练、`schema: feature_schema_v1` artifact。  
> **关联**：[schema_notes](../research/schema_notes.md)、[项目技术架构说明](../项目技术架构说明.md)。

---

## 1. 设计目标

| 目标 | 说明 |
|------|------|
| **可解释** | 时间与互动为「爬虫可见代理」，非平台真值；命名带 `_proxy` 或与 `like_proxy` 并列说明。 |
| **可空** | 老数据、缺字段导出：归一后允许 **`null`或省略键**（由实现二选一并在契约中写死），**禁止**用魔法数假装「有数据」。 |
| **可审计** | 批次仍靠 `digest` / `batch_id`；新字段变更需在实验报告记明爬虫版本与字段来源。 |

---

## 2. 归一后建议字段（canonical）

与现有 **`title_hint`、`body_hint`、`like_proxy`、`sop_tag`、`emotion_tag`** 并列（均为单条笔记对象上的键）：

| Canonical 键 | 类型（建议） | 语义 |
|--------------|--------------|------|
| **`published_at`** | `string` \| `null` |笔记发布时间 **ISO 8601**，建议 **UTC** 后缀 `Z` 或显式 `+08:00`；无法解析则为 `null` 或省略。 |
| **`comment_proxy`** | `integer` \| `null` | 评论数代理（≥0）；缺失则为 `null` 或省略。 |
| **`collect_proxy`** | `integer` \| `null` | 收藏 / 收藏量代理。 |
| **`share_proxy`** | `integer` \| `null` | 分享次数代理（若爬虫无则省略）。 |

**暂不纳入归一（避免过拟合未验证字段）**：完播率、曝光、CTR、搜索量、千瓜类指数。

**比率类特征**（如评论/点赞）：放在 **feature_schema v1** 的 **`export_features_v0` 计算列**，不在 Feed 里写死除零规则；Feed 只提供原始或代理整数。

---

## 3. 上游别名表（MediaCrawler / 常见导出）

> 实现时：在 `_normalize_external_sample` 内按**顺序**取第一个非空可解析值，与现有 title/like 别名模式一致。

### 3.1 时间 `published_at`

| 语义 | 可接受键名（示例） |
|------|---------------------|
| ISO 或近 ISO 字符串 | `published_at`、`publish_time`、`create_time`、`time`、`note_publish_time`、`last_update_time` |
| Unix 秒 / 毫秒 | `timestamp`、`create_timestamp`、`publish_timestamp`（实现时须判断数量级区分 s/ms） |

**解析失败**：不写 canonical 或写 `null`；**禁止**静默填「当前时间」。

### 3.2 评论数 → `comment_proxy`

| 可接受键名（示例） |
|---------------------|
| `comment_count`、`comments_count`、`comment_cnt`、`note_comment_count`、`sub_comment_count`（若仅为总数需与业务确认） |

### 3.3 收藏 → `collect_proxy`

| 可接受键名（示例） |
|---------------------|
| `collected_count`、`collection_count`、`favorite_count`、`bookmark_count`、`collect_count` |

### 3.4 分享 → `share_proxy`

| 可接受键名（示例） |
|---------------------|
| `share_count`、`shared_count`、`forward_count` |

---

## 4. JSON Schema 与校验（后续）

- 当前 **`scripts/schemas/xhs_feed_item_v1.schema.json`** 仅覆盖 v0 五元组。  
-归一实现后：可新增 **`xhs_feed_item_v2.schema.json`**（或 bump `additionalProperties` 策略），并在 **`--validate-schema`** 中选用；**勿**在未 bump 版本时改旧 Schema 必填项，以免 CI 误杀历史 Feed。

---

## 5. 验证命令（不写盘）

合并后仅看统计与校验，**不覆盖** `samples.json`（**已实现** `--dry-run`）：

```text
python scripts\export_to_xhs_feed.py --in <爬虫目录或文件> --dry-run --validate-mode report
```

无需传 `--out`。可与 `--dedupe`、`--validate-mode fail` 等联用；`fail` 且存在违规时 **exit 2** 且同样不写盘。

---

*本页仅作渐进演进的设计锚点；以仓库内实际归一代码为准。*
