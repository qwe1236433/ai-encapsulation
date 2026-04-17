# 种子阶段运营 SOP（四周）

> 配套工具：`scripts/diagnose_note.py`、`POST /api/diagnose/note`、`web/index.html` 诊断 tab
> 配套话术：`docs/seed_outreach_templates.md`
>
> **阶段目标**：在 4 周内完成 **10 位种子用户免费试用** + 回收 **结构化反馈**，用于：
> 1. 验证"诊断建议是否真的有人会照做"
> 2. 收集定价意愿（**最多愿付多少钱**，不是"会不会付"）
> 3. 发现当前 2 条强规则的盲区和跨账号偏差
>
> **明确 NOT 目标**：
> - ❌ 不上线付费
> - ❌ 不追求 DAU / 留存
> - ❌ 不做广告投放
> - ❌ 不扩品类（这一阶段只做健身/减脂）

---

## Week 1：工具端收尾 + 种子清单

### 1.1 工具自检（你做，本机跑）

- [ ] 启动 API：`python -m uvicorn api.main:app --host 127.0.0.1 --port 8099`
- [ ] 打开 http://127.0.0.1:8099/ → 进入「笔记诊断」卡片
- [ ] 用自己的 2 篇笔记（或 `scripts/test_data/diagnose_case_*.json`）跑一遍
- [ ] 在 `research/features_v2.csv` 里随机挑 3 篇高赞笔记、3 篇低赞笔记，粘贴诊断，看 3 条规则是否触发符合预期
- [ ] 读完诊断 Markdown，确认**不会让对方误以为"必涨流量"**

### 1.2 种子清单（你做，小红书上筛）

- [ ] 小红书搜关键词：健身 / 减脂 / 减脂餐 / 马甲线 / HIIT
- [ ] 筛选条件：
  - 粉丝 1,000 – 10,000（太小没数据价值，太大不会理你）
  - 发笔记频次 ≥ 每周 1 条
  - 最近 3 条笔记里**有至少 1 条明显踩本工具规则**（带问号 / 0 emoji / ≥2 hashtag）
- [ ] 整理成 `docs/seed_candidates.md`（**不提交到 git**），每行：`@账号名｜粉丝数｜踩雷条｜观察切入点`

### 1.3 话术本地化

- [ ] 打开 `docs/seed_outreach_templates.md`
- [ ] 针对清单里前 3 个账号，把话术 A/B/C 改成具体那个账号的观察切入点
- [ ] **千万不要群发**

---

## Week 2：私信发出 + 第一轮诊断

### 2.1 每天 3 条，一共 15 条（给自己留冗余）

- [ ] 每天中午 12:00–14:00 私信 3 个（避开早/晚高峰）
- [ ] 每条都附**具体笔记观察**，不群发模板
- [ ] 记录发送时间、对方反应到 `docs/seed_outreach_log.md`（不提交 git）

### 2.2 回复节奏

- [ ] 对方响应 → 让对方粘贴标题+正文到 http://127.0.0.1:8099/ 诊断卡片（截图给他页面），或者你代操作：他粘贴到微信，你贴到页面截图回他
- [ ] 诊断报告用「复制 Markdown」→ 粘到微信/私信
- [ ] **每次回复都附**：
  - "这只是写作级提示，不保证流量"
  - "如果你觉得建议有问题，告诉我，这对我的工具迭代最有用"

### 2.3 启用日志（可选）

为后续分析"哪些规则最常触发"，可启用 opt-in 日志：

```powershell
# 在 .env 或启动前设置
$env:FLOW_API_DIAGNOSE_LOG = "1"
python -m uvicorn api.main:app --host 127.0.0.1 --port 8099
```

日志落在 `research/runtime/diagnose_log.jsonl`，**只记脱敏摘要**（长度、特征值、触发的 action_code），不记原文。

---

## Week 3：反馈回收 + 数据整理

### 3.1 每位种子用户追问 4 问

在对方拿到诊断 24–48h 后私信：

1. 建议清楚吗？哪条最有道理？哪条像废话？
2. 你打算照做哪条？不打算照做哪条？为什么？
3. 如果你照做了，一周后告诉我流量变化
4. **关键**：这工具收费多少钱一次合理？多少钱/月合理？（只要心里价，不付钱）

记录到 `docs/seed_feedback.md`（不提交 git），结构化字段：

```
| 用户ID（脱敏） | 建议清晰度(1-5) | 照做规则 | 拒做规则 | 理由 | 心里价/次 | 心里价/月 | 7天后流量变化 |
```

### 3.2 规则命中率统计（如启了日志）

```powershell
python -c "
import json
from collections import Counter
counts = Counter()
with open('research/runtime/diagnose_log.jsonl', encoding='utf-8') as f:
    for line in f:
        try:
            for code in json.loads(line).get('suggestion_codes', []):
                counts[code] += 1
        except Exception:
            pass
for k, v in counts.most_common():
    print(f'{k:34s}  {v}')
"
```

期待看到：规则 1/2 触发占比远高于规则 3（hashtag 小样本预期应该少）。

---

## Week 4：定价决策 + 下一步路线

### 4.1 定价三档决策矩阵

把 Week 3 收集的"心里价"汇总。规则：

| 10 人中心里价 **均值**在这个区间 | 决策 |
|---|---|
| < ¥10/次 | **不定价，直接开源** —— 这等于说用户没价值感，强推只会掉品牌 |
| ¥10–30/次 | **单次免费 + 高阶付费（¥9.9/月 × 3 次；未来 v3 模型上线后再涨）** |
| ¥30–60/次 | **按次 ¥19/次 + 月度 ¥49/5 次** |
| > ¥60/次 | 很可能是客套回答，**不采信**，回到 ¥10–30 档位 |

### 4.2 基于反馈的工具迭代（按优先级排）

- [ ] 如果 ≥ 3 人反馈"建议不够具体 / 还想要别的维度" → 列为 v3 模型的特征需求（账号特征、封面图特征、评论区特征等）
- [ ] 如果 ≥ 3 人反馈"照做一周后流量有明显变化" → **记录这 3 人的 before/after**，作为产品最有力的公开证据（**必须征得对方同意**再用）
- [ ] 如果 ≥ 3 人反馈"规则 X 不合理" → 复查 `research/features_v2.csv` 在对方那个赛道的分布，**不要**直接改规则，要用数据说话

### 4.3 写一份 4 周复盘

模板：`research/SEED_PHASE_REPORT_W4.md`（可提交 git）

- 种子用户数：邀约 / 响应 / 完成诊断 / 给出反馈
- 规则命中率：3 条规则各自触发多少次
- 定价决策：走哪一档、理由
- 下一步：是上付费、是重做模型、还是停止

---

## 红线 · 绝对不做的事

1. ❌ 未经同意把种子用户的笔记内容发到其他平台/群里"展示"
2. ❌ 把"预期涨粉 X%"写进任何营销文案
3. ❌ 为了凑 10 人指标，群发同样的话术
4. ❌ 在收集反馈时暗示"付费会给你更好版本"（强买强卖）
5. ❌ 种子阶段就开始投流、接广告、卖课
6. ❌ 在工具里加"链接自动抓取"功能（合规红线）

---

## 附：本阶段会用到的文件清单

| 路径 | 用途 | 进 git? |
|---|---|---|
| `scripts/diagnose_note.py` | CLI 诊断 | ✅ |
| `openclaw/xhs_diagnose.py` | 引擎 | ✅ |
| `openclaw/xhs_diagnose_renderer.py` | Markdown renderer | ✅ |
| `api/main.py`（含 /api/diagnose/note） | API | ✅ |
| `web/index.html`、`web/diagnose.js` | 前端 | ✅ |
| `docs/seed_outreach_templates.md` | 话术模板 | ✅ |
| `docs/seed_phase_sop.md` | 本文档 | ✅ |
| `research/runtime/diagnose_log.jsonl` | opt-in 诊断日志 | ❌（含真实笔记特征） |
| `docs/seed_candidates.md` | 种子清单（账号名等） | ❌ |
| `docs/seed_outreach_log.md` | 私信记录 | ❌ |
| `docs/seed_feedback.md` | 用户反馈 | ❌ |
| `research/SEED_PHASE_REPORT_W4.md` | 四周复盘 | ✅ |

`.gitignore` 建议增补：
```
/docs/seed_candidates.md
/docs/seed_outreach_log.md
/docs/seed_feedback.md
/research/runtime/diagnose_log.jsonl
```
