-- 5140eda（7.29）时的账本结构：sheepdog.store.Store 在空文件上建出来的 sqlite_master，迁移测试用
CREATE TABLE messages (
    message_id     TEXT PRIMARY KEY,
    chat_id        TEXT NOT NULL,
    chat_name      TEXT,
    chat_type      TEXT,
    sender_id      TEXT,
    sender_name    TEXT,
    sender_type    TEXT,
    sender_tenant_key TEXT,
    content        TEXT,
    msg_type       TEXT,
    create_time    TEXT,
    mentions_json  TEXT,
    reply_to       TEXT,
    thread_id      TEXT,
    link           TEXT,
    deleted        INTEGER DEFAULT 0,
    updated        INTEGER DEFAULT 0,
    update_time    TEXT,
    is_read        INTEGER,            -- NULL = 未查询
    route          TEXT,               -- self | drop | inbox | dispatch
    reason         TEXT,
    tags_json      TEXT,
    topic_id       TEXT,
    dispatch_state TEXT,               -- pending | delivered | acked | failed | missed（adopted 回执超时）
                                       -- | uncertain（所在批次发送中断、不确定是否送达，不自动重发）
                                       -- | context（主人本人的发言作为背景已送达，不要回执）
    batch_id       TEXT,
    inbox_cleared  INTEGER DEFAULT 0,
    note           TEXT,               -- 未用（早期转交说明，转达改为 sheepdog 消息）
    security_tags  TEXT,               -- 安全规则命中的 rule name 列表（JSON）
    security_action TEXT,              -- none | tag | hold（取最严）
    message_position TEXT,             -- 消息在这个聊天里的序号（lark message_position，7.22 发前检查比先后用）；空 = 不知道
    thread_position TEXT,              -- 话题里的序号（lark thread_message_position，7.22）：普通聊天开头 "-1"、回复从 "0" 起；
                                       -- 话题群开头 "0"
    received_by    TEXT,               -- 哪个身份收到的（7.26，[identity] 开着才写）：user（主人身份的搜索）| bot（机器人身份的
                                       -- 实时接收）| both；关着时为空
    first_seen     TEXT NOT NULL,
    last_seen      TEXT NOT NULL
);
CREATE INDEX idx_msg_route ON messages(route, dispatch_state);
CREATE INDEX idx_msg_chat ON messages(chat_id, create_time);
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE topics (
    topic_id         TEXT PRIMARY KEY,
    title            TEXT NOT NULL,
    kind             TEXT,             -- bus | adopted（名册 managed）| known（名册 known，不投递）| retired（被接手的前任）
                                       -- | dynamic（总线临时新开，只在账本里，不写名册）
    duty             TEXT,             -- 一句话职责 + 边界，纠偏时修改
    conversation_id  TEXT,
    state            TEXT NOT NULL,
    anchors_json     TEXT DEFAULT '[]',
    summary          TEXT DEFAULT '',
    pending_batch_id TEXT,
    dispatched_at    TEXT,
    retries          INTEGER DEFAULT 0,
    onboarded_at     TEXT,             -- adopted 会话收到 onboarding 的时间，只发一次
    receipt_missed   INTEGER DEFAULT 0,  -- adopted 会话回执超时次数（不重投）
    spawned_at       TEXT,             -- 由 sheepdog spawn 新建的时间；名册 conversation_id 留空时以此为准
    origin_note      TEXT,             -- dynamic 会话的创建原因（new-session 的 --note）
    rules_hash       TEXT,             -- 上次送达的常驻规则指纹（7.11）；为空 = 没记录，下一批带上完整规则
    host             TEXT DEFAULT 'antigravity',  -- 会话宿主（7.18）：antigravity | codex | claude；建好后不能换
    model            TEXT,             -- 这个会话的模型；空 = 宿主默认值（每轮现取配置）
    effort           TEXT,             -- 这个会话的推理档位；空 = 宿主默认值
    compacted_at     TEXT,             -- 检测到宿主压缩的时间（7.19 K2）；下一批带上完整规则后清空
    attention_at     TEXT,             -- 进入 attention 的时间（7.21）；解除后清空
    attention_reason TEXT,             -- 进入 attention 的原因（机器可读）：receipt_timeout | no_receipt | send_failed
                                       -- | host_failed | host_timeout | config | capacity；只有 receipt_timeout 会自动恢复
    created_at       TEXT NOT NULL,
    last_active      TEXT NOT NULL
);
CREATE TABLE dispatches (
    batch_id    TEXT PRIMARY KEY,
    topic_id    TEXT NOT NULL,
    message_ids TEXT NOT NULL,
    sent_at     TEXT NOT NULL,
    state       TEXT NOT NULL,         -- sending（已记、正在发）| sent | acked | failed | missed | context
                                       -- | uncertain（发送中断、transcript 里找不到，等人工 redeliver）| redelivered
                                       -- | acked_late（判过回执超时以后回执才到，7.21；error 保留当时的超时原因）
    receipt     TEXT,
    error       TEXT
);
CREATE TABLE watches (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id     TEXT NOT NULL,
    person_id    TEXT NOT NULL,
    chat_id      TEXT,                 -- 空 = 对方在任何聊天里回复都算
    note         TEXT,
    started_at   TEXT NOT NULL,
    nudged_15_at TEXT,
    nudged_30_at TEXT,
    closed_at    TEXT,
    close_reason TEXT,                 -- done（会话确认等到了）| expired | cancelled | topic_closed（旧数据可能是 replied）
    status       TEXT,                 -- waiting（空同）| replied（对方回复了、等会话确认，提醒暂停）
    last_reply_at TEXT,                -- 对方最近一次回复的时间；回到 waiting 后从这里重新计时
    last_reply_message_id TEXT
);
CREATE INDEX idx_watch_person ON watches(person_id, closed_at);
CREATE TABLE escalations (
    message_id        TEXT PRIMARY KEY,  -- 机器人那条提问
    topic_id          TEXT NOT NULL,
    chat_id           TEXT,
    text              TEXT,
    asked_at          TEXT NOT NULL,
    answered_at       TEXT,
    answer_message_id TEXT,
    closed_at         TEXT,              -- 已答或超过 open_hours 时写入；为空 = 未结
    close_reason      TEXT               -- answered | expired
);
CREATE TABLE bot_outbox (
    message_id  TEXT PRIMARY KEY,
    topic_id    TEXT NOT NULL,
    is_question INTEGER DEFAULT 0,     -- 同时匹配 header_regex（问题仍照旧进 escalations）
    text        TEXT,
    sent_at     TEXT
);
CREATE TABLE acks (
    message_id   TEXT PRIMARY KEY,
    chat_id      TEXT,
    chat_type    TEXT,
    sender_id    TEXT,
    reason       TEXT,
    identity     TEXT,                 -- user（以主人身份点，回复后撤）| bot（以机器人身份点，永不撤）
    reaction_id  TEXT,                 -- 点失败时为空
    added_at     TEXT,
    removed_at   TEXT,
    error        TEXT                  -- 点或撤失败的原因
);
CREATE INDEX idx_ack_chat ON acks(chat_id, removed_at);
CREATE TABLE owner_reactions (
    message_id   TEXT NOT NULL,
    emoji_type   TEXT NOT NULL,
    operator_id  TEXT NOT NULL,
    reaction_id  TEXT,
    seen_at      TEXT NOT NULL,
    reported     INTEGER DEFAULT 0,    -- 0 = 首次检查时的基线，只记不报；1 = 已报给会话
    PRIMARY KEY (message_id, emoji_type, operator_id)
);
CREATE TABLE loop_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id      TEXT NOT NULL,
    chat_name    TEXT,
    triggered_at TEXT NOT NULL,
    replies      INTEGER,              -- 触发时 window 内的会话代回次数
    until        TEXT NOT NULL,
    cleared_at   TEXT                  -- 手动解除时间
);
CREATE INDEX idx_loop_chat ON loop_events(chat_id, until);
CREATE TABLE actions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,        -- init | spawn | new_session | push_rules | retire
    args_json    TEXT NOT NULL,
    requested_by TEXT,
    requested_at TEXT NOT NULL,
    done_at      TEXT,                 -- 执行完（成功或失败）；为空 = 排队中
    result       TEXT,
    error        TEXT
);
CREATE TABLE runs (
    batch_id        TEXT PRIMARY KEY,
    topic_id        TEXT NOT NULL,
    host            TEXT NOT NULL,
    conversation_id TEXT,
    pid             INTEGER,
    cmd_fingerprint TEXT,              -- 启动命令行（不含可执行文件本身）；重启后用 ps 核对 pid 还是不是这个进程
    started_at      TEXT,
    ended_at        TEXT,
    state           TEXT NOT NULL,     -- running | done | failed | timeout | locked | retry_wait
    attempts        INTEGER DEFAULT 0,
    error           TEXT,
    error_kind      TEXT,              -- capacity | locked | auth | other | timeout
    kind            TEXT,              -- new（新建会话）| resume（续接）
    title           TEXT,
    model           TEXT,
    effort          TEXT,
    retry_at        TEXT,              -- retry_wait 时下一次重试的时间
    denials         TEXT,              -- 宿主拒绝的工具 / 审批请求（JSON 列表）
    answer          TEXT               -- 这一轮最后的回答
);
CREATE INDEX idx_runs_state ON runs(host, state);
CREATE TABLE transcript_cursors (
    topic_id        TEXT NOT NULL,
    purpose         TEXT NOT NULL,
    conversation_id TEXT,
    path            TEXT,
    byte_offset     INTEGER DEFAULT 0,
    updated_at      TEXT,
    PRIMARY KEY (topic_id, purpose)
);
CREATE TABLE audit_hits (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id        TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    step            TEXT NOT NULL,
    command         TEXT NOT NULL,     -- 已脱敏、已截断（整理后的命令文本：判「同一条命令」按它比）
    pattern         TEXT,
    ran_at          TEXT,
    detected_at     TEXT NOT NULL,
    notified_at     TEXT,              -- 处理的时间：报了，或者并进了之前的报警
    alert_id        INTEGER,           -- 属于哪一次报警（那次报警里第一条命中的 id）
    merged          INTEGER DEFAULT 0, -- 1 = 10 分钟内报过同一条命令，并进那次报警、没有单独报
    UNIQUE (conversation_id, step)
);
CREATE TABLE owner_notices (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,         -- 通知类型：危险命令报警 / 羊群看板通知 / attention 通知 / 恢复通知 / 投递失败通知 …
    topic_id    TEXT,                  -- 说的是哪个会话（登录提醒说的是宿主，为空）
    body        BLOB NOT NULL,         -- 正文原样字节（UTF-8，命令名已换好）：每次重试发同一份，不重拼
    idem_key    TEXT NOT NULL UNIQUE,  -- 防重编号（lark-cli --idempotency-key，≤ 50 字符）：落账本时定好，每次重试都用它
    state       TEXT NOT NULL,         -- pending（待发）| sent（已发）| abandoned（放弃）
    attempts    INTEGER DEFAULT 0,     -- 从 window_at 起试了几次（人工 --retry 清零）
    last_error  TEXT,
    created_at  TEXT NOT NULL,
    window_at   TEXT NOT NULL,         -- 2 小时放弃计时的起点：落账本的时间；人工 --retry 以后从那一刻重新算
    attempt_at  TEXT,                  -- 最近一次开始发送的时间
    inflight_at TEXT,                  -- 发之前写、有结果后清空；重启后还在 = 上次发送中断，不知道发出去没有
    next_at     TEXT,                  -- 待发：下一次重试不早于这个时间
    sent_at     TEXT,
    closed_at   TEXT,                  -- 标放弃的时间
    acked_at    TEXT                   -- 人工确认已处理（sheepdog notices --ack）：不再提示、不再重发
);
CREATE INDEX idx_owner_notices_state ON owner_notices(state);
CREATE TABLE duty_cards (
    topic_id     TEXT PRIMARY KEY,
    run_state    TEXT,                 -- occupied | lost | stalled | working | waiting | idle
    spinning     INTEGER DEFAULT 0,    -- 同一条命令 / 工具调用连续 spin_repeat 次以上
    detail       TEXT,                 -- 判定依据（一句话）
    action       TEXT,                 -- 最近一次工具调用的动作说明（截一句；不读模型的思考）。在做时叫此刻动作，其他状态叫最近动作
    action_at    TEXT,                 -- 这次动作的时间
    last_act_at  TEXT,                 -- 会话记录里最近一次动作（含没有工具调用的回答）
    last_sig     TEXT,                 -- 空转判定：最近一次工具调用的签名
    sig_repeat   INTEGER DEFAULT 0,
    human_at     TEXT,                 -- 主人最近一次在会话里亲自说话（Antigravity 的 USER_INPUT；Codex / Claude 按宿主的读法）。投递要不要暂停也只看它
    compacted_at TEXT,                 -- 最近一次压缩点
    queued       INTEGER DEFAULT 0,    -- 排队的信号数
    stuck_since  TEXT,                 -- 中断 / 失联从几点算起
    idle_since   TEXT,                 -- 这一段空闲从几点算起（不空闲时为空）：自报的活「可能过期」要看它
    no_record    INTEGER DEFAULT 0,    -- 找不到会话记录（Antigravity / Codex 有会话编号却没有记录文件）：最近动作写「（找不到会话记录）」
    updated_at   TEXT
);
CREATE TABLE work_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id    TEXT NOT NULL,
    key         TEXT NOT NULL,
    norm_key    TEXT NOT NULL,         -- 比较用：去空白、转大写
    title       TEXT,
    state       TEXT NOT NULL,         -- doing | waiting_other | waiting_owner | done | handed_off
    waiting_on  TEXT,
    until       TEXT,
    expect      TEXT,                  -- 答应交出什么（一句话）
    handed_to   TEXT,                  -- 转交给哪个会话
    result      TEXT,
    source      TEXT,                  -- declared（会话申报）| watch | escalation | forward
    source_ref  TEXT,                  -- watch:N / escalation:<message_id> / forward:<message_id>
    declared    INTEGER DEFAULT 0,     -- 会话自己申报过：自动补活只补缺的字段
    declared_at TEXT,                  -- 会话最后一次自己申报 / 更新它的时间（自动补字段不算）
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    closed_at   TEXT,
    UNIQUE (topic_id, norm_key)
);
CREATE INDEX idx_work_key ON work_items(norm_key, closed_at);
CREATE TABLE mailbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    from_topic  TEXT NOT NULL,
    to_topic    TEXT NOT NULL,
    key         TEXT,
    body        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    message_id  TEXT
);
CREATE INDEX idx_mail_pair ON mailbox(from_topic, to_topic, created_at);
CREATE TABLE duty_history (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id  TEXT NOT NULL,
    kind      TEXT NOT NULL,           -- receipt | work | action
    text      TEXT NOT NULL,
    at        TEXT NOT NULL
);
CREATE INDEX idx_history_topic ON duty_history(topic_id, at);
CREATE TABLE duty_routes (
    topic_id        TEXT NOT NULL,
    norm_key        TEXT NOT NULL,
    key             TEXT NOT NULL,
    title           TEXT,              -- 最近一条带这个编号的消息（截一句）
    first_at        TEXT NOT NULL,
    last_at         TEXT NOT NULL,     -- 最近一次送到
    count           INTEGER DEFAULT 0, -- 送到过几条（同一条重投不重复算）
    last_message_id TEXT,
    closed_at       TEXT,
    closed_reason   TEXT,
    PRIMARY KEY (topic_id, norm_key)
);
CREATE INDEX idx_routes_key ON duty_routes(norm_key, last_at);
CREATE TABLE duty_route_seen (
    topic_id   TEXT NOT NULL,
    norm_key   TEXT NOT NULL,
    message_id TEXT NOT NULL,
    at         TEXT NOT NULL,
    PRIMARY KEY (topic_id, norm_key, message_id)
);
CREATE TABLE owner_threads (
    root_message_id TEXT PRIMARY KEY,  -- 话题开头那条
    thread_id       TEXT,              -- omt_…；发出时不知道（--reply-in-thread 的返回里没有），发完 mget 开头那条、
                                       -- 或拉到话题里的消息以后补上
    topic_id        TEXT NOT NULL,
    key             TEXT,              -- 钥匙原样；主人在老消息下开的话题（靠开头那条认出来的）为空
    norm_key        TEXT,              -- duty.norm_key(key)；为空 = 没有钥匙
    title           TEXT,              -- 开头那条的短标题
    source          TEXT NOT NULL,     -- ask（ask / report 带 --key 发出的）| recognized（主人在老消息下开话题，靠开头那条认出来补记的）
    created_at      TEXT NOT NULL,
    last_post_at    TEXT               -- 最近一次往这个话题里发（保留期清理按它）
);
CREATE UNIQUE INDEX idx_owner_threads_key ON owner_threads(topic_id, norm_key) WHERE norm_key IS NOT NULL;
CREATE INDEX idx_owner_threads_tid ON owner_threads(thread_id);
CREATE TABLE sends (
    idem_key          TEXT PRIMARY KEY,
    kind              TEXT NOT NULL,   -- ask | report | reply
    topic_id          TEXT NOT NULL,
    message_id        TEXT NOT NULL,   -- 飞书返回的消息号
    root_message_id   TEXT,            -- ask / report 带钥匙时：在哪个话题里（开头那条的消息号；新起的就是它自己）
    key               TEXT,
    norm_key          TEXT,
    target_message_id TEXT,            -- reply：回复的是哪条
    text              TEXT,            -- 发出去的正文（卡片记卡片正文）
    sent_at           TEXT NOT NULL,
    identity          TEXT             -- 用哪个身份发的（7.26，[identity] 开着才写）：user（主人本人）| bot（机器人）
);
CREATE INDEX idx_sends_message ON sends(message_id);
CREATE INDEX idx_sends_root ON sends(root_message_id);
CREATE TABLE seen_before_send (
    topic_id          TEXT NOT NULL,
    message_id        TEXT NOT NULL,   -- 主人的那条发言
    target_message_id TEXT,            -- 当时会话要回复的那条
    seen_at           TEXT NOT NULL,
    PRIMARY KEY (topic_id, message_id)
);
CREATE TABLE bot_dm_replies (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_id        TEXT NOT NULL,     -- 私聊机器人的那个人（open_id）
    chat_id          TEXT,
    message_id       TEXT,              -- 他发给机器人的那条
    reply_message_id TEXT,              -- 机器人回的那条
    replied_at       TEXT NOT NULL
);
CREATE INDEX idx_bot_dm_sender ON bot_dm_replies(sender_id, replied_at);
CREATE TABLE bot_absent_chats (
    chat_id     TEXT PRIMARY KEY,
    chat_name   TEXT,
    fallbacks   INTEGER DEFAULT 0,      -- 退回了几次
    last_reason TEXT,                   -- not_in_chat（群列表里没有）| 230002（发送时飞书报不在群里）| list_failed（群列表查不到）
    first_at    TEXT NOT NULL,
    last_at     TEXT NOT NULL
);
