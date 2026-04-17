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
# 动作文案（仅"该做什么"的说明；示例一律基于用户原文算法生成，无任何硬编码范例）
# ─────────────────────────────────────────────────────────────────────────────
_ACTION_DESC: dict[str, str] = {
    "REMOVE_TITLE_QUESTION_MARK":
        "删除标题中的所有问号（「？」「?」），改写成陈述句或感叹句",
    "ADD_ONE_TITLE_EMOJI":
        "在标题开头加入 1 个情绪 emoji（候选池：🔥 / 💪 / ✨ / 😍 / 🌱 / 🎯 等，按你的内容调性自选）",
    "REDUCE_TITLE_HASHTAG_TO_ONE":
        "把标题里多余的 hashtag 移到正文，只保留 1 个最相关的",
}

# 博主视图用的"人话版" action 名与"为什么"——完全不提 coef / CI / AUC / pp。
_BLOGGER_ACTION: dict[str, dict[str, str]] = {
    "REMOVE_TITLE_QUESTION_MARK": {
        "headline": "标题去掉问号",
        "why_template": (
            "标题带问号让读者觉得是「疑问/不确定」，划走率更高。"
            "我们看的健身/减脂赛道里，**每 10 条「{a_label}」的笔记，大约只有 {a_hit} 条能上热门；"
            "「{b_label}」的能到 {b_hit} 条**。差的那 {diff_hit} 条在长期会累积。"
        ),
        "how": "末尾的问号直接删；中间的问号换成句号。换成感叹号也行，只要别保留疑问语气。",
    },
    "ADD_ONE_TITLE_EMOJI": {
        "headline": "标题加 1 个 emoji",
        "why_template": (
            "emoji 让标题在密集的信息流里更跳眼。"
            "我们的数据里，**每 10 条「{a_label}」的笔记，大约只有 {a_hit} 条上热门；"
            "「{b_label}」的能到 {b_hit} 条**。"
            "⚠ 注意：0→1 的差别明显，1→2 基本没差——**有 1 个就够，不用堆**。"
        ),
        "how": "在标题最前面放一个 emoji 就行。放哪里不重要，放了就有效。",
    },
    "REDUCE_TITLE_HASHTAG_TO_ONE": {
        "headline": "标题只留 1 个 hashtag",
        "why_template": (
            "标题塞多个 #xx 让读者觉得「这是硬推/广告」，划走率高。"
            "数据里，**每 10 条「{a_label}」的笔记，大约只有 {a_hit} 条上热门；"
            "「{b_label}」的能到 {b_hit} 条**——差距很明显。"
        ),
        "how": "只保留最相关的 1 个，其它全部搬到正文末尾（正文里的 hashtag 不会让标题显得像广告）。",
    },
}

_SEVERITY_EMOJI: dict[str, str] = {
    "high":   "🔴 高优先级",
    "medium": "🔵 中优先级",
    "info":   "🟡 参考信息",
}

# ADD_ONE_TITLE_EMOJI 的候选池
_EMOJI_POOL: tuple[str, ...] = ("🔥", "💪", "✨", "😍", "🌱", "🎯")
_HASHTAG_RE = re.compile(r"#[^\s#＃]+")

# 按标题关键词挑一个语气匹配的 emoji；都不命中时再按哈希稳定 fallback。
# 关键词顺序很重要：越靠前的语气越"强"，优先命中。
_EMOJI_THEME_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("亲测", "真的", "绝了", "绝绝子", "爆", "太好用", "好绝"), "🔥"),
    (("练", "撸铁", "运动", "燃脂", "坚持", "深蹲", "瑜伽", "汗"), "💪"),
    (("瘦", "减脂", "减肥", "体重", "掉秤", "斤"), "😍"),
    (("教程", "方法", "步骤", "指南", "技巧", "攻略", "保姆级", "干货"), "🎯"),
    (("分享", "记录", "日记", "经验", "心得", "复盘", "盘点"), "✨"),
)


def _pick_emoji_for(title: str) -> str:
    """按标题语气匹配 emoji；无命中回退到基于哈希的稳定选择。
    不同标题 → 不同 emoji，同一标题多次渲染保持一致。"""
    if not title:
        return _EMOJI_POOL[0]
    for keywords, emoji in _EMOJI_THEME_RULES:
        if any(k in title for k in keywords):
            return emoji
    idx = sum(ord(c) for c in title) % len(_EMOJI_POOL)
    return _EMOJI_POOL[idx]


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
        picked = _pick_emoji_for(original)
        after = f"{picked} {original}"
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
    action = _ACTION_DESC.get(s.action_code, "（未定义动作）")

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

    # 2) 基于用户原标题的算法变换示例；
    #   不再有 fallback 模板——原标题为空或算法无法变换时，直接省掉示例段落。
    transform = _transform_title(s.action_code, original_title, s.user_state or {})
    if transform is not None:
        before, after = transform
        note = (
            "算法生成自你原标题；emoji 可从候选池自选"
            if s.action_code == "ADD_ONE_TITLE_EMOJI"
            else "基于你原标题算法生成"
        )
        lines.append(f"  - **改写示例**（{note}）：")
        lines.append(f"    - 原：`{before}`")
        lines.append(f"    - 改：`{after}`")

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


def render_markdown(result: DiagnoseResult, audience: str = "blogger") -> str:
    """
    audience:
      - "blogger"（默认）：面向博主的人话报告，不含 CI/系数/AUC 等术语
      - "dev"：面向开发者/研究者的完整学术版，保留全部统计细节
    """
    if audience == "dev":
        return _render_dev_view(result)
    return _render_blogger_view(result)


# ─────────────────────────────────────────────────────────────────────────────
# 博主视图（默认）
# ─────────────────────────────────────────────────────────────────────────────
def _rate_to_hits_out_of_10(rate: float) -> int:
    """把比例转成「10 条里大约 X 条」的整数。夹紧到 [0, 10]。"""
    x = round(rate * 10)
    return max(0, min(10, int(x)))


def _blogger_why(action_code: str, desc: dict[str, Any]) -> str | None:
    """把 descriptive.group_a/b 的 rate 翻译成人话。
    - group_a 是「不推荐的那种写法」（带问号 / 无 emoji / 多 hashtag）
    - group_b 是「推荐的那种写法」
    返回 None 表示无法生成（desc 缺字段），交给调用方省略。
    """
    tpl = _BLOGGER_ACTION.get(action_code, {}).get("why_template")
    if not tpl or desc.get("type") != "historical_ratio_diff":
        return None
    a = desc.get("group_a")
    b = desc.get("group_b")
    if not a or not b:
        return None
    a_hit = _rate_to_hits_out_of_10(a.get("rate", 0.0))
    b_hit = _rate_to_hits_out_of_10(b.get("rate", 0.0))
    return tpl.format(
        a_label=a["label"],
        b_label=b["label"],
        a_hit=a_hit,
        b_hit=b_hit,
        diff_hit=abs(b_hit - a_hit),
    )


def _render_blogger_suggestion(s: Suggestion, idx: int, original_title: str) -> list[str]:
    meta = _BLOGGER_ACTION.get(s.action_code)
    if not meta:
        # 未知 action_code，退回开发者格式以免信息丢失
        return _render_suggestion(s, original_title)

    lines: list[str] = []
    lines.append(f"### 建议 {idx}：{meta['headline']}")
    lines.append("")

    # 你当前的情况（尽量具体）
    human = (s.user_state or {}).get("human") or ""
    # engine 里的 human 字符串本身以 "你当前…" 开头，这里换个标签避免重复
    human = human.strip()
    if human.startswith("你当前"):
        human = human[len("你当前"):].lstrip("的 ，:：")
    if human:
        lines.append(f"**现状**：{human}")
        lines.append("")

    # 为什么
    why = _blogger_why(s.action_code, s.descriptive)
    if why:
        lines.append(f"**为什么**：{why}")
        lines.append("")

    # 怎么改（先给人话说明，再给基于你原标题的 before/after）
    lines.append(f"**怎么改**：{meta['how']}")
    lines.append("")

    transform = _transform_title(s.action_code, original_title, s.user_state or {})
    if transform is not None:
        before, after = transform
        lines.append("原：")
        lines.append(f"> `{before}`")
        lines.append("")
        lines.append("改成：")
        lines.append(f"> `{after}`")
        lines.append("")
        if s.action_code == "ADD_ONE_TITLE_EMOJI":
            lines.append("（上面的 emoji 是按你标题语气挑的，你也可以换成 💪 🔥 ✨ 😍 🌱 🎯 中任意一个。）")
            lines.append("")

    return lines


def _render_blogger_view(result: DiagnoseResult) -> str:
    original_title = (result.original_input or {}).get("title", "")
    lines: list[str] = []

    # 头部：一句话说清这是啥、给谁看的
    lines.append("# 你的笔记诊断")
    lines.append("")
    if original_title:
        safe_title = original_title.replace("`", "ˋ")
        lines.append(f"**你的标题**：`{safe_title}`")
        lines.append("")

    # 总览行：几条建议
    n_sugg = len(result.suggestions)
    if n_sugg == 0:
        lines.append("✅ **在我们关注的 3 个维度上（标题问号 / emoji / hashtag 数量），你都没踩坑。**")
        lines.append("")
        lines.append("> 注意：这不代表这条笔记一定会爆；只代表**标题的 3 个已知易扑街点**都没中。正文好不好、选题对不对，这个工具看不出来。")
        lines.append("")
    elif n_sugg == 1:
        lines.append(f"工具看你的标题后，给你 **1 条** 建议。")
        lines.append("")
    else:
        lines.append(f"工具看你的标题后，给你 **{n_sugg} 条** 建议，按下面编号往下改。")
        lines.append("")

    # 建议正文
    for i, s in enumerate(result.suggestions, start=1):
        lines.extend(_render_blogger_suggestion(s, i, original_title))

    # 博主视图有意不显示 info_notes：
    #   - 它们是"弱到不建议行动"的信号（如 body_len 系数 +0.0021/字）
    #   - 含有博主看不懂的措辞；强行翻译成人话的性价比低
    #   - 开发者视图仍保留，研究者可以通过 --view dev 查看

    # 边界声明：3 行，不再有 AUC / hold-out / 线性独立 这些术语
    lines.append("---")
    lines.append("")
    lines.append("### 使用边界（30 秒）")
    lines.append("")
    lines.append("1. 这个工具**只检查标题的 3 件事**：有没有问号、有没有 emoji、hashtag 数量。"
                 "内容好不好、选题对不对，它看不出来。")
    lines.append("2. 数据来自 **700+ 篇健身/减脂赛道**笔记。其他赛道（穿搭/美妆/美食等）参考价值有限。")
    lines.append("3. **这不是爆款预测工具**——它只帮你避开 3 个已知容易扑街的小坑。"
                 "改了也不保证涨流量，改了之后是否有效请你自己记录。")
    lines.append("")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# 开发者视图（保留原始学术版；带 CI / 系数 / AUC 等）
# ─────────────────────────────────────────────────────────────────────────────
def _render_dev_view(result: DiagnoseResult) -> str:
    lines: list[str] = []
    lines.append("# 小红书笔记诊断报告（开发者视图）")
    lines.append("")
    lines.append(f"> **引擎版本**：`{result.version}`")
    lines.append(f"> **依据模型**：`{result.model_ref}`")
    lines.append(f"> **生成时间**：{result.generated_at_utc}")
    lines.append("")
    lines.append(f"> ⚠ **使用范围**：{result.vertical_notice}")
    lines.append("")

    f = result.input_features
    original_title = (result.original_input or {}).get("title", "")
    lines.append("## 输入摘要")
    lines.append("")
    if original_title:
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

    if result.info_notes:
        lines.append("## 参考信息")
        lines.append("")
        for n in result.info_notes:
            lines.extend(_render_info_note(n))

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
