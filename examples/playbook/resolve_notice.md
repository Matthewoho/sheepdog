<!-- 可选（7.36）。会话用 sheepdog resolve 结案后，机器人在那条提问的话题里补的那一句（只取一行；默认不 @ 主人，见 [escalation] resolve_notice_at）。
     {{why}} 是会话写的依据，{{session}} 是会话标题。开头不要写成「🐕 需要你定」，否则会被当成新提问。没有这个文件时用代码里的默认话。 -->
✅ 已结案：{{why}}（{{session}}）
