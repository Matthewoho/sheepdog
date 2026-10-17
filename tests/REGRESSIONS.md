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
| 7.3 | 总线 forward 转交：目标是总线、known、不存在的会话要拒绝；转到 managed 会话的消息改归属、回到待推、带上总线备注 | `test_roster.ForwardTest.test_forward_rejects_known_bus_and_unknown`、`test_roster.ForwardTest.test_forward_delivers_to_managed_with_note` |
| 7.3 | 机器人发给主人的「找主人」消息（带 drop 前缀）被当成新信号推回总线，形成回环 | `test_handover_watch.EscalationTest.test_self_escalation_dropped` |
| 7.4 | 等待回复：对方的回复优先归给登记等待的会话（覆盖聊天归属、总线）；同一人多条等待取最新、按聊天限定 | `test_handover_watch.WatchTest.test_reply_routed_to_watcher_until_confirmed`、`test_handover_watch.WatchTest.test_chat_scoped_and_newest_wins` |
| 7.4 | 等待计时：15 / 30 分钟各提醒一次，60 分钟到期关闭并提示 | `test_handover_watch.WatchTest.test_nudges_once_each_then_expire` |
| 7.7 | 安全闸 hold：命中拦截规则的消息不投给名册会话、等待会话，改投总线 | `test_security.GateTest.test_hold_overrides_roster_and_all_messages`、`test_security.GateTest.test_hold_overrides_watch_and_keeps_waiting` |
| 7.7 | 安全闸 tag：被标记的消息照常投递，正文前带警示 | `test_security.GateTest.test_tag_delivered_with_banner_before_body` |
| 7.7 | 被 hold 的消息 forward 时要带核对过的主人原话，核不上就拒绝 | `test_security.GateTest.test_hold_forward_requires_verified_quote` |
| 7.8 | 找主人聊天：机器人发的「需要你定」登记、本身丢弃；别的机器人消息丢弃 | `test_escalation.EscalationTest.test_bot_question_registered_and_dropped`、`test_escalation.EscalationTest.test_other_messages_dropped` |
| 7.8 | 主人引用回复某条「需要你定」，送回提问的会话 | `test_escalation.EscalationTest.test_quoted_reply_goes_to_that_topic` |
| 7.8 | 主人本人的消息（含编辑后重新判定）不走安全闸 | `test_escalation.EscalationTest.test_owner_reply_edit_also_skips_security_gate` |
| 7.8 | 总线 forward 主人本人在别处的发言（本来没带已核对标记），送到时要补「✅ 已核对」标注 | `test_regress_routing.ForwardOwnerWordsRegressionTest.test_regression_7_8_forward_owner_background_gets_verified_label` |
| 7.10 | 总线新开的会话：--chat 的聊天按归属直推给它；会话收掉后聊天归属释放回总线 | `test_dynamic.DynamicTest.test_chat_ownership`、`test_dynamic.DynamicTest.test_done_closes_and_releases` |
| 7.13 | 主人本人的发言当背景送负责会话：名册归属，其次 follow_hours 内最近处理过的会话，超时不送；只有背景时不要回执、不改状态 | `test_owner_context.OwnerContextTest.test_roster_owned_chat_goes_to_owner_as_context`、`test_owner_context.OwnerContextTest.test_follow_hours_recent_topic_or_nothing` |
| 7.13 | 会话代回、找主人聊天、ignore 聊天里主人的话不当背景送 | `test_owner_context.OwnerContextTest.test_skipped_cases` |
| 7.13 | 名册负责的聊天，主人先开口（这个聊天还没投递过消息）也要送负责会话 | `test_regress_routing.OwnerContextRosterRegressionTest.test_regression_7_13_owner_first_word_in_roster_chat_goes_to_owner` |
| 7.15 | 主人引用了认不出来源的消息：送总线附原文，绝不当成回答唯一那条未结提问（10-09「方案1」事故） | `test_quote_reply.QuoteReplyTest.test_incident_unregistered_quote_goes_to_bus_not_open_question` |
| 7.15 | 主人引用会话发给他的普通消息（不是提问），送回那个会话 | `test_quote_reply.QuoteReplyTest.test_quote_of_session_normal_reply_goes_back` |
| 7.15 | 机器人消息的来源只认第一行；别名、标题唯一命中才算 | `test_quote_reply.QuoteReplyTest.test_attribution_only_first_line`、`test_quote_reply.QuoteReplyTest.test_aliases_and_titles_unique` |
| 7.15 | 有引用的消息投递时都附「↪ 回复的是」 | `test_quote_reply.QuoteReplyTest.test_quote_line_everywhere` |
| 7.16 第 3 条 | 对方随便回一句（寒暄）就关掉等待；等会话确认期间提醒照发 | `test_gaps_watch.WatchCandidateTest.test_chit_chat_does_not_close`、`test_gaps_watch.WatchCandidateTest.test_paused_while_replied_even_if_long_ago` |
| 7.16 第 4 条 | 免打扰群里回复主人的消息被丢（「回复我」要排在免打扰之前） | `test_gaps_collect.MutedReplyTest.test_reply_to_me_in_muted_chat_dispatched_vip_still_dropped` |
| 7.22 | 一事一串：同一把钥匙第一次起新消息，之后回到它的话题里 | `test_threads.AskTest.test_same_key_first_new_message_then_in_thread` |
| 7.22 | 主人在话题里回复（没有引用），按话题送回会话、附开头那条；话题长了再附最近几条 | `test_threads.ThreadReplyTest.test_thread_reply_goes_back_with_the_root`、`test_threads.ThreadReplyTest.test_complex_thread_attaches_recent_and_thread_hint` |
| 7.22 | 代回前先看一眼：主人已经说过话就不发、检查只读不动账本；检查失败不发 | `test_threads.ReplyCheckTest.test_owner_replied_check_is_read_only_and_idempotent`、`test_threads.ReplyCheckTest.test_check_failure_does_not_send` |
| 7.22 | 代回防重：同样内容重试只发一条，先回「稍等」再回结论两条都发 | `test_threads.ReplyCheckTest.test_ack_then_conclusion_both_sent_and_same_text_retried_once` |
| 7.26 | 代回选身份：机器人在的群里用机器人身份发（带代回标记）；坏了会以主人本人身份在群里回 | `test_identity.ReplyIdentityTest.test_group_with_bot_uses_bot_with_mark`、`test_identity.ReplyIdentityTest.test_plain_text_bot_reply_has_mark` |
| 7.26 | 代回选身份：别人和主人的私聊用主人本人；机器人不在的群退回本人，并记下这个群给 doctor 列出 | `test_identity.ReplyIdentityTest.test_p2p_uses_owner_with_mark`、`test_identity.ReplyIdentityTest.test_group_without_bot_falls_back_and_is_listed` |
| 7.26 | 机器人发送报不在群里（230002）：这一条退回主人本人，内容和防重编号不变；坏了代回直接发送失败 | `test_identity.ReplyIdentityTest.test_230002_falls_back_with_same_idem_key` |
| 7.26 | 开关关着时，代回和以前一样一律用主人本人，不按群选身份 | `test_identity.DisabledTest.test_reply_unchanged` |
| 7.26 | 两个身份收到同一条，账本只记一条（received_by = both），不当成编辑重新路由 | `test_identity.DedupTest.test_user_first_then_bot_one_row_both`、`test_identity.DedupTest.test_bot_first_then_user_one_row_both_and_no_edit` |
| 7.26 | 机器人自己发的消息认作自己（self_bot）：不分派、不算主人发言，防止自己回自己 | `test_identity.RouteTest.test_bot_own_message_is_self`、`test_identity.BotSelfTest.test_bot_reply_is_not_owner_speech_and_not_dispatched` |
| 7.26 | 群里 @机器人、回复机器人发的消息，和 @主人、回复主人一样送到会话；坏了这些消息落进 Inbox 没人管 | `test_identity.RouteTest.test_at_bot_same_as_at_me_and_before_muted`、`test_identity.ReplyToBotTest.test_at_bot_event_dispatches_to_owner_of_chat_and_acks_like_at_me`、`test_identity.ReplyToBotTest.test_reply_to_bot_message_same_as_reply_to_owner` |
| 7.26 | 别人私聊机器人：不分派（drop / bot_dm），机器人自动回一句，同一个人 24 小时只回一次 | `test_identity.BotDmTest.test_not_dispatched_and_auto_reply_once_per_day` |
| 7.26 | 机器人实时接收子进程退出后按退避重启，连上后退避回到起点；坏了接收断掉不再起来 | `test_identity.ConsumerTest.test_exit_restarts_with_backoff_and_resets_after_ready` |
| 7.27 | 会话用 sheepdog reply 往「找主人」聊天里发（以主人身份回主人自己）：要拒绝、不调飞书、账本不变，开不开 [identity] 都一样 | `test_reply_owner_chat.RejectReplyInOwnerChatTest.test_reply_to_owner_in_owner_chat_is_rejected_and_changes_nothing`、`test_reply_owner_chat.RejectReplyInOwnerChatTest.test_threads_owner_chat_id_counts_as_owner_chat`、`test_identity.OwnerChatWithIdentityTest.test_reply_in_owner_chat_rejected_before_choosing_identity` |
| 7.27 | 找主人聊天里主人身份、带代回标记的消息，被当成主人亲自回复（owner_reply）投回会话 | `test_reply_owner_chat.AgentReplyInOwnerChatTest.test_agent_reply_card_is_not_owner_reply`、`test_reply_owner_chat.AgentReplyInOwnerChatTest.test_scenario_session_misuses_reply_then_old_style_message_pulled`、`test_identity.OwnerChatWithIdentityTest.test_marked_messages_in_owner_chat_are_not_owner_reply` |
| 7.29 | 话题里的 ask / report 末尾另起一行 @ 主人（第一行不变，起话题的第一条不加）；坏了主人看不到话题里的「需要你定」 | `test_thread_at_owner.ThreadAtOwnerTest.test_thread_replies_end_with_at_first_message_does_not`、`test_owner_card.FourPathsTest.test_ask_in_thread_has_at_element_and_same_key_as_plain` |
| 7.29 | 防重编号、sends、bot_outbox 记的正文不含 @ 那一行 | `test_thread_at_owner.ThreadAtOwnerTest.test_idem_key_and_ledger_text_exclude_the_at_line` |
| 7.29 | 拉回来的带 @ 主人的提问照旧认作机器人的提问，不当 at_me 再派一次 | `test_thread_at_owner.ThreadAtOwnerTest.test_pulled_back_thread_ask_is_still_bot_question_not_at_me`、`test_thread_at_owner.ThreadAtOwnerWithIdentityTest.test_bot_event_first_is_still_bot_question` |
| 7.36 | 会话结案 sheepdog resolve：结掉提问（resolved，记依据和结案会话），话题里补一句「已结案」；--key 结整个话题里开着的 | `test_pending.ResolveTest.test_resolve_by_message_id_without_key_replies_in_thread_and_does_not_at_owner`、`test_pending.ResolveTest.test_resolve_by_key_closes_all_open_in_the_thread` |
| 7.36 | 结案只能结自己名下、还开着的；坏了会话能结掉别的会话的提问 | `test_pending.ResolveTest.test_only_own_and_open` |
| 7.36 | 牧羊犬自己的话（结案那句、待办清单）拉回来，被登记成「需要你定」或投给会话 | `test_pending.ResolveTest.test_notice_pulled_back_is_not_an_escalation_and_goes_nowhere`、`test_pending.PendingListTest.test_owner_says_daiban_bot_replies_list_and_nothing_is_dispatched` |
| 7.36 | ask 当场记来源（这一批 / --about），--about 指错要拒绝 | `test_pending.SourcesTest.test_ask_records_batch_sources_without_system_or_owner_chat`、`test_pending.SourcesTest.test_ask_about_explicit_and_invalid` |
| 7.36 | 主人后来在来源私聊里说话：标「可能已处理」，提醒负责会话核实，只提醒一次 | `test_pending.SourcesTest.test_owner_spoke_in_source_chat_marks_and_reminds_once` |
| 7.36 | 主人对来源消息点表情也算可能已处理 | `test_pending.SourcesTest.test_owner_reaction_on_source_marks` |
| 7.36 | 同来源的另一条提问被答复或结案，其余的标可能已处理；重复提问时 ask 提示「已经在问」 | `test_pending.SourcesTest.test_sibling_answered_or_resolved_marks_the_other_session`、`test_pending.SourcesTest.test_sibling_resolved` |
| 7.36 | 快到期提醒负责会话一次 | `test_pending.SourcesTest.test_expiring_reminder_once_and_off` |
| 7.36 | 主人在找主人聊天单独发「待办」：机器人当轮回清单卡片，不送总线；同来源的提问合成一组 | `test_pending.PendingListTest.test_owner_says_daiban_bot_replies_list_and_nothing_is_dispatched`、`test_owner_card.FourPathsTest.test_pending_list_is_a_card`、`test_pending.PendingListTest.test_pending_single_section_with_check_line` |
| 7.37 第 5 件 | 收工查回执：牧羊犬管的会话在跑、没交回执，就拦一次（continue + 批次号 + 完整 receipt 命令）；坏了会话不交回执就收工 | `test_stop_hook.BlockTest.test_blocks_with_batch_and_full_receipt_command`、`test_stop_hook.BlockTest.test_managed_and_dynamic_sessions_block_too` |
| 7.37 第 5 件 | 每一批最多拦 2 次，第三次放行 | `test_stop_hook.BlockTest.test_at_most_twice_per_batch_then_new_batch_counts_again` |
| 7.37 第 5 件 | 后台任务没跑完（fullyIdle 不是 true）、回执文件已在、账本里那批已不是 sent：不拦 | `test_stop_hook.PassTest.test_not_fully_idle`、`test_stop_hook.PassTest.test_receipt_file_already_there`、`test_stop_hook.PassTest.test_ledger_already_got_it` |
| 7.37 第 5 件 | 命令行宿主的会话、被主人占着的会话：不拦 | `test_stop_hook.PassTest.test_cli_host_session`、`test_stop_hook.PassTest.test_owner_occupies_session` |
| 7.37 第 5 件 | 只拦牧羊犬管的会话：只认识 / 已退役的会话收工不拦（原有测试被「读不到会话记录就放行」遮住，新补） | `test_regress_stop_hook_scope.StopHookScopeTest.test_regression_7_37_unmanaged_session_with_batch_in_transcript_is_not_blocked`、`test_regress_stop_hook_scope.StopHookScopeTest.test_regression_7_37_same_session_managed_is_blocked` |
| 7.37 第 5 件 | 任何异常（入参不是 JSON、账本坏了）一个字都不输出、退出码 0 | `test_stop_hook.ErrorPathTest.test_stdin_not_json`、`test_stop_hook.ErrorPathTest.test_ledger_corrupt` |
| 7.37 第 4 件 | 插队：会话在跑上一批时，主人的回复当轮单独送到；其他信号照旧排队 | `test_owner_priority.PriorityDeliveryTest.test_running_owner_reply_goes_out_same_round_as_priority_batch`、`test_owner_priority.PriorityDeliveryTest.test_other_signals_stay_queued_until_receipt` |
| 7.37 第 4 件 | 会话被羊群看板判为中断 / 失联时不插队，照旧排队 | `test_owner_priority.NoJumpTest.test_stalled_or_lost_session_queues` |
| 7.38 第 1 条 | 「可能已处理」的被挪出「要你定的」，清单显示 0 条；应留在原段，下面加「↳ 你几点在哪说过……」 | `test_pending.PendingListTest.test_pending_single_section_with_check_line`、`test_pending.PendingListTest.test_check_line_before_reminder_is_queued` |
| 7.38 第 2 条 | 不给 --about 时，一批来自多个聊天仍记来源（主人在任一个聊天说话都触发）；补来源同一规则 | `test_pending.SourcesTest.test_multi_chat_batch_records_no_source_and_hints`、`test_pending.SourcesTest.test_backfill_same_rule_multi_chat_records_nothing` |
| 7.38 第 3 条 | 来源是群时，主人在群里随便说话也算可能已处理（应只认回复 / 引用来源、在来源话题里说的） | `test_pending.SourcesTest.test_group_source_plain_speech_does_not_count` |
| 7.38 第 4 条 | sheepdog escalations --topic T 只列这个会话名下的（带 --all 也一样） | `test_pending.SourcesTest.test_escalations_topic_filter` |
| 7.38 第 6 条 | Stop hook 读会话记录：这一批送达后主人亲自打过字不拦；hook 插的 SYSTEM_SDK 输入不算；末尾找不到这一批不拦 | `test_stop_hook.TranscriptTest.test_owner_typed_after_batch`、`test_stop_hook.TranscriptTest.test_sdk_and_system_inputs_do_not_count`、`test_stop_hook.TranscriptTest.test_batch_not_in_transcript` |
| 7.38 第 7 条 | 名册会话的回执说法：要交回执，不交要等回执超时（约 N 分钟，N 取配置）才收得到下一批 | `test_managed_receipt.ManagedReceiptTest.test_texts_say_so_with_configured_minutes` |
| 7.38 第 8 条 | current_batch / 补来源取成主人插话批次（p…），ask 的防重编号和默认来源跟着错 | `test_phase1_merge.CurrentBatchSkipsPriorityTest.test_ask_after_priority_batch_uses_the_original_batch`、`test_phase1_merge.CurrentBatchSkipsPriorityTest.test_backfill_skips_priority_batch_too` |
| 7.38 第 9 条 | 「待办」只认整句：「待办的那件事按 A 办」不是查清单 | `test_pending.PendingListTest.test_other_words_are_not_the_query` |
| 7.38 合并第三轮 | 原提问会话已收掉的提问由总线跟进：提醒发给总线，总线可以 resolve 它 | `test_pending.PendingListTest.test_closed_session_ask_is_followed_by_the_bus`、`test_pending.PendingListTest.test_bus_resolves_only_orphaned_asks` |
| 7.41 | 机器人事件先到时群名为空，批次消息头写成 `群「」`：入账前补群名（先用账本里记过的，没有就只读查一次并记下） | `test_identity.ChatNameTest.test_bot_event_first_then_search_batch_has_chat_name`、`test_identity.ChatNameTest.test_name_looked_up_once_read_only_and_remembered`、`test_identity.ChatNameTest.test_name_from_ledger_without_lookup` |
| 7.41 | 机器人身份查不到群名就退回主人身份查；都查不到照常入账投递，消息头写「群（名字没取到，oc_…）」，一小时内同一个群不再查 | `test_identity.ChatNameTest.test_bot_lookup_fails_falls_back_to_owner_identity`、`test_identity.ChatNameTest.test_name_unavailable_still_ingested_and_delivered_with_readable_header`、`test_identity.ChatNameTest.test_chat_label` |
| 7.41 | 第一期提示里的群名（核实提醒、待办来源）：最近一条群名为空时，仍用账本里记过的 | `test_phase1_merge.ChatLabelAfterMainMergeTest.test_group_name_from_an_earlier_message_when_the_latest_has_none` |
| 7.43 | 会话自报「等主人」，它等的提问（自动补的 + 同钥匙 ask 的）都关了：改成进行中，清单不再列（主人答 / 结案 / 过期三条路） | `test_escalation_work_close.EscalationWorkCloseTest.test_declared_before_close_stops_waiting_on_the_owner_on_each_path`、`test_escalation_work_close.EscalationWorkCloseTest.test_declared_first_linked_by_the_ask_key` |
| 7.43 | 不误放：关闭之后会话又报「等主人」的不动；同钥匙还有开着的提问，照旧等主人 | `test_escalation_work_close.EscalationWorkCloseTest.test_declared_after_close_is_left_alone`、`test_escalation_work_close.EscalationWorkCloseTest.test_another_open_ask_with_the_same_key_keeps_it_waiting` |
| 7.43 | 自动补的活随提问关闭收掉，结果写清哪条路（主人已答 / 会话已结案：依据 / 已过期） | `test_escalation_work_close.EscalationWorkCloseTest.test_auto_work_closes_on_each_path` |
| 7.45 | 群话题直达：别人在群话题里回复，按话题归属直达处理过开头那条的会话 | `test_group_threads.GroupThreadReplyTest.test_reply_in_thread_goes_to_session_that_got_the_root` |
| 7.45 | 话题 72 小时没动静就不再自动归属；名册显式绑定优先、不过期 | `test_group_threads.GroupThreadReplyTest.test_expired_after_72_hours`、`test_group_threads.GroupThreadReplyTest.test_auto_attribution_expired_when_root_is_old`、`test_group_threads.GroupThreadReplyTest.test_roster_thread_binding_overrides_and_never_expires` |
| 7.45 | 别的会话登记的等待不抢有归属的话题回复；话题回复被 forward 后话题改归接手的会话 | `test_group_threads.GroupThreadReplyTest.test_watch_of_other_session_does_not_steal`、`test_group_threads.GroupThreadReplyTest.test_forward_of_thread_reply_repoints_thread` |
| 7.45 | 主人在群里 @ 机器人 = 指令：话题 / 引用归属优先，其次名册负责会话，最后总线 | `test_group_threads.OwnerInstructionTest.test_in_thread_goes_to_thread_owner`、`test_group_threads.OwnerInstructionTest.test_quote_of_session_bot_reply_goes_to_that_session_as_instruction`、`test_group_threads.OwnerInstructionTest.test_roster_chat_then_bus` |
| 7.45 | 主人在群话题里没 @ 机器人的发言（背景）送话题归属的会话 | `test_group_threads.OwnerContextGroupTest.test_in_thread_goes_to_thread_owner_as_context` |
| 7.45 | 机器人事件到了就做一次小轮次（入账 + 投递，不跑搜索），不等满一个轮询间隔 | `test_group_threads.WakeLoopTest.test_event_triggers_mini_round_ingest_and_deliver_without_search` |
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
| 7.48 | 发给主人的几条路（ask / report、系统通知、结案）都发卡片，记账仍存纯文本 | `test_owner_card.FourPathsTest.test_ask_new_is_a_card_and_ledger_keeps_plain_text`、`test_owner_card.FourPathsTest.test_system_notice_is_a_card_and_notice_ledger_keeps_plain`、`test_owner_card.FourPathsTest.test_resolve_notice_is_a_card_with_optional_at` |
| 7.48 | 卡片第一行进 header，正文是 markdown 元素（模型写的 md 能渲染） | `test_owner_card.CardUnitTest.test_build_header_body_at` |
| 7.48 | 读回的卡片（`<card title=…>`）先还原成「标题\n正文」再认：登记「需要你定」、话题回复送回对的会话 | `test_owner_card.ReadBackTest.test_card_ask_registers_escalation_with_plain_text`、`test_owner_card.ReadBackTest.test_thread_reply_on_card_goes_back_with_plain_root` |
| 7.48 | 卡片太长改发纯文本；飞书拒收卡片，用同一个防重编号改发纯文本 | `test_owner_card.FallbackTest.test_too_long_falls_back_to_plain_text_same_key`、`test_owner_card.FallbackTest.test_card_rejected_resends_plain_with_same_key`、`test_owner_card.FallbackTest.test_notice_card_rejected_resends_plain` |
| 7.48 | 开关 [notify] owner_card 关掉，回到纯文本 | `test_owner_card.SwitchTest.test_off_sends_plain_text_like_before` |
| 7.48 | 测试环境（[threads] owner_chat_id）的系统通知发进测试群，不私聊主人 | `test_owner_card.TestEnvTest.test_notice_goes_to_test_group_not_owner_dm` |
| 7.48 | 「待办」摘录里标题和列表读回时粘成一串（「事情Sammy 要一条…」） | `test_pending.ExcerptJoinTest.test_joined_inline_heading_and_list_from_readback`、`test_pending.ExcerptJoinTest.test_list_uses_sent_text_not_glued_readback` |
| 7.48 | 「待办」测试拿「现在减几分钟」当来源时间，过了午夜就红 | `test_pending.OwnerPendingTest.test_source_on_previous_day_shows_month_day` |
| 7.51 | 带 --track 第一次用钥匙：在话题群开一件事项（标题卡做开头），ask 发进它的话题 | `test_topic_group.EntryTest.test_track_first_use_opens_item_with_title_card_and_ask_in_thread` |
| 7.51 | 主人在私聊话题里整句回「放群里」，这件事搬进话题群 | `test_topic_group.EntryTest.test_owner_move_from_dm_thread` |
| 7.51 | 话题群里主人回复：引用了哪条送哪个会话；没引用时抄送话题里每个有未答提问的会话；都没有就送负责会话 | `test_topic_group.GroupReplyTest.test_quote_goes_only_to_quoted_session`、`test_topic_group.GroupReplyTest.test_no_quote_cc_every_session_with_open_ask`、`test_topic_group.GroupReplyTest.test_no_open_ask_goes_to_owner_session` |
| 7.51 | 话题群在入口就分开、不走私聊那套：主人新开顶层话题当指令送总线，总线 forward 后交给负责会话 | `test_topic_group.EntryTest.test_owner_new_topic_goes_to_bus_then_forward_hands_off` |
| 7.51 | 话题群里别人说话不分派（提醒主人）；牧羊犬自己发的卡片即使长得像提问也不登记 | `test_topic_group.FallbackTest.test_stranger_joins_or_speaks`、`test_topic_group.ReadbackTest.test_own_group_posts_are_dropped_even_if_they_look_like_asks` |
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
| 7.55 | 闸门：全量测试连跑 3 次都绿才写记录；有红、第二次才红（抖动）都不写 | `test_release_gate.GateRunTest.test_green_writes_record_and_check_passes`、`test_release_gate.GateRunTest.test_red_lists_failures_and_writes_nothing`、`test_release_gate.GateRunTest.test_flaky_second_run_is_red` |
| 7.55 | 闸门核 REGRESSIONS.md：列的测试不在、被跳过，或一行既没测试也没写「缺」，都拦 | `test_release_gate.GateRunTest.test_regression_test_missing_is_red`、`test_release_gate.GateRunTest.test_skipped_regression_test_is_red`、`test_release_gate.GateRunTest.test_row_without_test_or_que_is_red_and_no_file_is_red` |
| 7.55 | 闸门在导出的提交里跑，不带工作树改动；HOME / XDG / SHEEPDOG_* / PYTHONPATH 都指到临时目录 | `test_release_gate.GateRunTest.test_exports_the_commit_not_the_worktree`、`test_release_gate.GateRunTest.test_tests_run_isolated_from_real_home_and_code` |
| 7.55 | 上线脚本第一步 --check：记录提交号对不上、次数不够、有没全绿的一次、记录坏了，都退出 1 | `test_release_gate.CheckTest.test_tampered_records_fail`、`test_release_gate.CheckTest.test_cli_exit_codes` |
| 7.56 | 「这个群该不该管」只在 Config.chat_scope 一处判：找主人那一侧先于 ignore | `test_chat_scope.ChatScopeTest.test_one_place_decides` |
| 7.56 | 测试群里主人 @ 机器人，被当成指令送进正式总线（当时测试群不在 ignore；这几条守「进了 ignore 就不当真」，外加不误伤的对照） | `test_chat_scope.OwnerAtBotInIgnoredGroupTest.test_regression_7_56_owner_at_bot_in_ignored_group_is_not_instruction`、`test_chat_scope.OwnerAtBotInIgnoredGroupTest.test_regression_7_56_owner_reply_in_ignored_topic_group_thread_is_not_instruction`、`test_chat_scope.OwnerAtBotInIgnoredGroupTest.test_regression_7_56_not_ignored_group_still_instruction` |
| 7.56 | 建了测试群忘了登记进 ignore，正式牧羊犬照样把里面的消息当真 | `test_chat_scope.DoctorTestGroupsTest.test_lists_unignored_test_groups` |
| 7.60 | 上线闸门只核清单里的测试还在，不核这次上的功能有没有登记：`--specs` 点名的编号没有登记、只写「缺」、登记的测试名写错，都要拦住 | `test_release_gate.SpecsCheckTest.test_regression_760_unregistered_spec_is_red`、`test_release_gate.SpecsCheckTest.test_regression_760_que_only_spec_is_red`、`test_release_gate.SpecsCheckTest.test_regression_760_misspelled_test_is_red`、`test_release_gate.SpecsCheckTest.test_regression_760_spec_number_matches_exactly`、`test_release_gate.SpecsCheckTest.test_regression_760_reads_the_commit_not_the_worktree`、`test_release_gate.SpecsCheckTest.test_regression_760_record_still_required`、`test_release_gate.SpecsCheckTest.test_regression_760_registered_specs_pass` |
