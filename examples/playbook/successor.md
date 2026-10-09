## 接手
你接手前任会话 `{{predecessor_id}}` 的工作。
- 前任 transcript：`{{predecessor_transcript}}`
- 前任产出的文档在同一会话目录：`{{predecessor_dir}}`
- **开工前必须完整读完前任的上下文**：用脚本按顺序抽出全部 USER_INPUT 和 PLANNER_RESPONSE 正文，跳过工具输出（避免撑爆上下文），从头读到尾。先 `head -c 2000` 看一行确认正文字段名，再写脚本。
- 主人在前任里说过的话，视同对你说的。
- 读完先回执一份「我掌握了什么、有什么不清楚」，status 用 needs_decision，然后再开始处理信号。
