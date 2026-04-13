本目录：小红书生产链外置提示词（YAML）
=====================================

- extract_viral_patterns.yaml  → 挖掘（MiniMax）
- recreate_content.yaml        → 二创标题/正文/标签（MiniMax）
- predict_viral_score.yaml     → 预测分（MiniMax）
- prepare_xhs_post.yaml        → 待发清单模板（非模型，main.py 使用）

修改后 OpenClaw 容器内需重新构建：docker compose build openclaw && docker compose up -d

自定义目录（可选）：环境变量 OPENCLAW_PROMPTS_DIR 指向含本目录同名 yaml 的文件夹。

user 侧含 JSON 样本时用 string.Template（$占位符），见 prompt_store.substitute_user_template。
