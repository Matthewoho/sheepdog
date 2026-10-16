# 修过的毛病和守它的测试（7.55）

一行一个修过的毛病：规格编号、一句话症状、守它的测试（`模块.类.方法`，模块名按 `python3.12 -m unittest discover -s tests` 的叫法，不带 `tests.`）。

- 上线闸门 `scripts/release_gate.py` 会核这里列的每个测试都还在、会被全量测试跑到、没被跳过；少一个就算红。
- 一行可以列几个测试，每个用反引号括起来。
- 找不到守它的测试就写「缺」，不编。写「缺」的行闸门不拦，但要尽快补。
- 测试数据一律造的（`*_test_*`），仓库是公开的，不放真聊天内容。

| 规格 | 症状 | 守它的测试 |
| --- | --- | --- |
| 7.45 | 话题开头那条的序号当成 0（实际是 -1，回复从 0 起），话题群里开头和回复认反 | `test_group_threads.GroupThreadReplyTest.test_topic_chat_root_is_minus_one` |
| 7.45 | 主人在没人负责的群里 @ 机器人，路由成 self，谁也没送到 | `test_group_threads.OwnerInstructionTest.test_roster_chat_then_bus` |
| 7.45 | 机器人刷屏的话题回复按话题归属整串直达会话 | `test_group_threads.GroupThreadReplyTest.test_bot_thread_reply_not_attributed_human_still_is` |
| 7.45 | 测试读不了配置时，hook 日志退回默认状态目录，差点写进本机正式状态目录 | 缺（修在测试里；现在由闸门把 HOME、XDG_*、SHEEPDOG_* 指到临时目录兜住，不是单测） |
| 7.47 第 1 条 | forward 把已经送到目标会话的消息又重发一遍 | `test_review_priority.ForwardAfterPriorityDuplicateTest.test_priority_on_owner_reply_reaches_alpha_once`、`test_review_priority.ForwardSkipTest.test_forward_skips_message_already_delivered_to_target` |
| 7.47 第 2 条 | 主人撤回 / 改口插不了队，要等原批次交回执 | `test_review_priority.CorrectionDoesNotJumpTest.test_recall_of_priority_reply_reaches_running_session_same_round`、`test_review_priority.CorrectionDoesNotJumpTest.test_edit_of_priority_reply_reaches_running_session_same_round` |
| 7.47 第 3 条 | 插话答过以后交的「等主人拍板」回执把会话卡在 waiting_human | `test_review_priority.StaleWaitingAfterPriorityAnswerTest.test_board_and_next_batch_do_not_claim_waiting_for_owner_after_answer`、`test_review_priority.NeedsDecisionScopeTest.test_answered_by_priority_becomes_handled_with_note` |
| 7.47 第 4 条 | 收工查回执把插话正文里提到的原批次号当成批次开头 | `test_review_priority.StopHookNeedleTest.test_owner_typing_not_hidden_by_priority_line`、`test_review_priority.BatchOpeningTest.test_mentions_of_batch_are_not_openings` |
| 7.47 第 4 条返工 | 会话记录掐掉长消息中间，排在规则后面的批次头认不出 | `test_review_priority.BatchOpeningTest.test_truncated_openings_still_recognized`、`test_review_priority.BatchOpeningTest.test_truncated_b_with_rules_found_in_record` |
| 7.47 第 5 条 | 插话批次发送中崩溃后留在 uncertain，没人再核、主人也不知道 | `test_review_priority.CrashRecoveryUncertainTest.test_priority_inserted_later_is_reconciled_or_owner_told`、`test_review_priority.UncertainRecheckTest.test_not_found_tells_owner_once_after_30_minutes_and_never_resends` |
| 7.47 第 6 条 | 插话送到后会话一直没回音，没人提醒主人 | `test_review_priority.PriorityFollowupTest.test_silent_session_owner_told_once` |
| 7.47 第 7 条 | agentapi 调用超时没接住，整轮投递中断 | `test_review_priority.SendTimeoutTest.test_batch_timeout_does_not_stop_other_sessions_same_round`、`test_review_priority.SendTimeoutTest.test_priority_timeout_stays_sending_then_reconciled` |
| 7.48 | 「待办」摘录里标题和列表读回时粘成一串（「事情Sammy 要一条…」） | `test_pending.ExcerptJoinTest.test_joined_inline_heading_and_list_from_readback`、`test_pending.ExcerptJoinTest.test_list_uses_sent_text_not_glued_readback` |
| 7.48 | 「待办」测试拿「现在减几分钟」当来源时间，过了午夜就红 | `test_pending.OwnerPendingTest.test_source_on_previous_day_shows_month_day` |
| 7.51 | 7.45 的群消息分支把话题群当普通群（A 类指令进了话题群） | `test_topic_group.MergeWith745Test.test_a_class_never_applies_in_topic_group` |
| 7.51 | 话题群叠上「待办」重排后，给主人看的标题、摘录还带 markdown | `test_topic_group.PendingRelayoutTest.test_titles_use_markdown_free_excerpt` |
| 7.51 | 同一轮里登记又答掉的提问，话题里还发一行过时的「等你定」（G4） | `test_topic_group.ChangeLineFinalStateTest.test_ask_registered_and_answered_in_same_round_posts_no_decide_line` |
| 7.51 | 「放群里」开出的事项没算上私聊原话题里还开着的提问（G7） | `test_topic_group.P1CFixTest.test_moved_item_counts_open_ask_in_dm_thread` |
| 7.51 | 主人在话题群新开话题，当轮没有回音（G6） | `test_topic_group.P1CFixTest.test_owner_new_topic_gets_a_line_right_away` |
| 7.51 | 卡片里放了点不开的话题链接 | `test_topic_group.LinkTest.test_no_thread_links_anywhere_and_chat_links_where_jumping` |
| 7.54 | 送进会话的图片只是一把飞书钥匙，宿主显示 Preview unavailable，模型看不见图 | `test_media.ImageDeliveryTest.test_regression_7_54_image_key_replaced_with_local_path`、`test_media.ImageDeliveryTest.test_failure_keeps_original_with_reason_and_command`、`test_media.ImageDeliveryTest.test_same_image_not_downloaded_twice` |
| 7.56 | 测试群里主人 @ 机器人，被当成指令送进正式总线（当时测试群不在 ignore；这几条守「进了 ignore 就不当真」，外加不误伤的对照） | `test_chat_scope.OwnerAtBotInIgnoredGroupTest.test_regression_7_56_owner_at_bot_in_ignored_group_is_not_instruction`、`test_chat_scope.OwnerAtBotInIgnoredGroupTest.test_regression_7_56_owner_reply_in_ignored_topic_group_thread_is_not_instruction`、`test_chat_scope.OwnerAtBotInIgnoredGroupTest.test_regression_7_56_not_ignored_group_still_instruction` |
| 7.56 | 建了测试群忘了登记进 ignore，正式牧羊犬照样把里面的消息当真 | `test_chat_scope.DoctorTestGroupsTest.test_lists_unignored_test_groups` |
