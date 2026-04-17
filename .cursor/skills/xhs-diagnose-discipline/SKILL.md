---
name: xhs-diagnose-discipline
description: Enforces the non-negotiable engineering and statistical discipline for the XHS L3 note-diagnosis tool (openclaw/xhs_diagnose*, api/main.py /api/diagnose/note, web/diagnose.js, scripts/diagnose_note.py). Use this skill WHENEVER touching any of those files, writing diagnosis rules, rendering report examples, adjusting evidence strings, shipping a new suggestion, or communicating the tool's value to users or seed testers. This project has been burned by label leakage, hardcoded examples, and overclaimed "traffic boosts" — reloading these rules on every change keeps the tool honest.
---

# XHS Diagnose Discipline

Hard-won rules for the small-red-book (xiaohongshu) note diagnosis stack.
If you're editing the diagnosis engine, its renderer, its API endpoint, its
web tab, or any seed-user-facing copy, read this file first and treat every
rule below as a gating check.

## Scope (files under discipline)

- `openclaw/xhs_diagnose.py`            — rule-triggering engine
- `openclaw/xhs_diagnose_renderer.py`   — Markdown renderer
- `api/main.py` — only the `/api/diagnose/note` endpoint + `_append_diagnose_log`
- `web/diagnose.js` + the "笔记诊断" card in `web/index.html`
- `scripts/diagnose_note.py`            — CLI wrapper
- `docs/seed_outreach_templates.md`, `docs/seed_phase_sop.md`

---

## Rule 1 — No hardcoded examples in suggestions. Ever.

**Why**: shipping fallback strings like `减脂餐真的能瘦吗？ → 减脂餐吃一个月，我瘦了 15 斤` makes every user's report look identical. The user caught this live: "给了两条一摸一样的建议，真是服了".

**Do**:
- Engine populates `Suggestion.user_state` with the user's current numbers
  (e.g. `question_count`, `hashtags_in_title`, `human: "你当前标题里有 4 个问号"`).
- Engine populates `DiagnoseResult.original_input` with the raw title and a
  truncated body.
- Renderer calls `_transform_title(action_code, title, user_state)` to
  **algorithmically** produce `(before, after)` from the user's own text.
- If `_transform_title` returns `None`, **omit the example section entirely**.
  Do not ship a generic template as fallback.

**Don't**:
- Re-introduce `_ACTION_TEMPLATES` with `fallback_before` / `fallback_after`.
- Hardcode emojis: use `_pick_emoji_for(title)` against `_EMOJI_POOL`
  (hash-stable per title, not random).

---

## Rule 2 — Statistical honesty in every `evidence` string

**Why**: pure-text features give AUC ≈ 0.53 on a time-held-out split. That is
real-but-weak signal. Overclaiming destroys the tool's seed-user trust in week 1.

**Allowed phrasings**:
- `"高赞样本中带问号的占比比不带问号低 54%（95% CI [-60%, -36%], n=727）"`
- `"剔除该特征后模型 AUC 由基线上升 0.044（说明该特征对预测反向有害）"`
- `"每增加 1 个 emoji，高赞占比上升 7.8 个百分点（95% CI [+2, +15]）"`

**Banned phrasings**:
- `"预计流量提升 xx%"` — we have no causal data, only correlations.
- `"爆款概率提升 N 倍"` — logistic-regression odds ratios are not traffic.
- Anything comparing Δprobability without reporting CI and n.

**Two-layer convention**: the primary number in `evidence` is a descriptive
statistic (rate diff, percentile shift). The logistic-regression coefficient
and its CI go into the `coefficient` dict on the `Suggestion` as a scholarly
footnote — never as the headline.

---

## Rule 3 — Only three stable rules deserve `high` severity

| action_code | Trigger | Severity |
|---|---|---|
| `REMOVE_TITLE_QUESTION_MARK` | `title_has_question == 1` | high |
| `ADD_ONE_TITLE_EMOJI` | `title_emoji_count == 0` (**strictly zero**) | high |
| `REDUCE_TITLE_HASHTAG_TO_ONE` | `title_hashtag_count >= 2` (reverse-evidence) | high |

Everything else (body length, paragraph count, etc.) goes into
`DiagnoseResult.info_notes` with no action — because the effect size is small
and/or CI straddles zero. Do not promote new rules to `high` without:

1. Null-permutation control experiment showing effect is non-spurious.
2. Bootstrap CI that excludes zero **after** time-based train/test split.
3. Effect size ≥ the weakest of the three above.

The emoji rule must fire **only when count is 0**. Going from 1→2 does not
produce a statistically meaningful lift; do not suggest "add more emojis" for
titles that already have one.

---

## Rule 4 — Engine returns facts; renderer owns wording

Strict separation so presentation bugs cannot corrupt statistics:

- `xhs_diagnose.py` outputs only: `action_code`, `severity`, `evidence` text,
  `user_state` dict, `coefficient` dict, `caveats`. **No** `example_before`,
  `example_after`, `emoji_suggestion`.
- `xhs_diagnose_renderer.py` owns: `_ACTION_DESC`, `_EMOJI_POOL`,
  `_transform_title`, severity emojis, Markdown structure.

If you need a new piece of copy, add it to `_ACTION_DESC`. If you need a new
algorithmic transform, extend `_transform_title`. Never put strings in the
engine that are only used for display.

---

## Rule 5 — MVP scope red lines

The MVP must not:

1. **Parse or fetch xiaohongshu links server-side.** Users paste title+body
   manually. This is a compliance line; do not cross it without explicit legal
   sign-off from the user.
2. Publish notes on behalf of seed users. The diagnosis tool is read-only
   toward the platform.
3. Promise traffic / GMV / follower growth in any user-facing copy.
4. Emit PDF/Streamlit/extra frontends in the MVP loop. Everything ships through
   the existing FastAPI + `web/index.html` + `web/diagnose.js` trio.
5. Silently log raw titles or bodies. The opt-in
   `FLOW_API_DIAGNOSE_LOG=1` logger stores **lengths, feature counts, and
   action codes only** — never the text itself.

---

## Rule 6 — Seed-user communication

- Offer the tool free to the first 10 users. Do not set a price before
  collecting "would you pay?" feedback from at least 5 of them.
- Send the four-question feedback prompt in `docs/seed_phase_sop.md` after
  each user has run ≥ 3 diagnoses.
- Do not cite AUC to seed users; cite rate differences ("带问号的历史高赞率
  低 X 个百分点"). Seed users are bloggers, not statisticians.
- When a user asks "这工具能给我带来多少播放", the correct answer is:
  "无法承诺播放增幅；工具只基于 700+ 篇健身赛道历史笔记的描述性差异给
  写作建议。你拿建议改完上线后，流量变化请你自己记录反馈给我。"

---

## Rule 7 — Mandatory change-time checks

After any edit inside the scope files above, run, in order:

1. `python scripts/diagnose_note.py --title "…问号？" --body "…"` against
   **at least three dissimilar titles** and eyeball that `_transform_title`
   produces **different** before/after strings for each input.
2. If renderer changed: confirm the generated Markdown does not contain the
   substring `fallback_before` or `fallback_after` — these belong to the
   deleted template system and must not reappear.
3. If `api/main.py` changed: run a `POST /api/diagnose/note` with
   `FLOW_API_DIAGNOSE_LOG=1` and confirm the endpoint still returns 200 even
   when the log path is forced to fail (the log block is try/except).
4. If `web/diagnose.js` changed: open `http://127.0.0.1:8099/`, paste a title
   containing `<` or `` ` ``, and verify the rendered `<code>` block shows
   those characters literally rather than as HTML entities (`&lt;` / `&amp;`).

---

## Rule 8 — Things that are cheap to get wrong twice

A short list of mistakes that have already happened once in this codebase.
If you catch yourself about to do any of these, stop and re-read this file.

- ❌ Writing `"expected_boost: +49% 概率"` in an action string.
  ✅ Write the rate difference with CI instead.
- ❌ Replacing every question mark with `！`.
  ✅ Trailing `?` → delete; internal `?` → replace with `。` (Chinese period).
- ❌ Using `datetime.utcnow()` in new code.
  ✅ `datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")`.
- ❌ Calling `escapeHtml` inside the Markdown code-span replacer when the
  input has already been escaped upstream — causes double-escape display bugs.
- ❌ Ranking body-length as a suggestion. Its effect is roughly
  +0.2 percentage points per +100 characters; do not suggest "write longer".

---

## When to skip this skill

- Pure documentation-only edits that do not touch any scope file.
- Editing unrelated modules (`openclaw/xhs_factory*`, `research/`, `hermes/`).
- Infra changes that only affect the auto-sync cron.

Otherwise, re-read this file. The cost is a few seconds; the cost of
re-introducing hardcoded examples or over-claiming stats is a seed-user
churn event.
