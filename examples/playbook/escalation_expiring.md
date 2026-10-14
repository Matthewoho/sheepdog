<!-- 可选（7.36）。「需要你定」快到期时（离 [escalation] open_hours 还剩 remind_before_hours 小时），给负责会话的提醒末尾附的话。
     问的是什么、几点到期，由 sheepdog 写在前面。每条提问只提醒一次；到期照旧自动关闭。没有这个文件时用代码里的默认话。 -->
请核实主人是不是已经在别处处理了：办完了就用 `{{cli}} resolve --topic {{topic_id}} --message-id {{ask_message_id}} --why "<依据>"` 结案；还要他定，就用 `{{cli}} ask` 带同一把 `--key` 在原话题里再提醒他一次。
