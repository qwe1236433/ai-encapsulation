"""
openclaw/xhs_diagnose_renderer.py
==================================
把 DiagnoseResult 渲染为面向用户的 Markdown 报告。

职责边界：
  - 本模块承担全部"面向用户的文案" —— 标题、示例、措辞
  - 模板按 action_code 查表，引擎层永远不包含这些字符串
  - 引擎升级 / 系数变更时，本模块不用改（除非新增 action_code）

设计要求：
  - 永远不输出"综合评分 / 排名 / 百分位"
  - 永远不把 logistic 系数改写为"预期提升 x%"
  - 描述统计的真实占比差直接引用，CI 作为附注
"""

from __future__ import annotations

import re
from typing import Any

from openclaw.xhs_diagnose import DiagnoseResult, Suggestion

# ─────────────────────────────────────────────────────────────────────────────
# 动作文案模板（仅存放措辞；示例由 _transform_title 基于用户原文算法生成）
# ─────────────────────────────────────────────────────────────────────────────
_ACTION_TEMPLATES: dict[str, dict[str, str]] = {
    "REMOVE_TITLE_QUESTION_MARK": {
        "action": "删除标题中的所有问号（「？」「?」），改写成陈述句或感叹句",
        "fallback_before": "减脂餐真的能瘦吗？",
        "fallback_after":  "减脂餐吃一个月，我瘦了 15 斤！",
    },
    "ADD_ONE_TITLE_EMOJI": {
        "action": "在标题开头加入 1 个情绪 emoji（例：🔥 💪 ✨ 😍）",
        "fallback_before": "减脂餐吃一个月，我瘦了 15 斤",
        "fallback_after":  "🔥 减脂餐吃一个月，我瘦了 15 斤",
    },
    "REDUCE_TITLE_HASHTAG_TO_ONE": {
        "action": "把标题里多余的 hashtag 移到正文，只保留 1 个最相关的",
        "fallback_before": "#减肥 #减脂餐 #健身打卡 今日 160 卡减脂餐",
        "fallback_after":  "#减肥 今日 160 卡减脂餐（其它话题搬到正文）",
    },
}

_SEVERITY_EMOJI: dict[str, str] = {
    "high":   "🔴 高优先级",
    "medium": "🔵 中优先级",
    "info":   "🟡 参考信息",
}

_DEFAULT_EMOJI = "🔥"
_HASHTAG_RE = re.compile(r"#[^\s#＃]+")


def _transform_title(action_code: str, title: str, user_state: dict[str, Any]) -> tuple[str, str] | None:
    """
    基于用户原标题做**程序化变换**，返回 (before, after)。
    所有"例子"都使用用户自己的标题，而不是通用模板。
    传空标题时返回 None，由调用方决定是否回退到通用模板。
    """
    original = (title or "").strip()
    if not original:
        return None

    if action_code == "REMOVE_TITLE_QUESTION_MARK":
        # 策略：末尾的问号直接删；中间的问号改成中文句号（避免两句粘连）；
        # 不强加感叹号，在建议文案里已说明"改写成陈述句或感叹句"，让用户自选。
        stripped = re.sub(r"[?？]+\s*$", "", original)
        stripped = re.sub(r"\s*[?？]+\s*", "。", stripped)
        stripped = re.sub(r"\s+", " ", stripped).strip()
        if stripped == original or not stripped:
            return None  # 规则触发了却没找到问号（理论不该发生），交给 fallback
        return (original, stripped)

    if action_code == "ADD_ONE_TITLE_EMOJI":
        # 在开头加一个 emoji；如果开头已有空白先 trim
        after = f"{_DEFAULT_EMOJI} {original}"
        return (original, after)

    if action_code == "REDUCE_TITLE_HASHTAG_TO_ONE":
        tags = _HASHTAG_RE.findall(original)
        if len(tags) < 2:
            return None
        keep = tags[0]
        rest = tags[1:]
        # 从原标题里逐个删掉 rest 标签 + 相邻空白
        after = original
        for tag in rest:
            after = re.sub(re.escape(tag) + r"\s*", "", after, count=1)
        after = re.sub(r"\s+", " ", after).strip()
        # 附一个提示片段，明确告诉用户其他 hashtag 去了哪
        moved = "、".join(rest)
        after_with_hint = f"{after}（其余话题 {moved} 搬到正文）" if moved else after
        return (original, after_with_hint)

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Renderer
# ─────────────────────────────────────────────────────────────────────────────
def _fmt_pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _fmt_pp(x: float) -> str:
    sign = "+" if x >= 0 else ""
    return f"{sign}{x:.1f} pp"


def _fmt_ci(ci: list[float] | None) -> str:
    if not ci or len(ci) != 2:
        return "n/a"
    return f"[{ci[0]:+.3f}, {ci[1]:+.3f}]"


def _render_descriptive_block(desc: dict[str, Any]) -> list[str]:
    """把 descriptive 统计字段渲染成 markdown 行。"""
    if desc.get("type") != "historical_ratio_diff":
        return []
    if "group_a" not in desc or "group_b" not in desc:
        reason = desc.get("unavailable_reason") or "描述统计暂不可用"
        return [f"  - 描述统计：暂不可用（{reason}）"]
    a, b = desc["group_a"], desc["group_b"]
    diff_pp = desc.get("abs_diff_pp", 0.0)
    return [
        f"  - **历史占比对照**：{a['label']} (n={a['n']}) → {_fmt_pct(a['rate'])} "
        f"vs {b['label']} (n={b['n']}) → {_fmt_pct(b['rate'])}",
        f"    绝对差：**{_fmt_pp(diff_pp)}**",
    ]


def _render_coefficient_block(coef: dict[str, Any] | None) -> list[str]:
    if not coef:
        return []
    lines: list[str] = []
    b = coef.get("bootstrap")
    if b:
        stable_tag = "稳定" if b.get("verdict") == "STABLE" else "不稳定"
        lines.append(
            f"  - 模型附注（控制其他特征后）：系数 {b['mean']:+.3f}，"
            f"Bootstrap {b.get('n_iter', '?')} 次 95% CI {_fmt_ci(b['ci95'])}，"
            f"同号率 {b['same_sign_ratio']:.0%} → {stable_tag}"
        )
    elif coef.get("model_coef") is not None:
        lines.append(f"  - 模型附注：系数 {coef['model_coef']:+.4f}")
    ts = coef.get("time_segment")
    if ts:
        lines.append(
            f"  - 跨时间段稳定性：符号一致率 {ts['sign_consistency']:.0%} → {ts['verdict']}"
        )
    return lines


def _render_suggestion(s: Suggestion, original_title: str) -> list[str]:
    tmpl = _ACTION_TEMPLATES.get(s.action_code, {})
    action = tmpl.get("action", "（未定义动作模板）")

    lines: list[str] = []
    sev_tag = _SEVERITY_EMOJI.get(s.severity, s.severity)
    lines.append(f"### {sev_tag}：{s.title}")
    lines.append("")

    # 1) 个性化"你当前状态"行（基于用户实际输入）
    human = (s.user_state or {}).get("human")
    if human:
        lines.append(f"- **你当前**：{human}")

    lines.append(f"- **建议动作**：{action}")
    lines.extend(_render_descriptive_block(s.descriptive))
    lines.extend(_render_coefficient_block(s.coefficient))
    if s.caveats:
        for cav in s.caveats:
            lines.append(f"  - ⚠ {cav}")

    # 2) 基于用户原标题的算法变换示例；失败时回退到通用示例
    transform = _transform_title(s.action_code, original_title, s.user_state or {})
    if transform is not None:
        before, after = transform
        lines.append("  - **针对你标题的改写建议**（算法生成，仅供参考）：")
        lines.append(f"    - 原：`{before}`")
        lines.append(f"    - 改：`{after}`")
    else:
        fb_before = tmpl.get("fallback_before")
        fb_after = tmpl.get("fallback_after")
        if fb_before and fb_after:
            lines.append("  - **示例（通用模板，仅作风格参考）**：")
            lines.append(f"    - 原：`{fb_before}`")
            lines.append(f"    - 改：`{fb_after}`")

    lines.append("")
    return lines


def _render_info_note(note: dict[str, Any]) -> list[str]:
    sev = _SEVERITY_EMOJI["info"]
    return [
        f"### {sev}：{note.get('title', '')}",
        "",
        f"- {note.get('message', '')}",
        "",
    ]


def render_markdown(result: DiagnoseResult) -> str:
    lines: list[str] = []
    lines.append("# 小红书笔记诊断报告")
    lines.append("")
    lines.append(f"> **引擎版本**：`{result.version}`")
    lines.append(f"> **依据模型**：`{result.model_ref}`")
    lines.append(f"> **生成时间**：{result.generated_at_utc}")
    lines.append("")
    lines.append(f"> ⚠ **使用范围**：{result.vertical_notice}")
    lines.append("")

    # 提取输入摘要 + 原标题回显（让用户一眼知道"这是在分析我的内容"）
    f = result.input_features
    original_title = (result.original_input or {}).get("title", "")
    lines.append("## 输入摘要")
    lines.append("")
    if original_title:
        # 转义反引号，避免标题里含 ` 破坏排版
        safe_title = original_title.replace("`", "ˋ")
        lines.append(f"- **你的标题**：`{safe_title}`")
    lines.append(
        f"- 标题长度：{int(f.get('title_len', 0))} 字 ｜ 正文长度：{int(f.get('body_len', 0))} 字"
    )
    lines.append(
        f"- 标题 emoji：{int(f.get('title_emoji_count', 0))} 个 ｜ "
        f"问号：{'有' if f.get('title_has_question') else '无'} ｜ "
        f"hashtag：{int(f.get('title_hashtag_count', 0))} 个"
    )
    lines.append("")

    # 建议区
    if result.suggestions:
        lines.append("## 建议列表")
        lines.append("")
        for s in result.suggestions:
            lines.extend(_render_suggestion(s, original_title))
    else:
        lines.append("## 建议列表")
        lines.append("")
        lines.append("✅ 本篇笔记未触发任何已知的强证据建议。注意这**不**代表必爆，"
                     "仅说明在 2 条强规则上没有扣分项。")
        lines.append("")

    # 信息区
    if result.info_notes:
        lines.append("## 参考信息")
        lines.append("")
        for n in result.info_notes:
            lines.extend(_render_info_note(n))

    # 免责声明
    lines.append("---")
    lines.append("")
    lines.append("### ⚠ 使用边界（务必阅读）")
    lines.append("")
    lines.append(f"1. {result.combo_disclaimer}")
    lines.append("2. 本工具仅输出统计相关的**写作级**提示，不承诺任何流量/点赞提升。")
    lines.append("3. 模型 hold-out AUC ≈ 0.53，对整体排序能力极弱；请勿把诊断结果当作\"爆款预测\"。")
    lines.append("4. 样本集中于健身/减脂赛道；跨赛道使用时，描述统计可能不成立。")
    lines.append("")

    return "\n".join(lines)


__all__ = ["render_markdown"]
