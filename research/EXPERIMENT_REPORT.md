# 小红书爆文研究 — 实验报告（模板）与参考文档去幻觉说明

> **用途**：把「研究」从叙事变成**可审计的实验**：每一步有可操作定义、可复现命令、可检查的产出。  
> **对《小红书爆文模型的科学化构建与验证研究报告》的态度**：仅作**选题与变量清单的启发**；其中**凡未在你方原始数据上复现的数字，一律不得写入结论**。

---

## 一、参考报告中建议视为「不可直接引用」的内容（幻觉 / 不可验证风险）

以下类型在常见「AI 辅助写作」的研究稿中出现概率高，**在独立复现前应默认剔除**：

| 类型 | 示例（若出现在参考稿中） | 为何风险高 |
|------|--------------------------|------------|
| 精确绩效数字 | 「准确率 82.3%、R²=0.81、F1=82.0%」 | 无公开数据与代码链时无法核对；脚注已提示可能含生成内容 |
| 精细回归表 | 多位小数系数、p 值全系显著 | 真实数据常有缺失、共线、弱显著；全显著需警惕 |
| 具体样本量结局 | 「294 篇、剔除 6 篇」 | 若无原始表与清洗日志，无法验证 |
| 商业数据源细节 | 千瓜字段、曝光、CTR 全覆盖 | 多数本地环境**拿不到**；写进模型等于虚构输入 |
| 强因果表述 | 「瓶颈是完播而非封面」 | 需实验设计（如干预或工具变量）支撑；横截面相关不能当因果 |

**可保留的**：变量**类别**（选题/内容/账号/时间）、**建模顺序**（先简单后复杂）、**验证思路**（划分训练测试、交叉验证、分赛道）、**对幸存者偏差的警惕**。

---

## 二、本仓库下的「真科学」现实条件（可操作陈述）

1. **可观测**：经 `export_to_xhs_feed` 进入 `samples.json` 的字段（当前归一结构见 `schema_notes.md`）；以及你方爬虫导出的**原始 JSON/JSONL**（可扩展列）。  
2. **不可观测（默认缺失）**：平台曝光、真实 CTR、算法推荐强度、用户停留时长；第三方商业数据库以**是否采购**为准。  
3. **标签**：不存在「客观爆文」唯一真值；仅可定义**操作化标签**（如：`like_proxy ≥ T` 为高分组），必须在实验里**显式写出 T 与适用范围**（全站 / 单批次爬取 / 单赛道）。  
4. **公式**：「更贴近现实」的公式应指 **在你方数据上估计出的参数**（系数、分位点、校准曲线），而不是引用外部文档中的乘法指数。

---

## 三、实验报告正文模板（填写后即为一次正式实验记录）

### 3.1 标题与作者

- 实验编号（如 EXP-2026-04-001）  
- 日期、执行环境（OS、Python 版本、是否 Docker）、数据快照标识（文件哈希或日期）

### 3.2 摘要（最后写）

- **研究问题**（一句话）  
- **数据**：来源、N、时间范围、赛道/关键词。**方法**：特征版本（如 `features_v0`）、模型（如逻辑回归）  
- **主要量化结果**：仅写**本实验**算出的指标（AUC、Brier、校准误差等），无则写「未完成」

### 3.3 引言与可证伪假设

- 背景（1段，避免抄参考稿结论）  
- **假设 H1**（可证伪）：例 — 「在仅含文本长度与 log1p(赞) 的特征下，线性模型优于常数基线」  
- **零假设 H0**：模型无提升

### 3.4 材料与方法

#### 3.4.1 数据与伦理

- 采集工具、是否遵守平台 ToS、是否脱敏  
- **标签定义**：公式 + 阈值 + 是否排除广告号（如何判）  
- **数据范围（必填，避免对外过度外推）**：  
  - 特征行数 N；**`samples.json` 路径与 sha256**（可与 `research/runtime/features_export_provenance.json` 一致）  
  - 若使用 digest：**`batch_id`**、**`samples.digest.json` 内 `sha256`**（与 CSV 中 `feed_digest_sha256` 一致）  
  - **批次多样性**：表中 `batch_id` / `feed_digest_sha256` 是否**仅单一取值**——若是，结论须限定为**该次合并快照**，不写「全站 / 任意批次」泛化  
  - **`feed_quality_metrics.json` 中 `warnings`**：若有 `single_*_snapshot` 等，须在讨论中承认局限

#### 3.4.2 变量（操作化定义）

- 逐列对照 `schema_notes.md`；新增列须登记版本号 `feature_schema_v0` → `v1`

#### 3.4.3 流程

```text
原始 json(l) →（可选）export_to_xhs_feed → samples.json
→ scripts/export_features_v0.py → research/features_v0.csv
→（可选）research/train_baseline_v0.py → research/artifacts/coef_*.json
```

**推荐固定命令链（与契约 `research/labels_spec.json` 一致；仓库根目录、Windows）**：

```text
python scripts\export_features_v0.py --samples openclaw\data\xhs-feed\samples.json --out research\features_v0.csv --labels-spec research\labels_spec.json --feed-digest openclaw\data\xhs-feed\samples.digest.json --verify-samples-digest
python research\train_baseline_v0.py --features research\features_v0.csv --out research\artifacts\baseline_v0.json --labels-spec research\labels_spec.json --cv-folds 5
```

- **路径**：上列为仓库内常见落点；若你方 `samples` / digest /输出路径不同，只替换对应参数即可，**不必**与正文字符串逐字一致。  
- **交叉验证**：`--cv-folds 5` 会写入产出 JSON 的 `cross_validation`（含 `roc_auc_mean` / `roc_auc_std`、`brier_mean` / `brier_std` 等）。若正例或负例过少，分层 K 折可能无法满折：`n_folds_effective` 会小于请求值，且 `n_folds_capped_by_minority_class` 为 `true`（与 `minority_class_count` 一并可审计）。不设 `--cv-folds` 时行为与旧版一致（仅 hold-out + `holdout_brier_score`）。  
- **折数**：`5` 是正式记录时的**默认推荐**；探索阶段可改小（如 `3`）或改大，但须在报告「材料与方法」中写明所用 `K`。

（若尚未生成 `samples.digest.json`，可先省略 `--feed-digest` 与 `--verify-samples-digest` 两行参数；正式实验建议与合并步骤一并产出 digest。）

**口径（避免误解）**：`--verify-samples-digest` 仅保证 **samples 文件字节级**与 digest 记录一致，**不**替代「`y_rule` 与契约阈值」的校验（请仍跑 `verify_features_labels_spec.py`）。可审计性还来自产物中的 `input_features_sha256`、`features_provenance` 与嵌入的 `labels_spec`。若中途修改契约或重导特征，须重新训练并更新报告。

#### 3.4.4 统计

- 训练/测试划分比例、随机种子、交叉验证折数（与命令行 `--cv-folds` 及 artifact 中 `cross_validation` 一致）  
- **基线**：多数类、常数预测、仅主效应线性模型  
- **报告**：点估计 + 置信区间或 CV 方差（勿只报单点准确率）

### 3.5 结果

- 描述性统计表（均值、分位数、缺失率）  
- 模型对比表（仅本实验产出）  
- **不得**出现未运行的外部报告数字

### 3.6 讨论与局限

- 抽样偏差、标签噪声、不可观测混淆（算法流量）  
- 与参考稿「理想变量」的差距及后续数据需求

### 3.7 可复现清单

- 命令行历史（或 `research/run_log.txt`）  
- 输入文件哈希、输出文件路径、依赖 `requirements-research.txt` 版本  
- 训练前可运行：`python scripts/verify_features_labels_spec.py --features research/features_v0.csv --labels-spec research/labels_spec.json`（确认 `y_rule` 与契约阈值一致）

---

## 四、与代码产物的对应关系

| 产物 | 说明 |
|------|------|
| `research/schema_notes.md` | 特征与标签的操作化定义 |
| `research/features_v0.csv` | 由 `scripts/export_features_v0.py` 生成 |
| `research/artifacts/` | 训练输出的系数/指标（git可忽略，见 `.gitignore` 建议） |
| `openclaw/xhs_factory.py` | 在线：`linear_clamp_v1` 启发式；可选环境变量 **`XHS_FACTORY_BASELINE_JSON`** 挂载 `train_baseline_v0.py` 产出，与线性分 **blend/replace**（见 `.env.example`） |
| `kb/README.md` | 渐进式 Wiki（解释与索引）；**不**替代本表中的数据与实验真值 |

---

## 五、下一步（实现闭环）

1. 跑一次 `export_features_v0.py`，检查描述性统计是否合理。  
2. 安装 `research/requirements-research.txt` 后跑 `train_baseline_v0.py`（建议带 `--cv-folds 5`），把输出的 JSON 附在实验报告「结果」节，并摘录 `holdout_*` 与 `cross_validation`。  
3. 系数稳定后，可将对应 JSON 配入 **`XHS_FACTORY_BASELINE_JSON`**（容器内需挂载可读）；**时间外验证**仍须在报告中单独论证，勿与单次 hold-out 混淆。  
4. 若有心得要沉淀：在 **`kb/`** 里按需加页（见 `kb/README.md`），与实验编号互链即可，**不必**与数据闭环同步「一步到位」。

---

## 六、已执行批次 · EXP-2026-04-13-BATCH01（合并 → digest → 特征 → v0/v1 训练）

**目的**：走通「带 digest / 校验的 Feed → 特征 → 基线训练」的可审计链路；本笔输入为仓库内既有 `samples.json` 再合并（`--dedupe key`），**不等价于** MediaCrawler 全量原始 jsonl——换更大、更异质的 `--in` 后指标需重跑对照。

### 6.1 Feed 与 digest

| 项 | 值 |
|----|-----|
| 实验编号 | `EXP-2026-04-13-BATCH01` |
| 输出 Feed | `openclaw/data/xhs-feed/samples.json` |
| Digest | `openclaw/data/xhs-feed/samples.digest.json` |
| `sha256`（Feed 内容） | `65d97eb793165fcc9755b935579f5e4a414b70c59421329b971d1ab2aff1c3a1` |
| `merge_stats` | `raw_rows=40`，`dedup_drop=18`，`out=22`，`empty_drop=0` |

### 6.2 复现命令（Windows PowerShell，逐条执行）

```text
cd D:\ai封装

python scripts\export_to_xhs_feed.py --in openclaw\data\xhs-feed\samples.json --out openclaw\data\xhs-feed\samples.json --dedupe key --digest-out openclaw\data\xhs-feed\samples.digest.json --batch-id EXP-2026-04-13-BATCH01 --validate-mode report

python scripts\export_features_v0.py --feed-digest openclaw\data\xhs-feed\samples.digest.json --verify-samples-digest --out research\features_v0.csv

python scripts\verify_features_labels_spec.py --features research\features_v0.csv --labels-spec research\labels_spec.json

python research\train_baseline_v0.py --features research\features_v0.csv --out research\artifacts\exp_batch_baseline_v0.json --feature-schema v0 --cv-folds 5

python research\train_baseline_v0.py --features research\features_v0.csv --out research\artifacts\exp_batch_baseline_v1.json --feature-schema v1 --cv-folds 5
```

### 6.3 特征与训练产物指纹

| 项 | 值 |
|----|-----|
| 特征表 | `research/features_v0.csv`（22 行，与 Feed 去重后条数一致） |
| `input_features_sha256` | `31c9359c6a8fa577cba8f5fa3f0c0f8b5a35d774ece94404339a4cc583be13ac` |
| v0 产物 | `research/artifacts/exp_batch_baseline_v0.json`（`generated_at_utc`:2026-04-16T06:27:55Z） |
| v1 产物 | `research/artifacts/exp_batch_baseline_v1.json`（`generated_at_utc`: 2026-04-16T06:27:57Z） |

### 6.4 指标摘录（同一随机种子 42，分层 hold-out `test_size=0.3`）

| 模式 | `n_samples` | hold-out ROC-AUC | hold-out Brier | 5-fold CV ROC-AUC (mean±std) | 5-fold Brier (mean±std) |
|------|-------------|------------------|----------------|------------------------------|---------------------------|
| v0 | 22 | 1.0 | 0.00689 | 1.0 ± 0.0 | 0.00738 ± 0.00570 |
| v1 | 22 | 1.0 | 0.00689 | 1.0 ± 0.0 | 0.00738 ± 0.00570（与 v0 数值级一致） |

**解读约束**：少数类约 5、总样本 22 时，hold-out 与 CV 出现 **ROC-AUC = 1.0** 常见于小样本与可分假象；**不得**据此推断「换大数据仍成立」——需以更大 `N`、时间外或独立来源复验。

*本文档随实验迭代增删；参考研究报告中的叙事不得替代本节中的实测表格。*

---

## 6.5 AUC「跑真」专题（2026-04-17）：标签泄漏定位与修复对照

### 问题陈述
长期观察到 v0/v1 的 hold-out ROC-AUC 接近 1.0，伴随系统自带 warnings `holdout_auc_very_high_check_overfit`，与 null-permutation 基线的 AUC≈0.5 形成不合理的 0.5+ 鸿沟，怀疑标签泄漏。

### 根因
- **`y_rule = 1 if like_proxy >= viral_like_threshold else 0`**（操作化标签）
- **`feature_schema_v0`**：特征含 `log1p_like = log1p(like_proxy)` ←  与标签同源
- **`feature_schema_v1`**：在 v0 基础上额外加入 `log1p_comment / log1p_collect / log1p_share` ← 互动指标族系仍同源相关
- 模型实质学到「`like_proxy` 是否过阈值」，AUC 必然趋近 1.0

结论：v0/v1 的高 AUC 不是「过拟合」，而是**结构性标签泄漏**。

### 修复
新增 **`feature_schema_v2`**（纯文本特征，杜绝任何互动指标衍生）：
- 文本结构：`title_len, body_len, title_emoji_count, title_punct_count, title_has_number, title_has_question, title_char_diversity, title_hashtag_count`
- 正文结构：`body_paragraph_count, body_emoji_count, body_has_cta, body_char_diversity`
- 已枚举标签的 one-hot：`sop_{tutorial,review,story,list}, emo_{positive,negative,mixed}`

同时新增 `train_baseline_v0.py` 选项：
- `--feature-schema v2`
- `--split time`：按 `published_at` 升序前 70% 训练 / 后 30% 测试（**真实泛化**评估）
- `--null-perm-runs N`：N 次标签随机置换的对照实验，写入 `null_permutation` 字段并给出 verdict

### 对照实测（2026-04-17，n=741，`viral_like_threshold=1000`）

| feature_schema | split | hold-out ROC-AUC | Brier | null perm AUC (mean±std, n=20) | real − null | verdict |
|---|---|---|---|---|---|---|
| **v0**（含 `log1p_like`） | random 70/30 | **1.0000** | 0.0097 | 0.5078 ± 0.3459 | **+0.4922** | PASS_signal（**实为泄漏伪信号**） |
| **v1**（含互动指标族系） | time 70/30 | **1.0000** | 0.0080 | 0.4933 ± 0.3421 | **+0.5067** | PASS_signal（**实为泄漏伪信号**） |
| **v2**（纯文本） | random 70/30 | 0.5761 | 0.1380 | 0.4789 ± 0.0680 | +0.0972 | WARN_no_signal |
| **v2**（纯文本） | **time 70/30** | **0.5320** | 0.1836 | 0.5006 ± 0.0831 | +0.0314 | **WARN_no_signal**（真实结论） |

### 真实结论
1. **v2 时间外 AUC = 0.5320，与随机基线无显著差距**——「仅靠文本结构特征（标题长度 / emoji 数 / CTA / SOP 类型 / emotion）几乎无法预测高赞」是当前数据下的**真信号**。
2. v0/v1 之前报告的 AUC≈1.0 全部为标签泄漏导致的伪结果，**不可作为「公式有效」的证据**。
3. 后续如要重新追求 AUC > 0.65 的真实信号，需引入**与 like_proxy 不同源**的特征：
   - 内容质量人工标注（hook 强度、信息密度、情绪强度——蓝图中的 L0 标签）
   - 账号特征（粉丝量、历史均赞、历史命中率）
   - 时间/赛道特征（发布时段、关键词热度、节日窗口）
4. **生产侧 OpenClaw `xhs_factory._baseline_lr_logistic_p` 已在加载 v0/v1 产物做评分，等同于「在线上输出泄漏模型」**——本周需要把 `XHS_FACTORY_BASELINE_JSON` 切换到 `baseline_v2_time.json`，或先关闭 baseline 模式（`XHS_FACTORY_PREDICT_USE_LLM=1`）直到 v2 信号增强。

### 复现命令
```powershell
# 重新导出含 v2 列的特征
python scripts\export_features_v0.py --samples openclaw\data\xhs-feed\samples.json --out research\features_v2.csv --labels-spec research\labels_spec.json

# 对照：旧 v0（泄漏证据）
python research\train_baseline_v0.py --features research\features_v2.csv --feature-schema v0 --out research\artifacts\baseline_v0_LEAK.json --null-perm-runs 20

# 修复：v2 + 时间外切分（真实结论）
python research\train_baseline_v0.py --features research\features_v2.csv --feature-schema v2 --split time --out research\artifacts\baseline_v2_time.json --null-perm-runs 20
```

---

## 七、持续数分自动记录（evaluate_baseline_weights）

> 以下条目由 `scripts/continuous-xhs-analytics.ps1` 在 **满足 digest 代数间隔**（默认每 10 次新 digest）并完成 v0/v1 权重评估后自动追加。解释约束见各 `research/artifacts/eval_*.json` 内 `interpretation_constraints`。全量词表与历史快照见 `research/analytics_history/`下 `keyword_candidates.json` / `manifest.json`；若需人工收窄关键词可改 `-TopKeywords` 或自行编辑 CLI 行。

### AUTO-EVAL 2026-04-16 09:15 UTC
- **feed digest sha256**: `7e2f332506d8db67…`（全长 64 hex）
- **eval**: `research/artifacts/eval_auto_baseline_v0.json`
- **n_samples**: 481; **artifact holdout ROC-AUC**: 1.0000
- **warnings**: `holdout_auc_very_high_check_overfit`
- **std coef (top by |coef|)**: log1p_like=5.590, title_len=-0.145, body_len=0.044
- **null perm AUC** (single run, n_holdout=145): 0.4956
- **eval**: `research/artifacts/eval_auto_baseline_v1.json`
- **n_samples**: 481; **artifact holdout ROC-AUC**: 0.9990
- **warnings**: `holdout_auc_very_high_check_overfit`
- **std coef (top by |coef|)**: log1p_like=5.219, log1p_collect=1.067, log1p_share=0.217, title_len=-0.161, log1p_comment=0.131, age_days=0.085, body_len=-0.013
- **null perm AUC** (single run, n_holdout=145): 0.5346


### AUTO-EVAL 2026-04-16 11:31 UTC
- **feed digest sha256**: `00a918314d6c6d88…`（全长 64 hex）
- **eval**: `research/artifacts/eval_auto_baseline_v0.json`
- **n_samples**: 723; **artifact holdout ROC-AUC**: 1.0000
- **warnings**: `holdout_auc_very_high_check_overfit`
- **std coef (top by |coef|)**: log1p_like=6.210, title_len=-0.162, body_len=0.076
- **null perm AUC** (single run, n_holdout=217): 0.3699
- **eval**: `research/artifacts/eval_auto_baseline_v1.json`
- **n_samples**: 723; **artifact holdout ROC-AUC**: 1.0000
- **warnings**: `holdout_auc_very_high_check_overfit`
- **std coef (top by |coef|)**: log1p_like=5.734, log1p_collect=1.212, log1p_share=0.348, title_len=-0.181, log1p_comment=0.083, age_days=0.076, body_len=0.008
- **null perm AUC** (single run, n_holdout=217): 0.4662


### AUTO-EVAL 2026-04-16 11:34 UTC
- **feed digest sha256**: `00a918314d6c6d88…`（全长 64 hex）
- **eval**: `research/artifacts/eval_auto_baseline_v0.json`
- **n_samples**: 723; **artifact holdout ROC-AUC**: 1.0000
- **warnings**: `holdout_auc_very_high_check_overfit`
- **std coef (top by |coef|)**: log1p_like=6.210, title_len=-0.162, body_len=0.076
- **null perm AUC** (single run, n_holdout=217): 0.3699
- **eval**: `research/artifacts/eval_auto_baseline_v1.json`
- **n_samples**: 723; **artifact holdout ROC-AUC**: 1.0000
- **warnings**: `holdout_auc_very_high_check_overfit`
- **std coef (top by |coef|)**: log1p_like=5.734, log1p_collect=1.212, log1p_share=0.348, title_len=-0.181, log1p_comment=0.083, age_days=0.076, body_len=0.008
- **null perm AUC** (single run, n_holdout=217): 0.4662


### AUTO-EVAL 2026-04-16 11:37 UTC
- **feed digest sha256**: `00a918314d6c6d88…`（全长 64 hex）
- **eval**: `research/artifacts/eval_auto_baseline_v0.json`
- **n_samples**: 723; **artifact holdout ROC-AUC**: 1.0000
- **warnings**: `holdout_auc_very_high_check_overfit`
- **std coef (top by |coef|)**: log1p_like=6.210, title_len=-0.162, body_len=0.076
- **null perm AUC** (single run, n_holdout=217): 0.3699
- **eval**: `research/artifacts/eval_auto_baseline_v1.json`
- **n_samples**: 723; **artifact holdout ROC-AUC**: 1.0000
- **warnings**: `holdout_auc_very_high_check_overfit`
- **std coef (top by |coef|)**: log1p_like=5.734, log1p_collect=1.212, log1p_share=0.348, title_len=-0.181, log1p_comment=0.083, age_days=0.076, body_len=0.008
- **null perm AUC** (single run, n_holdout=217): 0.4662


### AUTO-EVAL 2026-04-17 07:20 UTC
- **feed digest sha256**: `00a918314d6c6d88…`（全长 64 hex）
- **eval**: `research/artifacts/eval_auto_baseline_v0.json`
- **n_samples**: 723; **artifact holdout ROC-AUC**: 1.0000
- **warnings**: `holdout_auc_very_high_check_overfit`
- **std coef (top by |coef|)**: log1p_like=6.210, title_len=-0.162, body_len=0.076
- **null perm AUC** (single run, n_holdout=217): 0.3699
- **eval**: `research/artifacts/eval_auto_baseline_v1.json`
- **n_samples**: 723; **artifact holdout ROC-AUC**: 1.0000
- **warnings**: `holdout_auc_very_high_check_overfit`
- **std coef (top by |coef|)**: log1p_like=5.734, log1p_collect=1.212, log1p_share=0.348, title_len=-0.181, log1p_comment=0.083, age_days=0.076, body_len=0.008
- **null perm AUC** (single run, n_holdout=217): 0.4662


### AUTO-EVAL 2026-04-17 07:24 UTC
- **feed digest sha256**: `00a918314d6c6d88…`（全长 64 hex）
- **eval**: `research/artifacts/eval_auto_baseline_v0.json`
- **n_samples**: 723; **artifact holdout ROC-AUC**: 1.0000
- **warnings**: `holdout_auc_very_high_check_overfit`
- **std coef (top by |coef|)**: log1p_like=6.210, title_len=-0.162, body_len=0.076
- **null perm AUC** (single run, n_holdout=217): 0.3699
- **eval**: `research/artifacts/eval_auto_baseline_v1.json`
- **n_samples**: 723; **artifact holdout ROC-AUC**: 1.0000
- **warnings**: `holdout_auc_very_high_check_overfit`
- **std coef (top by |coef|)**: log1p_like=5.734, log1p_collect=1.212, log1p_share=0.348, title_len=-0.181, log1p_comment=0.083, age_days=0.076, body_len=0.008
- **null perm AUC** (single run, n_holdout=217): 0.4662


<!-- AUTO_EVAL_TAIL -->
