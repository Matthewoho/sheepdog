<!-- Stop hook（7.37 第 5 件）：会话这一轮要结束、手上那批还没交回执时，sheepdog 不让它停，把这段话作为系统消息给它。同一批最多拦两次；缺了这个文件用代码里的默认话。 -->
[sheepdog] 先别结束：批次 `{{batch_id}}` 还没交回执。
- 处理完了：`{{receipt_command}}`
- 还在等别人回复：`{{waiting_command}}`，summary 写清等谁、等什么。
- 要主人定的：status 写 `needs_decision`，先用 `{{cli}} ask` 问他。
交完回执再结束。
