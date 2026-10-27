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
| 7.1 | 每批信号末尾附 batch_footer.md（代回提醒），丢了这行，长上下文里会话会忘掉代回标记（REG-001） | `test_roster.PlaybookTest.test_assembly_order_and_defaults`、`test_guardrails.ReminderTest.test_missing_means_nothing` |
| 7.1 | 配置的 session.reply_prefix 替换不进 {{reply_prefix}}，开场里写的代回前缀是空的 | `test_roster.PlaybookTest.test_dispatcher_uses_config_values` |
| 7.1 | 代回规则所在的 common.md 没拼进总线开场、登记通知、spawn 开场，会话不知道要代回 | `test_handover_watch.EscalationTest.test_common_rules_in_all_prompts` |
| 7.2 | spawn 先给前任发退休通知、再建接手会话，前任 topic 关掉不再投递；不发或顺序反了，新旧会话会同时对外说话 | `test_handover_watch.SpawnTest.test_retire_before_new_and_predecessor_closed` |
| 7.2 | spawn 只建一次、新 id 只记账本不回写名册；重复 spawn 或名册同步冲掉账本 id，会多建会话或丢掉接手会话 | `test_handover_watch.SpawnTest.test_spawn_once_and_no_roster_writeback` |
| 7.2 | 接手会话回执「读完前任」之前信号排队不丢；前任交接回执原样转给接手会话一次；开场带 successor.md（前任 id、transcript 路径） | `test_handover_watch.SpawnTest.test_successor_reads_first_then_gets_signals_and_handover` |
| 7.2 | sessions 不显示名册（留空）和账本（新会话）的差异、不显示前任，人看名册会以为会话还没建 | `test_regress_spawn.SpawnSessionsViewTest.test_regression_7_2_sessions_shows_roster_vs_ledger_and_predecessor` |
| 7.3 | 总线 forward 转交：目标是总线、known、不存在的会话要拒绝；转到 managed 会话的消息改归属、回到待推、带上总线备注 | `test_roster.ForwardTest.test_forward_rejects_known_bus_and_unknown`、`test_roster.ForwardTest.test_forward_delivers_to_managed_with_note` |
| 7.3 | 机器人发给主人的「找主人」消息（带 drop 前缀）被当成新信号推回总线，形成回环 | `test_handover_watch.EscalationTest.test_self_escalation_dropped` |
| 7.4 | 等待回复：对方的回复优先归给登记等待的会话（覆盖聊天归属、总线）；同一人多条等待取最新、按聊天限定 | `test_handover_watch.WatchTest.test_reply_routed_to_watcher_until_confirmed`、`test_handover_watch.WatchTest.test_chat_scoped_and_newest_wins` |
| 7.4 | 等待计时：15 / 30 分钟各提醒一次，60 分钟到期关闭并提示 | `test_handover_watch.WatchTest.test_nudges_once_each_then_expire` |
| 7.5 | 回执 anchors 里的 project: 原样存进会话锚点、名册同步不冲掉、sessions 显示「关联项目」；坏了，需求和项目就挂不上 | `test_handover_watch.ProjectAnchorTest.test_project_anchor_kept_and_shown` |
| 7.6 | playbook 占位符替换，未知占位符原样保留 | `test_roster.PlaybookTest.test_render_substitutes_and_keeps_unknown` |
| 7.6 | playbook 文件缺了，那一段为空，doctor 报缺哪个 | `test_roster.PlaybookTest.test_missing_files_render_empty`、`test_roster.PlaybookTest.test_doctor_reports_missing` |
| 7.6 | playbook 每次组装 prompt 时现读，改完下一批就生效，不用重启 | `test_roster.PlaybookTest.test_hot_reload` |
| 7.6 | 业务参数进配置：watch 提醒和到期的分钟数取 [watch]，防回环前缀取 routing.drop_bot_message_prefixes，不写死 | `test_handover_watch.WatchTest.test_configurable_minutes`、`test_handover_watch.EscalationTest.test_self_escalation_dropped` |
| 7.7 | 安全闸 hold：命中拦截规则的消息不投给名册会话、等待会话，改投总线 | `test_security.GateTest.test_hold_overrides_roster_and_all_messages`、`test_security.GateTest.test_hold_overrides_watch_and_keeps_waiting` |
| 7.7 | 安全闸 tag：被标记的消息照常投递，正文前带警示 | `test_security.GateTest.test_tag_delivered_with_banner_before_body` |
| 7.7 | 被 hold 的消息 forward 时要带核对过的主人原话，核不上就拒绝 | `test_security.GateTest.test_hold_forward_requires_verified_quote` |
| 7.8 | 找主人聊天：机器人发的「需要你定」登记、本身丢弃；别的机器人消息丢弃 | `test_escalation.EscalationTest.test_bot_question_registered_and_dropped`、`test_escalation.EscalationTest.test_other_messages_dropped` |
| 7.8 | 主人引用回复某条「需要你定」，送回提问的会话 | `test_escalation.EscalationTest.test_quoted_reply_goes_to_that_topic` |
| 7.8 | 主人本人的消息（含编辑后重新判定）不走安全闸 | `test_escalation.EscalationTest.test_owner_reply_edit_also_skips_security_gate` |
| 7.8 | 总线 forward 主人本人在别处的发言（本来没带已核对标记），送到时要补「✅ 已核对」标注 | `test_regress_routing.ForwardOwnerWordsRegressionTest.test_regression_7_8_forward_owner_background_gets_verified_label` |
| 7.9 | Get 表情在消息入账归属（通过安全闸）后当场点、只点配置的原因，投递时不重复点 | `test_ack.AckTest.test_only_configured_reasons_after_delivery`、`test_ack.AckTest.test_queued_not_acked_until_delivered` |
| 7.9 | 每条消息只点一次（编辑重投、超时重投都不再点） | `test_ack.AckTest.test_once_per_message` |
| 7.9 | 主人回复后撤表情：私聊撤同一聊天里更早的；群里只撤 reply_to 或 @ 了发送人的那条；at_all 不撤 | `test_ack.AckTest.test_p2p_own_message_removes_earlier_in_same_chat`、`test_ack.AckTest.test_group_reply_or_mention_removes`、`test_ack.AckTest.test_at_all_not_removed` |
| 7.9 | 点失败只记 error、不重试；dry-run 不调接口；没有 [ack] 段完全不动 | `test_ack.AckTest.test_failures_do_not_block_and_are_not_retried`、`test_ack.AckTest.test_dry_run_does_not_call`、`test_ack.AckTest.test_config_section_switch_and_cli` |
| 7.10 | 总线新开的会话：--chat 的聊天按归属直推给它；会话收掉后聊天归属释放回总线 | `test_dynamic.DynamicTest.test_chat_ownership`、`test_dynamic.DynamicTest.test_done_closes_and_releases` |
| 7.11 | 改了 playbook，下一批最前面带完整新规则、只带一次；没记过指纹的老会话也当成规则变了 | `test_rules_sync.RulesSyncTest.test_common_change_sent_once_in_front`、`test_rules_sync.RulesSyncTest.test_missing_hash_treated_as_changed` |
| 7.11 | 建会话、onboarding 时记下指纹，规则没改不重发 | `test_rules_sync.RulesSyncTest.test_hash_recorded_at_creation`、`test_rules_sync.RulesSyncTest.test_no_change_no_rules` |
| 7.11 | 总线同样同步，但名册变化不触发整份规则重发（走「名册更新」） | `test_rules_sync.RulesSyncTest.test_bus_too_but_not_on_roster_change` |
| 7.11 | push-rules 立即发、更新指纹；dry-run 不发不写；sessions 显示规则是否最新 | `test_rules_sync.RulesSyncTest.test_push_rules`、`test_rules_sync.RulesSyncTest.test_push_rules_cli_dry_run` |
| 7.11 | 总线新开（dynamic）和 spawn 新建的会话：建好时记指纹，改 playbook 后同样同步；坏了，这两类会话永远收不到新规则，或第一批多发一整份规则 | `test_regress_rules_sync.RulesSyncNewSessionsTest.test_regression_7_11_dynamic_session_hash_at_creation_and_gets_update`、`test_regress_rules_sync.RulesSyncNewSessionsTest.test_regression_7_11_spawned_session_hash_at_creation_and_gets_update` |
| 7.12 | 不在常驻进程里跑 spawn / new-session / push-rules / init / retire 只入队、不调 agentapi（会话里直接调会撞项目边界）；常驻进程直接执行；dry-run 在本地执行 | `test_actions.ActionsTest.test_non_resident_enqueues_without_agentapi`、`test_actions.ActionsTest.test_resident_runs_directly`、`test_actions.ActionsTest.test_dry_run_runs_locally` |
| 7.12 | run 每轮先执行排队的动作；单条失败记 error，不挡后面的 | `test_actions.ActionsTest.test_run_loop_executes_actions_before_poll`、`test_actions.ActionsTest.test_run_pending_in_order_failure_does_not_block` |
| 7.12 | retire 能给已关闭的前任或裸 conversation_id 补发退休通知，交接回执转给接手会话 | `test_actions.ActionsTest.test_retire_closed_predecessor_resends`、`test_actions.ActionsTest.test_retire_bare_conversation_id_of_unretired_predecessor` |
| 7.12 | 排队动作按提交顺序执行；顺序反了，依赖前一条的动作报「不存在」失败，而且不重试 | `test_regress_actions.ActionsOrderTest.test_regression_7_12_actions_run_in_submission_order` |
| 7.13 | 主人本人的发言当背景送负责会话：名册归属，其次 follow_hours 内最近处理过的会话，超时不送；只有背景时不要回执、不改状态 | `test_owner_context.OwnerContextTest.test_roster_owned_chat_goes_to_owner_as_context`、`test_owner_context.OwnerContextTest.test_follow_hours_recent_topic_or_nothing` |
| 7.13 | 会话代回、找主人聊天、ignore 聊天里主人的话不当背景送 | `test_owner_context.OwnerContextTest.test_skipped_cases` |
| 7.13 | 名册负责的聊天，主人先开口（这个聊天还没投递过消息）也要送负责会话 | `test_regress_routing.OwnerContextRosterRegressionTest.test_regression_7_13_owner_first_word_in_roster_chat_goes_to_owner` |
| 7.14 | 会话和 Agent 机器人来回代回，出现以下任一就会无限循环：窗口内代回超过阈值不熔断；冷却期间对方消息照样投进会话；冷却中重复触发、重复提示；冷却结束不恢复 | `test_loop_guard.LoopGuardTest.test_breaker`、`test_loop_guard.LoopGuardTest.test_loops_cli_and_clear` |
| 7.14 补丁 | 和真人同事来回聊也被熔断，真人的消息被拦进 Inbox（10-09 21:50）；混合聊天没按窗口内最近一条非主人消息的发送人判 | `test_loop_guard.LoopGuardTest.test_human_conversation_not_tripped`、`test_loop_guard.LoopGuardTest.test_mixed_chat_uses_latest_other_sender` |
| 7.14 | 机器人 / Agent 发的消息（按名字、id、机器人类型）认不出来，不打 agent_sender、不带标注 | `test_loop_guard.LoopGuardTest.test_agent_detection_and_label` |
| 7.15 | 主人引用了认不出来源的消息：送总线附原文，绝不当成回答唯一那条未结提问（10-09「方案1」事故） | `test_quote_reply.QuoteReplyTest.test_incident_unregistered_quote_goes_to_bus_not_open_question` |
| 7.15 | 主人引用会话发给他的普通消息（不是提问），送回那个会话 | `test_quote_reply.QuoteReplyTest.test_quote_of_session_normal_reply_goes_back` |
| 7.15 | 机器人消息的来源只认第一行；别名、标题唯一命中才算 | `test_quote_reply.QuoteReplyTest.test_attribution_only_first_line`、`test_quote_reply.QuoteReplyTest.test_aliases_and_titles_unique` |
| 7.15 | 有引用的消息投递时都附「↪ 回复的是」 | `test_quote_reply.QuoteReplyTest.test_quote_line_everywhere` |
| 7.16 第 1 条 | 分页没拉完照样推进水位线，后面的消息被静默丢掉；没拉完要从原 start 重拉，同一缺口只提示总线一次 | `test_gaps_collect.PartialTest.test_partial_round_keeps_watermark_and_refetches`、`test_gaps_collect.PartialTest.test_first_round_partial_without_watermark` |
| 7.16 第 1 条 | 飞书搜索翻到 max_pages 仍 has_more、或 has_more 却没 page_token，不报「没拉完」 | `test_gaps_collect.LarkPagingTest.test_more_pages_than_limit_is_partial`、`test_gaps_collect.LarkPagingTest.test_has_more_without_token_is_partial` |
| 7.16 第 2 条 | 发出去以后、记账之前崩溃，下一轮把同一批又投一遍；会话记录里找不到的批次没标 uncertain、自动重发了 | `test_gaps_delivery.DeliveryTest.test_crash_after_send_recovered_from_transcript_no_resend`、`test_gaps_delivery.DeliveryTest.test_not_in_transcript_uncertain_then_redeliver` |
| 7.16 第 5 条 | prune 只清 messages，已关的提问、回执文件这些永远不清；或者反过来把未结的、不确定送没送到的也清掉 | `test_gaps_misc.PruneTest.test_prune_all_tables_and_receipts` |
| 7.16 第 6 条 | 长消息固定只送前 40 行，截断了不提示，`sheepdog show` 看不到全文 | `test_gaps_misc.LongMessageTest.test_41st_line_now_delivered`、`test_gaps_misc.LongMessageTest.test_truncated_with_hint_and_show` |
| 7.16 第 7 条 | run 不每轮重读 config.toml（改关键词不生效）；改坏了不沿用上一份；sink 改了当场切换（应该只提示要重启） | `test_gaps_misc.HotReloadTest.test_run_reloads_config_each_round` |
| 7.16 第 3 条 | 对方随便回一句（寒暄）就关掉等待；等会话确认期间提醒照发 | `test_gaps_watch.WatchCandidateTest.test_chit_chat_does_not_close`、`test_gaps_watch.WatchCandidateTest.test_paused_while_replied_even_if_long_ago` |
| 7.16 第 4 条 | 免打扰群里回复主人的消息被丢（「回复我」要排在免打扰之前） | `test_gaps_collect.MutedReplyTest.test_reply_to_me_in_muted_chat_dispatched_vip_still_dropped` |
| 7.17 | reply-card 输出合法的 Card 2.0：正文原样、右下角灰色小字标记、中文和 emoji 不转义；宽度取配置；支持 --text-file 和标准输入；空正文报错 | `test_reply_card.ReplyCardCliTest.test_structure_and_defaults`、`test_reply_card.ReplyCardCliTest.test_config_width_and_suffix`、`test_reply_card.ReplyCardCliTest.test_text_file_stdin_and_empty` |
| 7.17 | agent_markers 认出卡片代回（任意位置包含）：主人发言背景不送回，防循环照样计数 | `test_reply_card.AgentMarkerTest.test_is_agent_reply`、`test_reply_card.AgentMarkerTest.test_owner_context_skips_card_reply_and_loop_counts_it` |
| 7.17 | 配置的 reply_suffix 替换不进 {{reply_suffix}}，会话被告知的代回标记和 reply-card 实际打的对不上 | `test_regress_reply_card.ReplySuffixFromConfigTest.test_regression_7_17_reply_suffix_from_config_reaches_prompts` |
| 7.18 | 宿主分派错：Codex / Claude 会话的批次走了 Antigravity 那条路（engine 不按 topic 的 host 取 sink，cli 不按配置建各宿主的 sink） | `test_hosts.EngineHostTest.test_batch_receipt_from_answer`、`test_hosts.CliTest.test_sinks_per_host` |
| 7.18 | 续接失效：已有会话每轮又新建一条线程或会话（Codex 没走 thread/resume，Claude 没走 --resume，新建那轮容量满重试时又新建一条） | `test_hosts.CodexSinkTest.test_resume_sequence`、`test_hosts.ClaudeSinkTest.test_new_and_resume_command_lines`、`test_hosts.CodexSinkTest.test_capacity_on_new_thread_retries_as_resume` |
| 7.18 | 可重试错误：容量满 / 5xx 不按 5、10、20 分钟重试就记失败（或者认不出这类错误）；重试用完不进 attention | `test_hosts.CodexSinkTest.test_capacity_retries_then_fails`、`test_hosts.ClaudeSinkTest.test_capacity_is_retried`、`test_hosts.EngineHostTest.test_capacity_exhausted_marks_attention` |
| 7.18 | 写锁冲突（Codex 应用开着线程）被当成失败计数，没有视同主人在用、退回排队；登录失效没把整个宿主标成不可用 | `test_hosts.EngineHostTest.test_write_lock_requeues_as_human_attached`、`test_hosts.CodexSinkTest.test_logged_out_marks_host_unavailable`、`test_hosts.EngineHostTest.test_login_failure_queues_and_notifies_once` |
| 7.18（19:0x） | 新开 Codex / Claude 会话不先问主人：不带 --quote、或原话核对不通过也照样开 | `test_hosts.OwnerConsentTest.test_without_quote_rejected`、`test_hosts.OwnerConsentTest.test_unverified_quote_rejected` |
| 7.18（20:05） | 配置档用 -p 传给 app-server，app-server 一启动就退出（10-10 20:05 日志采样会话连续 3 次失败）；应该展开成 -c | `test_hosts.CodexProfileTest.test_profile_expanded_to_c_only` |
| 7.19 K2 | 会话被压缩后牧羊犬不知道，下一批不重推完整规则；非压缩的 CHECKPOINT 被误认成压缩；Codex 的压缩点认不出 | `test_guardrails.CompactionTest.test_bus_checkpoint_next_batch_has_full_rules_once`、`test_guardrails.CompactionTest.test_non_compaction_checkpoint_ignored`、`test_guardrails.CompactionTest.test_codex_session_rollout` |
| 7.19 K1 | 每批末尾不附 reminders/<key>.md 的红线 | `test_guardrails.ReminderTest.test_appended_at_very_end` |
| 7.19 K3 | 总线跑了危险命令没人知道：不私聊主人、不给犯规会话排停手提示、不知会 also_notify；已知误报不过滤；同一 step 重复报 | `test_guardrails.AuditTest.test_bus_command_alerts_owner_offender_and_also_notify`、`test_guardrails.AuditTest.test_ignore_patterns_drop_known_false_positives`、`test_guardrails.AuditTest.test_dedup_by_step_and_safe_commands_ignored` |
| 7.19 K3 | Codex 会话跑的危险命令（CommandExecution、exec 里的 JS）认不出 | `test_guardrails.AuditTest.test_codex_command_execution_and_code_mode` |
| 7.19 报警修复 F3 | 给主人的报警遇到飞书 429 发不出去就丢了（10-12 13:28、21:15）；应该落账本、退避重试、只到一次；发出去后崩溃，重启后不另起一条 | `test_owner_alerts.F3OwnerNoticeLedger.test_rate_limited_then_backoff_retry_sends_exactly_once`、`test_owner_alerts.F3OwnerNoticeLedger.test_crash_after_send_before_marked_sent_is_not_resent_after_restart` |
| 7.19 报警修复 F4 | 10 分钟合并窗口把后来更严重的危险命令吞掉（10-12 21:17 发生产 BFF，没有单独报警、没有再排停手提示） | `test_owner_alerts.F4AuditMerge.test_incident_grep_then_trigger_and_approve_each_alert`、`test_owner_alerts.F4AuditMerge.test_pending_first_alert_never_holds_back_new_ones`、`test_guardrails.AuditTest.test_merge_only_repeats_of_reported_commands` |
| 7.19 报警修复 F5 | 压缩后要等下一批才重推规则，长批次里有空档（10-12 21:02）；手上有没交差的批次时应当场补发，同一压缩点只补一次 | `test_owner_alerts.F5RulesAfterCompaction.test_compaction_mid_batch_resends_rules_in_the_same_round`、`test_owner_alerts.F5RulesAfterCompaction.test_unfinished_batch_after_receipt_timeout_also_gets_rules`、`test_owner_alerts.F5RulesAfterCompaction.test_same_point_resent_once_even_after_restart_and_reread` |
| 7.20 | 运行状态算错：批次没人碰、或动过又停下 15 分钟不判中断；会话记录 30 分钟不动不判失联；主人在会话里说话不判被占着 | `test_duty.RunStateTest.test_stalled_when_batch_untouched_for_15_minutes_queues_and_notifies_once`、`test_duty.RunStateTest.test_lost_when_record_does_not_move_for_30_minutes`、`test_duty.RunStateTest.test_occupied_by_owner` |
| 7.20 | 判了中断 / 失联的会话照样投递；同一段卡住每轮都私聊主人 | `test_duty.RunStateTest.test_stalled_when_batch_untouched_for_15_minutes_queues_and_notifies_once`、`test_duty.NoRecordTest.test_untouched_batch_lost_after_lost_minutes_queues_and_notifies_once` |
| 7.20 第三轮 F1 | 找不到会话记录时整段卡住判定被跳过：卡片一直「在做」，不扣消息、不通知（黑盒 BUG-01） | `test_duty.NoRecordTest.test_untouched_batch_lost_after_lost_minutes_queues_and_notifies_once` |
| 7.20 第三轮 F2 | 「主人在说话」两处各算各的：卡片说被占着，投递还在照投（黑盒 BUG-02） | `test_duty.OwnerOneSourceTest.test_codex_owner_message_held_through_the_card`、`test_duty.MailTest.test_tell_queued_while_recipient_busy` |
| 7.20 | 撞车提醒不出现：消息里的编号在别的会话手上，或者最近送到过别的会话 | `test_duty.CollisionTest.test_reminder_attached_but_delivery_unchanged`、`test_duty_registration.ZeroRegistrationTest.test_routes_drive_collisions_and_board` |
| 7.20 | 总线摘要每批都附（应该只在在岗表变了时附） | `test_duty.DigestTest.test_digest_only_when_board_changes` |
| 7.20 | 会话留言 tell 送不到收件会话；同一对会话 10 分钟内超过 6 条不暂停、不通知总线 | `test_duty.MailTest.test_tell_delivered_next_batch_and_read_after_receipt`、`test_duty.MailTest.test_pair_rate_limit_pauses_and_notifies_bus` |
| 7.20 | 登记了 watch 没自动挂成「等别人」的活；同一条命令连跑不标空转；履历清理把还开着的活删了 | `test_duty.RunStateTest.test_waiting_from_watch_autofill_and_from_receipt`、`test_duty.RunStateTest.test_spinning_same_command_three_times`、`test_duty.PruneTest.test_history_and_closed_work_pruned_open_work_kept` |
| 7.20 第二轮 | 会话自报「等别人 / 等主人」把卡片改成了等回复，乱登记的一遍和不登记的一遍决定不一样 | `test_duty_registration.WrongRegistrationTest.test_declared_states_never_drive_run_state`、`test_duty_registration.TwinRunTest.test_decisions_identical` |
| 7.20 第二轮 | 卡住的会话自报「进行中」（或做完了 / 等别人 / 等主人）就照常投递（10-12 18:0x 架构侧证伪时只有长跑抓到） | `test_regress_duty_declared.DeclaredStuckTest.test_regression_7_20_stuck_session_declaring_doing_is_still_held`、`test_regress_duty_declared.DeclaredStuckTest.test_regression_7_20_stuck_session_any_declared_state_is_still_held` |
| 7.21 | 因回执超时卡在 attention 的会话恢复了（晚到的回执，或会话记录里有新动作），也不自动解开，排着的消息一直不投（10-11 断网，01:30 手动 reset） | `test_attention.RecoveryTest.test_late_receipt_recovers_and_delivers_queue`、`test_attention.RecoveryTest.test_transcript_activity_after_attention_recovers`、`test_attention.RecoveryTest.test_late_receipt_of_earlier_batch_also_counts` |
| 7.21 | 恢复时把晚到那一批里会话已经处理过的消息又重投，会造成重复代回 | `test_attention.RecoveryTest.test_late_receipt_recovers_and_delivers_queue`、`test_attention.RecoveryTest.test_late_receipt_of_earlier_batch_also_counts` |
| 7.21 | 会话进 attention 没人知道（总线自己进 attention 时，提示发给了它自己）；恢复了也不说 | `test_attention.NoticeTest.test_receipt_timeout_three_times_notifies_owner_once`、`test_attention.NoticeTest.test_bus_itself_in_attention_gets_no_self_hint`、`test_attention.RecoveryTest.test_transcript_activity_after_attention_recovers` |
| 7.21 | 不是回执超时进的 attention（如发送失败），被晚到的回执或新动作自动解开了（应该只认回执超时，其余要人 session-reset） | `test_regress_attention_reason.SendFailedAttentionTest.test_regression_7_21_send_failed_attention_not_unlocked_by_late_receipt`、`test_regress_attention_reason.SendFailedAttentionTest.test_regression_7_21_send_failed_attention_not_unlocked_by_new_activity` |
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
| 7.61 | 名册 `[[person]]`：合法的要能按 open_id 查到；未知键、open_id 重复或不是 ou_、tier 不认识、vip 没写 authority 或 authority 看不出日期，都要报错并说清是哪一条、哪个键 | `test_vip.RosterPersonTest.test_regression_761_valid_person_parsed_and_looked_up`、`test_vip.RosterPersonTest.test_regression_761_invalid_persons_rejected_with_where`、`test_vip.RosterPersonTest.test_regression_761_authority_date_forms`、`test_vip.RosterPersonTest.test_regression_761_example_roster_has_fake_vip` |
| 7.61 | 没有 `[[person]]` 的老名册升级后 content_hash 变了，误触发「名册更新」；有 person 时改了却不进 hash | `test_vip.RosterPersonTest.test_regression_761_old_roster_hash_unchanged`、`test_vip.RosterPersonTest.test_regression_761_person_enters_hash` |
| 7.61 | VIP 本人的消息投递时没盖章（私聊、群、插话批次、forward 转交过去的原消息），或章的位置不对（要在 @ 行、引用行之后，安全警示之前，安全警示还在） | `test_vip.StampTest.test_regression_761_p2p_and_group_stamped_after_mention_and_quote`、`test_vip.StampTest.test_regression_761_stamp_name_falls_back_to_sender_name`、`test_vip.StampTest.test_regression_761_priority_batch_stamped_and_rules_attached`、`test_vip.StampTest.test_regression_761_security_banner_kept_and_after_stamp`、`test_vip.DispatcherStampTest.test_regression_761_dispatcher_stamps_vip_in_managed_chat`、`test_vip.DispatcherStampTest.test_regression_761_forwarded_vip_message_stamped` |
| 7.61 | 主人自己、机器人 / Agent、sheepdog 自己生成的、正文里转述「某某让你…」的消息被误盖 VIP 章；名册没有 person 时也盖 | `test_vip.StampTest.test_regression_761_not_stamped_for_non_vip_owner_bot_relay_system`、`test_vip.DispatcherStampTest.test_regression_761_owner_listed_as_person_is_not_vip`、`test_vip.DispatcherStampTest.test_regression_761_no_person_roster_no_stamp` |
| 7.61 | vip.md 规则不跟批次走：有 VIP 消息的批次没附、附了多次、位置不对；没有 VIP 消息的批次多出字节；vip.md 进了常驻规则、触发全员规则重推 | `test_vip.StampTest.test_regression_761_rules_attached_once_after_footers_before_reminder`、`test_vip.StampTest.test_regression_761_no_vip_batch_byte_identical`、`test_vip.StampTest.test_regression_761_vip_md_from_playbook_overrides_default`、`test_vip.VipRulesNotStandingTest.test_regression_761_vip_md_optional_and_not_in_standing_rules`、`test_vip.VipRulesNotStandingTest.test_regression_761_example_vip_md_is_generic` |
| 7.61 | 为 VIP 请求 `sheepdog ask` 不带 `--floor`（或只写空白）也发出去了，又回来等主人批；被拒时发了消息或写了账本；带了 `--floor` 没记进账本；测试环境那条路没拦 | `test_vip.AskFloorTest.test_regression_761_ask_for_vip_batch_without_floor_refused`、`test_vip.AskFloorTest.test_regression_761_ask_about_vip_without_floor_refused`、`test_vip.AskFloorTest.test_regression_761_default_sources_cover_all_chats_in_batch`、`test_vip.AskFloorTest.test_regression_761_ask_with_floor_passes_and_floor_recorded`、`test_vip.AskFloorTest.test_regression_761_test_env_also_refused` |
| 7.61 | ask 的 VIP 拦截误伤：`--about` 只指非 VIP 消息、非 VIP 的批次、`report`、名册没有 person 时，都要和以前一样 | `test_vip.AskFloorTest.test_regression_761_about_non_vip_only_needs_no_floor`、`test_vip.AskFloorTest.test_regression_761_non_vip_batch_unaffected`、`test_vip.AskFloorTest.test_regression_761_report_unaffected`、`test_vip.AskFloorTest.test_regression_761_no_person_roster_behaves_as_before` |
| 7.61 | 旧账本没有 `sends.floor` 列，升级后 ask 记 floor 报错；重试改掉第一次记下的 floor | `test_vip.LedgerMigrationTest.test_regression_761_old_ledger_gets_floor_column` |
| 7.50 第 6 条 | 常规批次 sendMessage HTTP 200 但会话记录里核不到批次开头：当作已送达把会话锁在待回执、阻塞后续批次；支持 `--redeliver` / `forward --to` 手动恢复，且整批全被转交时立刻释放原会话 `pending_batch_id` | `test_standing_patrol.StandingPatrolTest.test_750_6_unverified_transcript_marks_uncertain_and_unblocks_topic`、`test_standing_patrol.StandingPatrolTest.test_750_6_redeliver_and_forward_recover_unverified_sent_batch`、`test_standing_patrol.StandingPatrolTest.test_750_6_forward_releases_source_pending_batch_when_all_messages_moved` |
| 7.57 | 常驻巡检（`patrol_every_minutes`）：空闲到点没入账巡检信号，或者会话正忙 / 还有未投递真实信号 / 不在窗口内也强塞巡检 | `test_standing_patrol.StandingPatrolTest.test_757_patrol_enqueues_when_idle_and_respects_interval`、`test_standing_patrol.StandingPatrolTest.test_757_patrol_skips_when_busy_or_outside_hours_or_pending_signals` |
| 7.58 | 动态会话生命周期与一人一会话：`standing`（或 `person-*` 推断）交 `status: done` 后被误关掉、释放聊天归属；`one-shot` 交 `done` 后没按原样关掉；`open-session` 恢复已关闭动态会话没复用原 conversation_id 或多占新建配额；总线名册表看不到已关闭动态会话 | `test_standing_patrol.StandingPatrolTest.test_758_standing_dynamic_session_survives_done_receipt`、`test_standing_patrol.StandingPatrolTest.test_758_person_prefix_infers_standing_lifecycle`、`test_standing_patrol.StandingPatrolTest.test_758_oneshot_dynamic_session_closes_on_done_receipt`、`test_standing_patrol.StandingPatrolTest.test_758_reopen_closed_dynamic_session_reuses_conversation_id_and_skips_quota`、`test_standing_patrol.StandingPatrolTest.test_758_bus_roster_table_lists_closed_dynamic_sessions_for_reopen` |
| 7.62 | 卡片外层直接堆长正文、没把原文原封不动收进默认折叠的 `more` 面板（配原生旋转下拉箭头图标），或把短消息也多余套上折叠面板 | `test_more_card.MoreCardTest.test_762_owner_ask_and_report_fold_with_summary_and_more_panel`、`test_more_card.MoreCardTest.test_762_reply_and_reply_card_fold_with_summary_and_inline_delimiter`、`test_more_card.MoreCardTest.test_762_short_message_passes_through_without_more_panel` |
| 7.62 | 长消息（>4 行、>160 字或含代码块）没给白话总结（或总结超 6 行、超 220 字、含代码块）未以退出码 2 拦下，或报错没明确提示「原文不需要改，只需要增加白话总结」导致模型删改原文 | `test_more_card.MoreCardTest.test_762_long_message_rejected_with_preserve_original_guidance` |
| 7.62 | 带 `more` 折叠面板的卡片读回后认不出「需要你定」，或「待办」清单摘录把 `more` 折叠面板里的长明细漏出来 | `test_more_card.MoreCardTest.test_762_readback_and_pending_excerpt_use_summary` |
| 7.62 | 代回原文里写的 `@人`（`<at id=ou_…>` / `<at user_id="ou_…">`）被一起折进 `more` 里面、外层白话总结看不见 `@人`，或 `<at>` 标签长度挤占 220 字额度 | `test_more_card.MoreCardTest.test_762_at_mentions_hoisted_outside_more_panel` |
| 7.62 | 代回卡片把 `more` 折叠面板套在两列 `column_set` 左列、右列放 `[🐕Sheepdog Reply]`，导致展开 `more` 后右侧被撑出约 1/3 宽度的空白列 | `test_more_card.MoreCardTest.test_762_reply_more_panel_is_top_level_full_width_not_in_column_set` |
| 7.63 | 注册 subagent 羔羊或母羊 transcript 触发 `invoke_subagent` 后没写入 `lambs` 表、`sheepdog board` 未在母羊卡片下缩进展示 `↳ 🐑` 或 `--json` 缺 `lambs` 列表 | `test_lamb.LambTest.test_763_lamb_register_and_board_display`、`test_lamb.LambTest.test_763_lamb_register_and_auto_discover_from_transcript` |
| 7.63 | 真实 Antigravity 3 步 transcript（`PLANNER_RESPONSE` JSON 字符串 `Subagents` 列表、`GENERIC` `Created the following subagents:` 回填 `conversationId`、`SYSTEM_MESSAGE` `[Message] ... sender=<cid>` 自动结案）解析漏掉子代理、未关联 `conversation_id` 或未自动标 `done` | `test_lamb.LambTest.test_763_real_antigravity_transcript_subagent_lifecycle` |
| 7.63 | 专属会话常驻规则 `interface_section(bus=False)` 未包含 `### 羔羊（重活后台并发与 30 秒卸锁）`、45 秒判别式、`sheepdog lamb spawn` 与 `lamb:<lamb_id>` 卸锁说明，或误注入给总线 `tp_bus` | `test_lamb.LambTest.test_763_lamb_interface_in_standing_rules` |
| 7.63 | `sheepdog lamb spawn` 后台 CLI 命令没生成 `lamb_id` 与日志文件、`lamb done` 没更新状态与摘要，或 `lamb list` 过滤运行中 / 全部羔羊不准 | `test_lamb.LambTest.test_763_lamb_spawn_cli_and_done_lifecycle` |
| 7.63 | 母羊交 `waiting` 回执带 `anchors: ["lamb:<id>"]` 后未当场释放 `pending_batch_id` 导致等待期间新消息阻塞、未在 `work_items` 登记 `lamb:<id>`，或羔羊完工后未向母羊投递 `lamb_report` 批次并关闭事项 | `test_lamb.LambTest.test_763_parent_waiting_receipt_releases_lock_and_lamb_done_reports_back`、`test_lamb.LambTest.test_763_parent_waiting_lamb_releases_batch_lock`、`test_lamb.LambTest.test_763_lamb_completion_delivers_report_batch` |
| 7.63 | 羔羊环境（`SHEEPDOG_LAMB_ID` 或 `lambs.conversation_id`）越权调用 `reply` / `reply-card` / `ask` / `report` / `forward` / `receipt` / `resolve` / `watch` 未以退出码 2 硬拦截 | `test_lamb.LambTest.test_763_lamb_permission_block_outbound_and_receipt`、`test_lamb.LambTest.test_763_lamb_outbound_commands_hard_blocked` |
| 7.63 | 母羊等待羔羊期间超 `stall_minutes` 无自身动作，活跃羔羊新鲜心跳未透传导致母羊被误判 `stalled` / `lost` 发失联报警，或羔羊跑危险命令未归属回母羊并注明由羔羊执行 | `test_lamb.LambTest.test_763_active_lamb_prevents_parent_stalled_alert`、`test_lamb.LambTest.test_763_lamb_heartbeat_prevents_parent_stalled` |
| 7.63 | `sheepdog reply --text` 短文本退回发送纯文本气泡而非统一发 `interactive` 卡片 | `test_lamb.LambTest.test_763_reply_text_always_builds_card` |
| 7.63 | `write_receipt` 抢先结清 `status="waiting"`（或 `lamb:` 锚点）批次导致 `owner_echoes.relay_batch` 未绑定、主人在飞书收不到回音或 `watches` 未结清；以及 `_echo_sync` 未对已 `acked` 却漏绑 `relay_batch` 的记录自愈 | `test_lamb.LambTest.test_763_waiting_receipt_binds_owner_echo_and_settles_watch_and_echo_sync_heals` |
| 7.63 | `lamb spawn` 未传 `stdin=DEVNULL` 导致子进程读 stdin 报 `Bad file descriptor`，或 CLI 羔羊子进程退出后 `_tick_lambs` 未自动收割标为 `done` / `failed` | `test_lamb.LambTest.test_763_cli_lamb_stdin_devnull_and_auto_reap_done_and_failed` |
| 7.63 | `invoke_subagent` 自动发现与手动 `lamb register --cid` 产生重复羔羊记录、`apply_receipt_work` 未把 `lamb:<cid>` 归一为 `lamb:<lamb_id>`，或子代理原生回信结案后 `work_items` 事项未关闭 | `test_lamb.LambTest.test_763_subagent_dedup_and_cid_anchor_closes_work_item`、`test_lamb.LambTest.test_763_real_transcript_edge_cases_singular_truncated_cron_and_kill` |
| 7.49 | 羊圈内回复链、引用回复或未结 `watch` 未按亲和性回到正在跟进该线程的成员，导致同一话题被拆到别的成员重复处理 | `test_session_group.SessionGroupTest.test_749_group_thread_affinity_never_splits` |
| 7.49 | 主会话（`tp_<key>`，`#1`）空闲（`active` 且无待处理消息）时新工作未优先进主会话，误分给副会话 | `test_session_group.SessionGroupTest.test_749_group_idle_primary_absorbs_new_work` |
| 7.49 | 主会话繁忙时无关新消息被主会话锁死排队，未路由给空闲副会话或自动按需扩出 `tp_<key>#2..#N`，或羊圈满额时未按最少待处理消息排队 | `test_session_group.SessionGroupTest.test_749_group_busy_primary_spawns_or_routes_to_secondary` |
| 7.49 | 羊圈存在活跃同侪时批次提示词前未注入 `## 🏠 羊圈同侪状态`（或超过 10 行上限），或看板未展示 `[羊圈: <group_id> · <N>会话]` | `test_session_group.SessionGroupTest.test_749_group_peer_status_injected_in_prompt` |
| 7.49 | 名册 `max_sessions` / `group` 默认值改变了老名册 `content_hash`，或 `[[group]]` 语法糖、副会话合成与 `known` 模式校验异常 | `test_session_group.SessionGroupTest.test_749_roster_group_syntax_and_content_hash_invariance` |
| 7.64 | 宿主重登/退出时 10 秒内连环触发 Stop Hook 把 2 次拦截额度烧光并停机，或正常达到拦截上限放行时删掉状态文件导致引擎不知道会话已停机 | `test_stop_hook.AutoResume764Test.test_regression_764_rapid_stop_under_10s_marks_interrupted_and_passes_without_burning_quota`、`test_stop_hook.AutoResume764Test.test_regression_764_max_blocks_pass_keeps_stop_state_with_passed_at_until_receipt` |
| 7.64 | 在途会话因重登/断连被 Stop Hook 放行停机后引擎干等 20 分钟才解锁，未在静止满 60 秒后自动发送断点续跑唤醒，或 `sheepdog session-reset` 拒绝将 `running` 会话复位为 `active` | `test_stop_hook.AutoResume764Test.test_regression_764_engine_auto_resumes_once_after_60s_and_unlocks_on_second_pass`、`test_stop_hook.AutoResume764Test.test_regression_764_session_reset_supports_running_to_active` |
| 7.65 | VIP 消息命中 `action=tag` 安全规则时仍渲染普通人「必须回对方等 Matthew 确认并私聊 Matthew」死命令，或 `vip.md` 未将合理生产操作、内部脱敏只读查询/导出及多选项直办并入直办并把底线收紧为 3 条 | `test_vip.VipBranch765Test.test_regression_765_vip_rules_include_prod_authorization_and_three_floors`、`test_vip.VipBranch765Test.test_regression_765_vip_with_security_tag_renders_merged_vip_banner_not_mandatory_ask`、`test_vip.VipBranch765Test.test_regression_765_jessie_desensitized_export_and_multi_option_prompt` |
| 7.65 | 会话编造非底线理由（如「涉及生产数据库访问权限与跨系统数据导出」「生产环境常规部署确认」「脱敏/只读」）或未同时带合法 `--floor-type`（`credentials` \| `destructive` \| `vip_conflict`）与非空 `--floor` 自由造句绕过 `check_vip_floor` 调用 `sheepdog ask` 挂起 VIP 请求，或带了合法 `--floor-type` 的正常自由造句被误伤拦截 | `test_vip.VipBranch765Test.test_regression_765_non_floor_excuses_rejected_with_direct_handle_and_report_hint`、`test_vip.VipBranch765Test.test_regression_765_floor_requires_valid_floor_type_and_allows_free_sentence` |
| 7.65 | 总线 `tp_bus` 把消息 `forward` 转交给专属会话后，又越俎代庖对该消息调用 `sheepdog ask` 或 `sheepdog reply`，或 `bus.md` 未收紧为纯分流路由（禁自己干业务/起草文档/建改飞书云文档/写记忆/用 `report --topic tp_bus` 锁死话题） | `test_vip.VipBranch765Test.test_regression_765_bus_cannot_ask_or_reply_for_forwarded_message`、`test_vip.VipBranch765Test.test_regression_765_bus_playbook_forbids_self_work_and_forwarded_reply` |
| 7.67 | 目标会话处于 `running` 长批次期间，新入账 `dispatch` 消息迟迟不点 `[Get]` 表情，或安全闸 `hold` / `inbox` 误点表情、投递时重复点第二次 | `test_ack.AckTest.test_767_ingest_acks_immediately_while_session_running_and_no_double_ack_on_delivery` |
| 7.67 | 主人在别人私聊/群聊里直接文字点名 `sheepdog` 被当成纯背景 `owner_context` 排队、未升格为 `owner_instruction` 走 `_dispatch_priority` 插话直达，或代码块/路径/代回前缀里的 `sheepdog` 被误升格、提示词仍渲染 `OWNER_CONTEXT_LABEL` | `test_owner_context.OwnerContextTest.test_767_owner_named_sheepdog_in_p2p_upgrades_to_instruction_and_jumps_running_queue` |
| 7.67 | 羊圈（`max_sessions > 1`）合成 `#2..#N` 副会话时继承了主槽位一次性 `predecessor_conversation_id` / `self_polling`，或同一发送人在窗口期内连发多条未投递消息被拆散到不同槽位 | `test_session_group.SessionGroupTest.test_767_laohu_group_max_sessions_spawns_clean_secondary_and_coalesces_burst` |
| 7.68 | 版本号双写漂移、`release_gate.py --specs 7.68` 未校验 `sheepdog.__version__ == "0.7.68"`、常驻进程未写 `daemon_runtime` 或本机/远端新版巡检漏报与重复轰炸提醒（REG-002） | `test_version_and_observe.VersionGateAndNoticeTest.test_release_gate_rejects_version_mismatch_and_daemon_notifies_once` |
| 7.68 | `sheepdog observe` 四阶段水流 SLA 漏报：入账超 90s 未点 `[Get]` 表情（Ingest Ack SLA）、主人指令/回复超 90s 未插话（Priority SLA）、普通消息排队超 5 分钟队头阻塞（Queue Head-of-Line SLA 含阻塞批次号）、主人说话回音超 15 分钟未结清（Owner Echo SLA）（REG-003） | `test_version_and_observe.ObserveWaterflowSlaTest.test_l2_ack_90s_priority_90s_and_hol_5m_sla_alerts` |
| 7.68 | 宿主 Transcript 深度探针漏检 `The prompt could not be submitted` 投递拦截、做完活忘交回执假 `running` 僵尸锁（>180s 且 DONE 静默 >90s），或 `--self-heal` 未写 `hooks/stop/<topic>.json` 驱动常驻进程 7.64 自动催交与解锁（REG-004） | `test_version_and_observe.ObserveTranscriptProbesTest.test_l3_prompt_filter_and_zombie_running_self_heal` |
| 7.68 | 总线 `tp_bus` 超 150s 或越权调用 `write_file`/`edit_file`/`gcloud`/`lark-cli docs` 未被 `tp_bus Boundary Guard` 揪出，或已正常答复结案的提问误触发 `ask_jumps` 噪音告警、持续故障因秒数跳动穿透指纹冷却（REG-005） | `test_version_and_observe.ObserveBusBoundaryAndNoiseTest.test_l3_bus_overreach_and_l4_ask_jumps_noise_razor` |
| 7.69 | 代回卡片右下角签名未升级为动态 `🐕SheepDog v0.7.69`，或跨版本升级后旧版 `[🐕Sheepdog Reply]` 与新版 `🐕SheepDog vX.Y.Z` 在 `is_agent_reply` / `_agent_reply_text` / `outbound` 发前检查 / `pending.is_agent_text` 中不能互认导致把代回卡片误判为主人发言（REG-006） | `test_version_and_observe.Version769SignatureAndUpgradeTest.test_reg_006_reply_card_version_signature_and_legacy_anti_echo` |
| 7.69 | 新版提醒卡片缺标题行或未引导主人回复「你来升级」/ 运行 `sheepdog upgrade`，或主人在飞书回复「你来升级 / 升级 / 重启生效」被当成普通消息送进 `tp_bus` 而未入队 `upgrade` 动作触发常驻进程原地 `os.execv` 热重启（REG-007） | `test_version_and_observe.Version769SignatureAndUpgradeTest.test_reg_007_version_notice_guidance_and_owner_upgrade_shortcut_execv` |
| 7.69 | `sheepdog run` 缺少 `sheepdog.run.lock` 内核级 `flock` 非阻塞排他锁导致双常驻进程脑裂，或 `Store.upsert_message` 并发写入同一 `message_id` 抛出 `sqlite3.IntegrityError: UNIQUE constraint failed: messages.message_id`（REG-008） | `test_version_and_observe.Version769SignatureAndUpgradeTest.test_reg_008_single_instance_flock_and_idempotent_upsert_message` |
| 7.70 | 代回卡片首屏（`reply` / `reply-card`）把全文技术细节浓缩成高密度黑话或超过 3 行 / 100 字，或首屏外露 6 类机器符号（Cloud Run revision 如 `00005-5vc`、Git commit SHA、URL/文件路径、飞书内部 ID `om_`/`oc_`/`ou_`、代码块/行内反引号、Markdown `1. ... 2. ...` 编号列表，含汉字无空格紧贴边界）未以退出码 2 拦截（REG-009） | `test_more_card.MoreCardTest.test_770_plain_first_screen_reply_limits_and_machine_symbol_guard` |
| 7.71 | 会话将可回退的 Cloud Run 环境变量/配置更新、切流或宿主 `pre-tool hook` 拦截借口伪造为 `--floor-type destructive` 绕过 `check_vip_floor` 调用 `sheepdog ask` 挂起 VIP 请求，或误拦真不可逆删除（REG-010） | `test_vip_771.Vip771UnblockTest.test_reg_010_reversible_prod_env_update_and_hook_excuse_rejected_as_destructive` |
| 7.71 | 含 VIP 消息的批次在未成功发起带合法底线 `sheepdog ask` 的情况下，会话直接提交 `status=needs_decision` 回执把会话锁进 `waiting_human`（REG-011） | `test_vip_771.Vip771UnblockTest.test_reg_011_vip_batch_needs_decision_receipt_without_floor_ask_rejected` |
| 7.71 | 母羊 transcript 中 `invoke_subagent` 紧随 `GENERIC` `tool call denied by pre-tool hook` 报错时，预建羔羊未被当场标为 `failed` 而是一直留在 `running` 阻塞超时（REG-012） | `test_vip_771.Vip771UnblockTest.test_reg_012_pre_tool_hook_denied_subagent_immediately_marked_failed` |
| 7.71 | `find_recent_thread_topic_in_group` 漏查 `running` 态导致同一发送人私聊紧接无引用跟进消息被拆给副会话 `#2` 脑裂，或同羊圈兄弟槽位无法按 `--key` 协同 `resolve` 结案（REG-013） | `test_vip_771.Vip771UnblockTest.test_reg_013_running_affinity_prevents_p2p_split_and_sibling_resolve_works` |
| 7.72 | 母羊直接把生产命令/密钥/URL 塞进 `invoke_subagent.Task` 触发宿主 Conseca `pre-tool hook` 拦截：未通过 `sheepdog lamb prepare` 将详细任务落盘至 `lamb_tasks/<lamb_id>.md` 并输出零敏感文件指针 `subagent_prompt` 与预登记 `running` 羔羊（REG-014） | `test_lamb_772.Lamb772Test.test_reg_014_lamb_prepare_writes_spec_file_and_emits_zero_sensitive_pointer_prompt` |
| 7.72 | 母羊单轮独自串行执行 $\ge 6$ 步业务工具且耗时 $\ge 90$ 秒、未派生任何羔羊卸锁时，`sheepdog receipt` 未记录 `monolithic_turn` 审计并输出并发守卫告警，或误伤已派羔羊/轻活批次（REG-015） | `test_lamb_772.Lamb772Test.test_reg_015_monolithic_parent_turn_without_lamb_triggers_receipt_guard` |
| 7.72 | 单人私聊羊圈 `running` 态被 30 分钟窗口无限吸附导致慢任务期间新消息无法溢出到 `#2`（缺少 `90s` 连发微窗口 vs `180s` 租约上限双时间窗解耦）、同羊圈缺少兄弟在跑羔羊与最近收发快照注入，或恢复 `max_sessions` 后 `sync_roster` 未重开 `closed` 副槽位并收敛已落盘回执（REG-016） | `test_session_group.SessionGroupTest.test_reg_016_p2p_dual_window_and_group_context_snapshot_and_reopen`、`test_lamb_772.Lamb772Test.test_reg_016_render_group_peer_status_renders_snapshot` |

## 7.73 飞书交互式卡片回调（ABCDE 按钮 + 其他输入表单、双层幂等与双锚点零错路由，REG-017 ~ REG-019）

| 规格 | 症状 | 守它的测试 |
| --- | --- | --- |
| 7.73 | `sheepdog ask` 决策卡片未自动提取或按 `--options` 注入 Card JSON 2.0 选项按钮（A~E `behaviors: [{type: "callback", value: {...}}]`）与根节点「其他：可输入」表单（`form` + `input` + `submit button`），或误把代码块内内容当选项、点击后未将卡片原地回写为绿头 `✅ 已拍板` 卡片（REG-017） | `test_card_callback_unit.CardCallbackUnitTest.test_reg_017_parse_ask_options_auto_and_explicit`、`test_card_callback_unit.CardCallbackUnitTest.test_reg_017_ask_card_renders_buttons_and_other_form_and_updates_card`、`test_card_callback_unit.CardCallbackUnitTest.test_reg_017_cli_ask_options_and_callback_unit_guards` |
| 7.73 | `BotEventConsumer` 未同时常驻消费 `im.message.receive_v1` 与 `card.action.trigger` 双事件流、卡片回调未落盘 `messages` 表（`sd_card_cb_<event_id>`）并触发 `patch_card` 与 `answer_escalation`，或同一 `event_id` WebSocket 重推 / 主人连点同一张卡片两次导致重复改卡、重复投递（REG-018） | `test_card_callback_e2e.CardCallbackE2ETest.test_reg_018_bot_events_consumes_card_action_and_persists_message`、`test_card_callback_e2e.CardCallbackE2ETest.test_reg_018_duplicate_click_and_replay_idempotent_single_dispatch`、`test_card_callback_unit.CardCallbackUnitTest.test_reg_018_and_019_other_form_and_closed_slot_revival` |
| 7.73 | 群内非主人点击审批卡未被拦截为 `card_callback_non_owner` `drop`，或多会话与羊圈副槽位（如 `tp_laohu#2`）并存时点击按钮 / 提交「其他」表单串线到别的会话或 `tp_bus`、`running` 态未触发 `priority` 插话秒级送达（REG-019） | `test_card_callback_e2e.CardCallbackE2ETest.test_reg_019_dual_anchor_routes_exact_session_and_rejects_non_owner`、`test_card_callback_unit.CardCallbackUnitTest.test_reg_018_and_019_other_form_and_closed_slot_revival` |

## 7.74 话题群因果防串线、精简胶囊卡片布局、未建档话题兜底锚定与回音防串线（REG-020 ~ REG-022）

| 规格 | 症状 | 守它的测试 |
| --- | --- | --- |
| 7.74 | 主人在话题群回复触发 `p...` 插队批次或投递到 `#N` 兄弟槽位后，会话回信换了新 `--key`（或未传 `--key`）时 `resolve_inbound_item` 未命中原 `T-n` 并追认 `alias_key` 导致回信脱群落入私聊；或 Card JSON 2.0 决策卡按钮未收进流式胶囊 `column_set`（`flex_mode="flow"`）、「其他」输入表单未合并同行提交按钮或仍残留 `max_length` 触发 `0/1000` 角标（REG-020） | `test_topic_group_causal_routing.TopicGroupCausalRoutingTest.test_priority_batch_and_changed_key_routes_back_to_item_and_binds_alias`、`test_topic_group_causal_routing.TopicGroupCausalRoutingTest.test_slot_family_and_no_key_and_done_reopen_via_causal_route`、`test_topic_group_order_e2e.TopicGroupOrderE2ETest.test_reg_020_compact_card_json_v2_layout_and_no_max_length` |
| 7.74 | 置顶索引卡 `patch_card` 限流/超限报错误调 `mark_down` 把整个话题群熔断、已有 `root_message_id` 的老事项发消息未绕过 `tg_down` 直连回复并自愈；或话题群卡片点击回调未关联 `item:T-n` 标签、私聊卡片缺一键「📌 转入长期跟踪」（`tg_move`）回调（REG-021） | `test_topic_group_causal_routing.TopicGroupBreakerDecoupleTest.test_index_card_patch_or_post_failure_never_marks_group_down`、`test_topic_group_causal_routing.TopicGroupBreakerDecoupleTest.test_existing_item_reply_bypasses_tg_down_and_clears_down_on_success`、`test_topic_group_order_e2e.TopicGroupOrderE2ETest.test_reg_021_topic_group_card_callback_routing_and_one_click_move` |
| 7.74 | 话题群内未建档顶层消息下主人回复时未自动兜底建档 `T-n` 产生悬空无主 thread；或 `Dispatcher._echo_sync` 被会话发往私聊的无关 `report` 误销账话题群 `owner_echo`；或话题首卡缺动态状态看板（`sync_title_card`）与 `sheepdog topic-group move` CLI 幂等迁群（REG-022） | `test_topic_group_causal_routing.TopicGroupTitleCardAndMoveCliTest.test_sync_title_card_updates_template_summary_and_alias_keys`、`test_topic_group_causal_routing.TopicGroupTitleCardAndMoveCliTest.test_cli_topic_group_move_is_idempotent`、`test_topic_group_order_e2e.TopicGroupOrderE2ETest.test_reg_022_unregistered_thread_auto_anchor_and_echo_sync_isolation` |

## 7.75 双轨 GitHub 版本分发（Release=Stable 稳定版 vs Tag=Beta 尝鲜版）、非交互远端探针与诚实双轨升级（REG-023 ~ REG-025）

| 规格 | 症状 | 守它的测试 |
| --- | --- | --- |
| 7.75 | 后台常驻进程 `_git_run` 未注入 `GIT_TERMINAL_PROMPT=0` 与 `GIT_SSH_COMMAND`（`BatchMode=yes`）导致无 SSH Agent 时卡死或失败，或 `origin` 鉴权失败后未自动 fallback 到公开免鉴权 HTTPS 源 `https://github.com/Matthewoho/sheepdog.git`；或附注标签 `refs/tags/vX.Y.Z^{}` 解引用 commit SHA 被丢弃、本机版本领先远端时误发降级提醒（REG-023） | `test_remote_version_watch_beta_stable.RemoteVersionWatchBetaStableTest.test_reg_023_non_interactive_git_fallback_and_peeled_tag_and_no_downgrade_alert` |
| 7.75 | 将每次同步的 Git Tag 与稳定版混为一谈导致每次打 Beta Tag（`tag > release`）都向别的 `sheepdog` 后台主动推送刷屏提醒，而非仅在发布新 GitHub Release（Stable）高于本机版本时发送单次非强制更新建议，或未将 `stable_tag` / `beta_tag` / `has_beta` 落盘 `meta["version_remote_latest"]`（REG-024） | `test_remote_version_watch_beta_stable.RemoteVersionWatchBetaStableTest.test_reg_024_stable_release_advisory_only_and_beta_silence_in_background` |
| 7.75 | 主人在飞书找主人私聊问「有新版本吗 / 有没有 beta / 查版本」（或 CLI `sheepdog version --check-remote`）未被拦截并实时返回本机、GitHub 稳定版（Release）与 Beta 尝鲜版（`tag > release`）及双轨非强制升级指引；或回复「升级稳定版 / 你来升级」与「升级 beta」未区分 `channel="stable" \| "beta"`，以及工作区 `dirty` 或 `git pull --ff-only` 失败时 `Dispatcher.upgrade` 谎报「✅ 当前代码已是最新」（REG-025） | `test_remote_version_watch_beta_stable.RemoteVersionWatchBetaStableTest.test_reg_025_owner_ask_beta_query_and_dual_channel_honest_upgrade` |

## 7.76 可点击交互式新版提醒与版本/Beta状态查询卡片（Card JSON 2.0 胶囊按钮、主人鉴权、双重防重与原地绿卡回写，REG-026）

| 规格 | 症状 | 守它的测试 |
| --- | --- | --- |
| 7.76 | `🐕 汇报 · 新版提醒`（远端稳定版提醒 / 本地待重启提醒）与 `🐕 汇报 · 版本与更新状态` 未经 `outbound.owner_content` 注入合法 Card JSON 2.0 流式胶囊按钮（`column_set` `flex_mode="flow"`，禁含 `vertical_spacing`），或点击 `[🚀 升级稳定版]` / `[🧪 升级 Beta 尝鲜版]` / `[🔄 立即重启生效]` / `[🔍 查有没有 Beta 版]` / `[⏸ 暂不升级]` 未在 `card_callback` + `Collector` 闭环入队 `upgrade`（区分 `channel="stable" \| "beta"` 与 `no_pull`）、未原地 `patch_card` 回写绿卡、非主人点击未被 `card_callback_non_owner` 拦截、连点两次或点卡后再发文字重复入队（REG-026） | `test_remote_version_watch_beta_stable.RemoteVersionWatchBetaStableTest.test_reg_026_interactive_version_notice_and_beta_query_card_callbacks` |

## 7.77 飞书原生常驻 WebSocket 守护线程、毫秒级同步 ACK+Toast 与主动心跳秒级自愈监管（REG-027 ~ REG-029）

| 规格 | 症状 | 守它的测试 |
| --- | --- | --- |
| 7.77 | 依赖外部 `lark-cli event _bus` 僵尸进程或处理完业务才回包导致飞书卡片点击报 `200672` 超时：未由进程内原生 WebSocket 在 `< 10ms` 内优先同步回写 pbbp2 Frame HTTP 200 + Toast 回执并异步归一化投递 v2 事件（REG-027） | `test_native_ws_supervisor.NativeWSSupervisorTest.test_reg_027_native_ws_sub10ms_ack_with_toast_and_v2_normalization` |
| 7.77 | 睡眠唤醒或 NAT 静默断网后 TCP 半开连接被永久挂死：未通过主动应用层 Ping/Pong 帧与静默看门狗在超时后秒级主动断线重连并刷新 endpoint（REG-028） | `test_native_ws_supervisor.NativeWSSupervisorTest.test_reg_028_half_open_zombie_detection_and_fast_reconnect` |
| 7.77 | `BotEventConsumer` 在 `native_ws` 模式下未清理孤儿 `lark-cli event _bus`、客户端线程异常死掉时 `poll()` 未自动拉起并同步 `identity_consumer` 状态，或 `sheepdog observe` 在 `native_ws` 在线时误报 `lark-cli event consume 没在跑`（REG-029） | `test_native_ws_supervisor.NativeWSSupervisorTest.test_reg_029_bot_event_consumer_native_supervision_and_observe_health` |

## 7.78 飞书原生 WebSocket 多 IP TLS 故障转移、坏节点冷却降权、断线零等待瞬时重连与合成消息表情查询过滤（REG-030）

| 规格 | 症状 | 守它的测试 |
| --- | --- | --- |
| 7.78 | 飞书边缘节点单 IP 出现 TCP 连通但 TLS 握手 EOF（`UNEXPECTED_EOF_WHILE_READING`）时在 DNS TTL 周期内反复撞同一坏首 IP、已建连长连接闪断后先睡 backoff 导致分钟级 `✖ 目标回调服务当前未在线` 空窗、心跳混发冗余 `0x9` 控制帧，或 `reactions_of` 将 `sd_card_cb_*` 合成卡片消息 ID 传给 `im reactions batch_query` 报 `99992354 Invalid ids`（REG-030） | `test_native_ws_supervisor.NativeWSSupervisorTest.test_reg_030_multi_ip_tls_failover_zero_gap_reconnect_and_card_reaction_filter` |

## 7.79 原生 SQLite 自学习花名册（people_roster）、多说话人身份反查与 Mattheuu 单一人格契约（REG-031 ~ REG-032）

| 规格 | 症状 | 守它的测试 |
| --- | --- | --- |
| 7.79 | 缺少原生 SQLite `people_roster` 双向花名册种子与 `sheepdog roster` CLI、主人语音/文字指令无法实时自学习落盘别名与展开私聊检索词、对主人汇报未把英文显示名（如 `Wenbo Wang`/`Mac`）归一为首选称呼或误伤 `Mac 桌面端` 平台词，以及 `[persona]` 单一人格契约（`Mattheuu`）与多说话人 `resolve_speaker_profile` 身份反查缺失（REG-031） | `test_people_roster_779.PeopleRoster779Test.test_reg_031_auto_seed_and_bidirectional_lookup_and_cli`、`test_people_roster_779.PeopleRoster779Test.test_reg_031_voice_natural_language_self_learning_and_query_expansion`、`test_people_roster_779.PeopleRoster779Test.test_reg_031_canonicalize_names_for_owner_and_mac_platform_guard`、`test_people_roster_779.PeopleRoster779Test.test_reg_031_persona_contract_and_multi_speaker_counterparty_profile` |
| 7.79 | `zekai`（泽楷 / Mac）与 `max_luo`（Max Luo）自然人实体混绑导致旧库 `zekai` 残留 `max`/`max luo`/`bymax` 脏别名与错绑 `open_id`，或 `canonicalize_names_for_owner` 把对主人汇报里的 `Max` 误替换成 `泽楷`（REG-032） | `test_people_roster_779.PeopleRoster779Test.test_reg_032_max_vs_mac_zekai_entity_isolation_and_seed_self_healing` |

## 7.80 飞书 Bot TAT 跨进程文件锁缓存、`_run_once` 限流退避与失效重取、话题群瞬态错误免熔断及索引卡时间去抖（REG-033）

| 规格 | 症状 | 守它的测试 |
| --- | --- | --- |
| 7.80 | `lark-cli --as bot` 短命子进程每次调用无状态重打 `POST /oauth/v3/token` 触发 `TAT endpoint rate limited (HTTP 429)` 风暴、`_run_once` 遇 429 零重试或遇 `99991663` 令牌失效未清缓存重取、`outbound._send_to_group` 与 `topicgroup.dispatcher_round` 将瞬态 429 当作群永久故障误触发 `tg.mark_down` 熔断退回私聊，或 `topicgroup._strip_time` 未剥离每分钟跳变的相对时间（`已等 X 分钟` / `最近动静 X前` / `拖了 X 小时`）导致每分钟无效 `patch_card`（REG-033） | `test_tat_rate_limit_780.TatRateLimit780Test.test_reg_033_tat_cache_reuses_token_and_injects_env`、`test_tat_rate_limit_780.TatRateLimit780Test.test_reg_033_lark_run_once_retries_429_and_refreshes_invalid_tat`、`test_tat_rate_limit_780.TatRateLimit780Test.test_reg_033_transient_429_does_not_mark_topic_group_down`、`test_tat_rate_limit_780.TatRateLimit780Test.test_reg_033_strip_time_ignores_relative_minute_ticks` |

## 7.81 独立自身可观测与告警群、report 球权契约与结案门禁、全局待拍板聚合台双视图同步与三阶温和催办（REG-034 ~ REG-037）

| 规格 | 症状 | 守它的测试 |
| --- | --- | --- |
| 7.81 | 看门狗、羊群看板中断/失联、attention、回音账本与版本提醒等系统通知发进主人私聊或主业务话题群造成噪音污染，或 `alert_chat_id` 为空/失效时未自动创建绑定并置顶欢迎卡的独立告警群 `🐕 牧羊犬 · 自身可观测与告警`（REG-034） | `test_decision_hub_and_alert_group_781.TestAlertGroupIsolation781.test_reg_034_auto_create_bind_pin_and_route_owner_notices`、`test_decision_hub_and_alert_group_781.TestAlertGroupIsolation781.test_reg_034_fallback_recreate_on_invalid_chat` |
| 7.81 | `sheepdog report` 允许传 `--ball owner` 或在正文末尾塞交互选项块/祈使回执口令把待拍板伪装成汇报，或普通词黑名单误杀含「授权/拍板/方案 A」的正常事实汇报；或 `sheepdog resolve` 在 `answered_at IS NULL` 且未提供可验真 `--quote` / `--superseded-by` 时允许会话自关未答提问（REG-035） | `test_decision_hub_and_alert_group_781.TestBallContractAndResolveGuard781.test_reg_035_report_allows_normal_prose_with_authorization_words`、`test_decision_hub_and_alert_group_781.TestBallContractAndResolveGuard781.test_reg_035_report_blocks_ball_owner_and_disguised_option_prompts`、`test_decision_hub_and_alert_group_781.TestBallContractAndResolveGuard781.test_reg_035_resolve_anti_self_close_guard` |
| 7.81 | 话题群未自动初始化置顶『📌 待我拍板 · 全局聚合台』根话题、新 `ask` 未双向镜像投射至聚合台或同一 `(topic_id, key)` 重复建楼刷屏而非原地双端 `PATCH`，以及在原业务话题或聚合台任一处点击按钮/文字回复后未原子结案并同步将双端卡片与 1F 汇总卡刷绿（REG-036） | `test_decision_hub_and_alert_group_781.TestDecisionHubDualViewAndKeyIsolation781.test_reg_036_decision_hub_auto_init_and_dual_view_mirror`、`test_decision_hub_and_alert_group_781.TestDecisionHubDualViewAndKeyIsolation781.test_reg_036_same_key_ask_idempotent_in_place_dual_patch`、`test_decision_hub_and_alert_group_781.TestDecisionHubDualViewAndKeyIsolation781.test_reg_036_button_click_in_either_view_settles_both_cards_atomic_cas`、`test_decision_hub_and_alert_group_781.TestDecisionHubDualViewAndKeyIsolation781.test_reg_036_text_reply_in_hub_routes_and_settles_both_cards` |
| 7.81 | 未答提问缺少三阶温和催办（`+30m`、`+2h`、次日 `10:00 CST`，上限 3 次）、同一阶梯重复刷屏催办，或 `00:00~09:00 CST` 夜间免打扰时段未静默顺延至 `10:00 CST`（REG-037） | `test_decision_hub_and_alert_group_781.TestEscalationNudgeLadder781.test_reg_037_three_tier_nudge_ladder_and_quiet_hours` |

## 7.82 Canonical 话题群一会话一主贴、会话级钥匙隔离与跨会话追问路标、waiting/话题群回执 0s 原帖带回、存量账本自愈与群顶层 reply Bot 优先发送（REG-038 ~ REG-042）

| 规格 | 症状 | 守它的测试 |
| --- | --- | --- |
| 7.82 | Canonical 话题群模式下业务会话调 `ask` / `report` 未传 `--track`（或未传 `--key`）退回机器人私聊 `owner_threads` 死锁，或单职责会话后续换用新 `--key` 时分裂出新话题 `T-(n+1)` 而未复用唯一 Canonical `T-n` 并自动追认别名钥匙（REG-038） | `test_canonical_topic_routing_782.TestCanonicalTopicRouting782.test_reg_038_canonical_session_topic_and_no_dm_scatter` |
| 7.82 | `find_by_key` 缺少会话边界导致不同业务会话（如 `tp_release` 与 `tp_wenbo-ops`）传同一发版单号 `--key` 时串入同一话题；或主人在 `T-A`（归属 `tp_alpha`）顺带问 `tp_beta` 的事并由 `tp_alpha` `forward` 转交时夺舍 `T-A.owner_topic`、未重绑到 `tp_beta` 专属话题 `T-B` 并在原帖发 `↪️ 这件事归属...已转入专属话题` 路标（REG-039） | `test_canonical_topic_routing_782.TestCanonicalTopicRouting782.test_reg_039_session_key_isolation_and_cross_domain_forward_signpost` |
| 7.82 | `"回执带回"` 被误放入 `SYSTEM_ALERT_KINDS` 导致剥离 `reply_to` 跑偏到独立告警群顶层，或主人在话题群发言后会话交 `status="waiting"` 回执（未发 `ask/report`）受 `owner_relay_after_minutes` 延迟阻塞/静默吞没而未在 `0s` 内原帖 `in_thread=True` 盖楼带回 `已派后台任务处理中`（REG-040） | `test_canonical_topic_routing_782.TestCanonicalTopicRouting782.test_reg_040_instant_in_thread_waiting_receipt_relay_not_hijacked_to_alert_group` |
| 7.82 | 存量账本中跨会话污染的 `tracked_keys`、单职责会话重复分裂的多个 `active` `tracked_items` 以及残留未关联的 `owner_threads` 私聊串缺少 `heal_canonical_topics` 自愈剥离、归并与收编（REG-041） | `test_canonical_topic_routing_782.TestCanonicalTopicRouting782.test_reg_041_heal_canonical_topics_merges_duplicates_and_cleans_cross_session_keys` |
| 7.82 | `sheepdog reply --to` 不支持 `oc_<chat_id>` 群顶层主动发卡片、未在机器人在群时强制优先用 `identity="bot"` 调 `post_to` 并记入 `sends`，或遇飞书 `230002`（机器人不在群）未作废缓存、记入 `bot_absent_chats` 并自动降级 `identity="user"` 重发，以及未拦截找主人侧聊天（REG-042） | `test_canonical_topic_routing_782.TestCanonicalTopicRouting782.test_reg_042_reply_to_group_chat_id_uses_bot_identity_first_and_falls_back_on_230002` |

## 7.83 机器人私聊追问自动路标引导与回执反死球真空硬门禁（REG-043 ~ REG-044）

| 规格 | 症状 | 守它的测试 |
| --- | --- | --- |
| 7.83 | 主人在机器人私聊（`oc_owner_dm`）发问并由 `tp_bus` 转交（或直接派发）给业务会话后，业务会话调 `ask` / `report` 自动路由进话题群专属话题 `T-n`，导致私聊原消息下静默无声、缺少 `↪️ 这件事已在话题群专属话题 T-n 回复你` 自动路标引导（REG-043） | `test_canonical_topic_routing_782.TestCanonicalTopicRouting782.test_reg_043_p2p_dm_question_gets_signpost_when_answer_routes_to_canonical_topic` |
| 7.83 | 会话在 `work_items` 残留 `waiting_on="主人"` / `state="waiting_owner"` 或提交 `status="needs_decision"` 但账本 `open_escalations=0`、`open_watches=0`、`running_lambs=0` 时未调 `sheepdog ask` 就交 `handled`/`waiting` 回执收工、`duty.set_work` 切回 `doing` 未自动清空残留 `waiting_on="主人"`，或受管会话在面板直聊回合结束（`fullyIdle=True`）最后一条 `PLANNER_RESPONSE` 写了非空 `- **等你定的事和回法...**` 却未同步调 `sheepdog ask` 落盘飞书决策卡造成死球真空（REG-044） | `test_canonical_topic_routing_782.TestCanonicalTopicRouting782.test_reg_044_receipt_rejects_dead_ball_vacuum_when_waiting_on_owner_without_open_ask` |

## 7.84 飞书 batch_query 空 reaction_id 自点 Get 防误认回声与总线 at_me 真实动作门禁（REG-045 ~ REG-046）

| 规格 | 症状 | 守它的测试 |
| --- | --- | --- |
| 7.84 | 飞书 `im reactions batch_query` 返回聚合表情时不带 `reaction_id`（`reaction_id=""`），导致 `Collector._check_owner_reactions` 将 sheepdog 自己以主人身份（`acks` 表 `identity='user'`）自动点的确认表情（`Get`）误判为主人新点的表情，循环生成虚假 `📝 主人对这条消息点了 Get`（`sd_owner_context_*`）系统消息并污染 `owner_reactions` 表（REG-045） | `test_anti_self_get_and_bus_at_me_784.TestAntiSelfGetAndBusAtMe784.test_reg_045_self_ack_get_with_empty_reaction_id_not_mistaken_for_owner_reaction`、`test_anti_self_get_and_bus_at_me_784.TestAntiSelfGetAndBusAtMe784.test_reg_045_purge_self_ack_owner_reactions_cleans_polluted_rows` |
| 7.84 | 总线 `tp_bus` 收到外部群聊点名 `@Matthew` 的真实待办/求确认消息（`reason='at_me'`）后，未执行 `forward` / `new-session` / `ask` / `report` / `reply` 真实路由动作（仅写 `noop` 或仅 `tell` 旁路会话）就交 `status="handled"` 回执将主人的待办静默吞没（REG-046） | `test_anti_self_get_and_bus_at_me_784.TestAntiSelfGetAndBusAtMe784.test_reg_046_bus_at_me_action_request_blocks_silent_noop_receipt` |

## 7.85 forward --answers 双端绿卡同步与非卡片已答提问点击回调原地自愈（REG-047 ~ REG-048）

| 规格 | 症状 | 守它的测试 |
| --- | --- | --- |
| 7.85 | `sheepdog forward --answers` 与超时关闭（`_expire_escalations`）跨通道关结「需要你定」时，未同步触发 `decision_hub.sync_escalation_dual_cards` 刷绿业务原卡、全局聚合台镜像卡并刷新 1F 汇总卡，或未给转交消息打上 `escalation:<mid>` 标签（REG-047） | `test_forward_answers_and_card_heal_785.TestForwardAnswersAndCardHeal785.test_reg_047_forward_answers_patches_dual_green_cards` |
| 7.85 | 当「需要你定」此前已通过非卡片回调路径（`prev_cb is None`，如 `forward --answers` 或 `resolve`）在 SQLite 标为 `answered_at/closed_at`、但主人仍在飞书橙卡上点击按钮或提交「其他」表单时，`process_card_action_event` 在防重 `drop` 返回前未调用 `decision_hub.sync_escalation_dual_cards` 原地自愈刷绿飞书双卡，导致「点提交没反应」的橙卡死锁（REG-048） | `test_forward_answers_and_card_heal_785.TestForwardAnswersAndCardHeal785.test_reg_048_card_callback_on_non_card_answered_escalation_self_heals_green_card` |

