# 上线的功能、修过的毛病和守它的测试（7.55、7.60）

## 规矩（7.60）

- 每个上线的功能、每个修过的毛病，至少在这里登记一条守它的测试。新功能上线、修好一个毛病，同一个提交里补上这一行。
- 上线脚本第一步跑 `python3.12 scripts/release_gate.py --check <提交号> --specs 7.xx,7.yy`，点名这次上的规格编号。
  每个编号在这里至少要有一行列了测试（只写「缺」的不算），列的测试在那个提交里都在；缺一个就上不去。
- 守卫要先证明管用：在导出副本里改坏它守的那段，确认它会红（不在工作树里就地改坏）。
- 模型、规则、提示词这类单测测不到的，靠定时体检的指标守，不在这里登记。

## 怎么写

一行一个功能或毛病：规格编号、一句话症状、守它的测试（`模块.类.方法`，模块名按 `python3.12 -m unittest discover -s tests` 的叫法，不带 `tests.`）。

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
| 7.52 | 会话交了回执、没给主人发 ask / report，主人在飞书上收不到任何回音（10-14 起 26 条） | `test_owner_echo.RelayTest.test_receipt_without_report_relays_one_card_quoting_owner` |
| 7.52 | 主人的话排队、送到后没回音，没有任何提醒（原来只有插话批次有） | `test_owner_echo.EchoLedgerTest.test_queued_then_delivered_then_silent_each_once` |
| 7.52 | 主人 @ 机器人的话没送进任何会话（10-16 19:41），谁也没收到、也没人告诉他 | `test_owner_echo.EchoLedgerTest.test_undelivered_reported_on_the_spot_once` |
| 7.52 | 话题群里主人的话，回执带回没回到那件事的话题里 | `test_topic_group.EchoInTopicGroupTest.test_owner_reply_in_item_thread_relayed_back_into_that_thread` |
| 7.52 返工 | 一批里出自不同地方的主人的话被合成一张带回卡片，发到其中一句的位置（P1C 03:59，E1 的回音落进 T-3 话题） | `test_topic_group.EchoInTopicGroupTest.test_relay_one_card_per_destination_in_the_same_batch`、`test_topic_group.EchoInTopicGroupTest.test_relay_same_destination_two_sentences_one_card` |
| 7.52 上线后 | 推迟带回的 10 分钟里同一会话又交了回执，每份都把还欠着的那句改记到最新那份上：卡片引用主人那句，正文却是另一件事（10-17 04:59） | `test_owner_echo.DeferredRelayBindingTest.test_regression_752_deferred_relay_keeps_its_own_batch_receipt` |
| 7.52 上线后 | 这句随 b1 送到、b1 还在跑，之前判过超时的 b0 晚到的回执先到，绑上了这句：卡片正文是 b0 那件事（不推迟也会错） | `test_owner_echo.DeferredRelayBindingTest.test_regression_752_late_receipt_of_an_earlier_batch_does_not_take_it` |
| 7.52 上线后 | 插话批次送进去的那句，绑上送到后的第一份回执以后，又被同一会话后来的回执改绑 | `test_owner_echo.DeferredRelayBindingTest.test_regression_752_priority_binds_first_receipt_after_delivery_only` |
| 7.52 上线后 | 改成只绑一次时守住原有行为：推迟期间会话自己 report 了，就算还上、不发卡片 | `test_owner_echo.DeferredRelayBindingTest.test_regression_752_session_reported_during_deferral_no_card` |
| 7.52 上线后 | 绑上回执、还在推迟的那句又被转交给别的会话：不能拿原来那个会话的回执带回，要等新会话送它进去那一批的回执 | `test_owner_echo.DeferredRelayBindingTest.test_regression_752_forward_after_binding_drops_the_old_receipt` |
| 7.52 上线后 | 同一份回执、同一个去处的两句到点时间不同：先到点的卡片占了「每批、每个去处一次」，后一句再也带不回，还被报「插话没回音」 | `test_owner_echo.DeferredRelayBindingTest.test_regression_752_same_receipt_same_place_sent_together_when_first_is_due` |
| 7.53 | 私聊里没引用的新指令被猜成唯一那条提问的回答，送错会话还关掉了提问（10-17 02:43 发版 → jev） | `test_escalation.EscalationTest.test_unquoted_single_open_is_an_instruction_to_bus_and_does_not_close` |
| 7.53 | 总线 forward 主人的话时顺手关掉目标会话最近一条提问 | `test_escalation.EscalationTest.test_forward_owner_message_verified_and_closes_only_with_answers` |
| 7.53 | `--answers` 指了别的会话的提问或已关的提问，悄悄关错 | `test_escalation.EscalationTest.test_forward_answers_must_be_open_and_belong_to_target` |
| 7.54 | 送进会话的图片只是一把飞书钥匙，宿主显示 Preview unavailable，模型看不见图 | `test_media.ImageDeliveryTest.test_regression_7_54_image_key_replaced_with_local_path`、`test_media.ImageDeliveryTest.test_failure_keeps_original_with_reason_and_command`、`test_media.ImageDeliveryTest.test_same_image_not_downloaded_twice` |
| 7.56 | 测试群里主人 @ 机器人，被当成指令送进正式总线（当时测试群不在 ignore；这几条守「进了 ignore 就不当真」，外加不误伤的对照） | `test_chat_scope.OwnerAtBotInIgnoredGroupTest.test_regression_7_56_owner_at_bot_in_ignored_group_is_not_instruction`、`test_chat_scope.OwnerAtBotInIgnoredGroupTest.test_regression_7_56_owner_reply_in_ignored_topic_group_thread_is_not_instruction`、`test_chat_scope.OwnerAtBotInIgnoredGroupTest.test_regression_7_56_not_ignored_group_still_instruction` |
| 7.56 | 建了测试群忘了登记进 ignore，正式牧羊犬照样把里面的消息当真 | `test_chat_scope.DoctorTestGroupsTest.test_lists_unignored_test_groups` |
| 7.60 | 上线闸门只核清单里的测试还在，不核这次上的功能有没有登记：`--specs` 点名的编号没有登记、只写「缺」、登记的测试名写错，都要拦住 | `test_release_gate.SpecsCheckTest.test_regression_760_unregistered_spec_is_red`、`test_release_gate.SpecsCheckTest.test_regression_760_que_only_spec_is_red`、`test_release_gate.SpecsCheckTest.test_regression_760_misspelled_test_is_red`、`test_release_gate.SpecsCheckTest.test_regression_760_spec_number_matches_exactly`、`test_release_gate.SpecsCheckTest.test_regression_760_reads_the_commit_not_the_worktree`、`test_release_gate.SpecsCheckTest.test_regression_760_record_still_required`、`test_release_gate.SpecsCheckTest.test_regression_760_registered_specs_pass` |
