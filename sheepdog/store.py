"""SQLite 账本：消息、元数据（水位线等）、话题/session 注册表、投递记录。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator

from . import media
from .models import Mention, Message

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
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
    thread_position TEXT,              -- 话题里的序号（lark thread_message_position，7.22）：开头那条 "-1"、回复从 "0" 起
                                       -- （7.45 用正式账本核实：普通群、话题群都是这样；7.22 说的「话题群开头 0」不对）
    received_by    TEXT,               -- 哪个身份收到的（7.26，[identity] 开着才写）：user（主人身份的搜索）| bot（机器人身份的
                                       -- 实时接收）| both；关着时为空
    root_id        TEXT,               -- 话题 / 回复链开头那条（7.45）：只有机器人实时事件带（root_id），搜索、mget 没有；空 = 不知道
    first_seen     TEXT NOT NULL,
    last_seen      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_route ON messages(route, dispatch_state);
CREATE INDEX IF NOT EXISTS idx_msg_chat ON messages(chat_id, create_time);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS topics (
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
    lifecycle        TEXT,             -- dynamic 会话生命周期（7.58）：standing（常设）| oneshot（一次性）；空 = 按标题推断
    group_id         TEXT NOT NULL DEFAULT '', -- 会话组（羊圈，7.49）：同组会话共享聊天归属与同侪看板
    created_at       TEXT NOT NULL,
    last_active      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dispatches (
    batch_id    TEXT PRIMARY KEY,
    topic_id    TEXT NOT NULL,
    message_ids TEXT NOT NULL,
    sent_at     TEXT NOT NULL,
    state       TEXT NOT NULL,         -- sending（已记、正在发）| sent | acked | failed | missed | context
                                       -- | uncertain（发送中断、transcript 里找不到，等人工 redeliver）| redelivered
                                       -- | acked_late（判过回执超时以后回执才到，7.21；error 保留当时的超时原因）
                                       -- | priority（主人插话批次，7.37：送到即终态，不要回执、不算超时）
    receipt     TEXT,
    error       TEXT
);

-- 等别人回复（7.4）：对方回复优先归给登记的 topic；15/30 分钟提醒，60 分钟到期
CREATE TABLE IF NOT EXISTS watches (
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
CREATE INDEX IF NOT EXISTS idx_watch_person ON watches(person_id, closed_at);

-- 「需要你定」（7.8）：会话以 bot 身份找主人的提问；主人在 IM 上的回复按它投回对应 topic
CREATE TABLE IF NOT EXISTS escalations (
    message_id        TEXT PRIMARY KEY,  -- 机器人那条提问
    topic_id          TEXT NOT NULL,
    chat_id           TEXT,
    text              TEXT,
    asked_at          TEXT NOT NULL,
    answered_at       TEXT,
    answer_message_id TEXT,
    closed_at         TEXT,              -- 已答、会话结案或超过 open_hours 时写入；为空 = 未结
    close_reason      TEXT,              -- answered | expired | resolved（会话自己结案，7.36）
    resolved_by       TEXT,              -- 结案的会话（7.36）
    resolve_note      TEXT,              -- 结案依据（会话写的一句话）
    possibly_handled_at          TEXT,   -- 标「可能已处理」的时间（7.36）：主人在来源聊天里后来说过话 / 对来源消息点了表情 /
                                         -- 同来源的另一条提问被答复或结案
    possibly_handled_message_id  TEXT,   -- 触发的那条：主人的发言 / 被点表情的来源消息 / 同来源的另一条提问
    possibly_handled_reason      TEXT,   -- 触发的是哪种：owner_spoke | owner_reaction | sibling_answered | sibling_resolved
    possibly_handled_notified_at TEXT,   -- 已经给负责会话排了核实提醒（每条只提醒一次）
    expiry_reminded_at           TEXT    -- 快到期提醒已排（[escalation] remind_before_hours，每条一次）
);

-- 「需要你定」为哪条消息问的（7.36）：ask 发成功后当场写；提问登记进 escalations 时按 ask_message_id 连上。
-- 一行 = 一条提问的一条来源消息
CREATE TABLE IF NOT EXISTS escalation_sources (
    ask_message_id   TEXT NOT NULL,     -- 机器人那条提问（= escalations.message_id）
    about_message_id TEXT NOT NULL,     -- 为它问的那条消息
    about_chat_id    TEXT,              -- 那条消息所在的聊天
    origin           TEXT NOT NULL,     -- about（--about 指定）| batch（这个会话最近一批送到的）| backfill（老提问按提问时间补的）
    topic_id         TEXT,              -- 提问的会话
    created_at       TEXT NOT NULL,
    PRIMARY KEY (ask_message_id, about_message_id)
);
CREATE INDEX IF NOT EXISTS idx_esc_src_about ON escalation_sources(about_message_id);
CREATE INDEX IF NOT EXISTS idx_esc_src_chat ON escalation_sources(about_chat_id);

-- 机器人发给主人的消息来自哪个 topic（7.15）：主人引用回复它时按这里送回。
-- 7.22 起 sheepdog ask / report / reply 替会话发出的消息也当场记进来（reply 是以主人身份代回的，is_question = 0）
CREATE TABLE IF NOT EXISTS bot_outbox (
    message_id  TEXT PRIMARY KEY,
    topic_id    TEXT NOT NULL,
    is_question INTEGER DEFAULT 0,     -- 同时匹配 header_regex（问题仍照旧进 escalations）
    text        TEXT,
    sent_at     TEXT
);

-- 确认表情（7.9）：消息送达会话后以主人身份点的表情；主人回复后撤下。每条消息只点一次，失败不重试
CREATE TABLE IF NOT EXISTS acks (
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
CREATE INDEX IF NOT EXISTS idx_ack_chat ON acks(chat_id, removed_at);

-- 主人对已投递消息点的表情（7.13 补充）：报过的不重复报
CREATE TABLE IF NOT EXISTS owner_reactions (
    message_id   TEXT NOT NULL,
    emoji_type   TEXT NOT NULL,
    operator_id  TEXT NOT NULL,
    reaction_id  TEXT,
    seen_at      TEXT NOT NULL,
    reported     INTEGER DEFAULT 0,    -- 0 = 首次检查时的基线，只记不报；1 = 已报给会话
    PRIMARY KEY (message_id, emoji_type, operator_id)
);

-- 疑似和 Agent 机器人循环的熔断记录（7.14）：until 之前该聊天非主人的消息进 Inbox
CREATE TABLE IF NOT EXISTS loop_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id      TEXT NOT NULL,
    chat_name    TEXT,
    triggered_at TEXT NOT NULL,
    replies      INTEGER,              -- 触发时 window 内的会话代回次数
    until        TEXT NOT NULL,
    cleared_at   TEXT                  -- 手动解除时间
);
CREATE INDEX IF NOT EXISTS idx_loop_chat ON loop_events(chat_id, until);

-- 会话发起、要调 agentapi 的动作（7.12）：只有常驻进程能跨项目投递，其他进程只入队，由 run 每轮执行
CREATE TABLE IF NOT EXISTS actions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,        -- init | spawn | new_session | push_rules | retire
    args_json    TEXT NOT NULL,
    requested_by TEXT,
    requested_at TEXT NOT NULL,
    done_at      TEXT,                 -- 执行完（成功或失败）；为空 = 排队中
    result       TEXT,
    error        TEXT
);

-- 命令行宿主（Codex / Claude）每一轮的后台运行（7.18）。batch_id 是批次号；不属于批次的轮（新建会话的开场、
-- push-rules）用 sheepdog 自己生成的编号
CREATE TABLE IF NOT EXISTS runs (
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
CREATE INDEX IF NOT EXISTS idx_runs_state ON runs(host, state);

-- 增量读会话记录（7.19 K2、K3）：每个会话、每种用途（compaction 压缩检测 / audit 命令审计）记读到的字节偏移。
-- 会话编号或记录文件变了、文件变短了，就从头读
CREATE TABLE IF NOT EXISTS transcript_cursors (
    topic_id        TEXT NOT NULL,
    purpose         TEXT NOT NULL,
    conversation_id TEXT,
    path            TEXT,
    byte_offset     INTEGER DEFAULT 0,
    updated_at      TEXT,
    PRIMARY KEY (topic_id, purpose)
);

-- 会话跑过的危险命令（7.19 K3）。同一条命令按（会话、step）去重；notified_at 为空 = 还没处理
CREATE TABLE IF NOT EXISTS audit_hits (
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

-- 给主人的私聊通知（2026-10-12 事故后）：先落账本再发。发不出去留在待发，按退避重试，重启后接着试；
-- 2 小时还没发出去标放弃，doctor / board 开头提示，直到人工 sheepdog notices --retry / --ack。[notify] file 模式不走这里
CREATE TABLE IF NOT EXISTS owner_notices (
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
    acked_at    TEXT,                  -- 人工确认已处理（sheepdog notices --ack）：不再提示、不再重发
    reply_to    TEXT,                  -- 7.52：回复哪一条（回执带回回到主人那句话 / 它的话题里）；空 = 新起一条
    in_thread   INTEGER DEFAULT 0      -- 7.52：1 = 回到那一条的话题里
);
CREATE INDEX IF NOT EXISTS idx_owner_notices_state ON owner_notices(state);

-- 在岗表（7.20）：会话卡片，牧羊犬每轮算出来写入。run_state 见 duty.RUN_LABEL
CREATE TABLE IF NOT EXISTS duty_cards (
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

-- 活（7.20）：会话申报（work set / 回执的 work 字段），牧羊犬从 watch、「需要你定」、forward 自动补。
-- 同一把钥匙可以登记在不同会话名下（撞车只提醒不拦）；同一会话同一把钥匙只有一行
CREATE TABLE IF NOT EXISTS work_items (
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
CREATE INDEX IF NOT EXISTS idx_work_key ON work_items(norm_key, closed_at);

-- 信箱（7.20）：会话之间的留言。正文作为一条 sheepdog 系统消息排给收件会话（message_id），
-- 投递、排队、离线不丢都跟着那条消息走；已读 = 带它的批次交了回执
CREATE TABLE IF NOT EXISTS mailbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    from_topic  TEXT NOT NULL,
    to_topic    TEXT NOT NULL,
    key         TEXT,
    body        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    message_id  TEXT
);
CREATE INDEX IF NOT EXISTS idx_mail_pair ON mailbox(from_topic, to_topic, created_at);

-- 履历（7.20）：回执摘要、做完的活、最近动作；保留 [duty] history_days 天
CREATE TABLE IF NOT EXISTS duty_history (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id  TEXT NOT NULL,
    kind      TEXT NOT NULL,           -- receipt | work | action
    text      TEXT NOT NULL,
    at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_topic ON duty_history(topic_id, at);

-- 路由事实（7.20 第二轮）：牧羊犬把带编号的消息投给了哪个会话。不靠会话登记，撞车提醒和「手上的事」以它为主。
-- 同一会话同一把钥匙一行；转交出去或会话自报做完时记 closed_at，这之前的路由不再算，之后又送到就重新算
CREATE TABLE IF NOT EXISTS duty_routes (
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
CREATE INDEX IF NOT EXISTS idx_routes_key ON duty_routes(norm_key, last_at);
CREATE TABLE IF NOT EXISTS duty_route_seen (
    topic_id   TEXT NOT NULL,
    norm_key   TEXT NOT NULL,
    message_id TEXT NOT NULL,
    at         TEXT NOT NULL,
    PRIMARY KEY (topic_id, norm_key, message_id)
);

-- 一事一串（7.22）：会话经 sheepdog ask / report 找主人，同一会话同一把钥匙的消息归在一个话题里。
-- 一行 = 一个话题：开头那条是机器人在主聊天里发的第一条，之后同一把钥匙的消息都回复到它的话题里
CREATE TABLE IF NOT EXISTS owner_threads (
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
CREATE UNIQUE INDEX IF NOT EXISTS idx_owner_threads_key ON owner_threads(topic_id, norm_key) WHERE norm_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_owner_threads_tid ON owner_threads(thread_id);

-- sheepdog ask / report / reply 替会话发出去的每一条（7.22）。按防重编号（发给飞书的 --idempotency-key）一行：
-- 重试时先查这里，发过的不再调飞书；只记发成功的
CREATE TABLE IF NOT EXISTS sends (
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
    identity          TEXT,            -- 用哪个身份发的（7.26，[identity] 开着才写）：user（主人本人）| bot（机器人）
    sources_origin    TEXT,            -- ask 发出时定下的来源口径（about | batch，来源可以是空的：一批来自多个聊天就故意不记）；
                                       -- 空 = 没定过（7.36 以前的版本发的、report、reply）。定过的不再补来源
    floor             TEXT             -- ask 带的 --floor（7.61）：碰到哪条底线才找主人；空 = 没带。和 escalations.message_id
                                       -- 按 message_id 连，体检按它区分「VIP 请求又去找主人批」和「碰到底线」
);
CREATE INDEX IF NOT EXISTS idx_sends_message ON sends(message_id);
CREATE INDEX IF NOT EXISTS idx_sends_root ON sends(root_message_id);

-- 发前再看一眼（7.22）：sheepdog reply 发送前查到主人已经在这个聊天里说过话、没发，记下「这个会话发送前已经看到过
-- 这条主人发言」。同一会话同一条只记一次；这条发言之后作为背景送到这个会话时注明，不重复提醒
CREATE TABLE IF NOT EXISTS seen_before_send (
    topic_id          TEXT NOT NULL,
    message_id        TEXT NOT NULL,   -- 主人的那条发言
    target_message_id TEXT,            -- 当时会话要回复的那条
    seen_at           TEXT NOT NULL,
    PRIMARY KEY (topic_id, message_id)
);

-- 别人私聊机器人（7.26）：不分派，机器人回一句请对方直接找主人本人。同一个人 [identity] bot_dm_reply_hours 内只回一次；
-- 一行 = 回过的一次（只记发成功的）
CREATE TABLE IF NOT EXISTS bot_dm_replies (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_id        TEXT NOT NULL,     -- 私聊机器人的那个人（open_id）
    chat_id          TEXT,
    message_id       TEXT,              -- 他发给机器人的那条
    reply_message_id TEXT,              -- 机器人回的那条
    replied_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bot_dm_sender ON bot_dm_replies(sender_id, replied_at);

-- 回音账本（7.52）：主人每一句需要回应的话（找主人聊天里的回复、改口 / 撤回，群里 @ 机器人的指令）一行，只由牧羊犬写。
-- 阶段：undelivered（没送进任何会话）→ queued（排队等某个会话）→ delivered（送到了）→ settled（还上了）。
-- 还上：会话在送到以后发了 ask / report（report）；回执带回发出去了（relay）；会话已收掉（topic_closed）。
-- alerted：已经报过的阶段（逗号分隔），每个阶段最多报一次
CREATE TABLE IF NOT EXISTS owner_echoes (
    message_id   TEXT PRIMARY KEY,
    chat_id      TEXT,
    kind         TEXT NOT NULL,     -- owner_reply | owner_instruction | recalled | edited
    said_at      TEXT,              -- 主人说这句的时间（消息 create_time）
    stage        TEXT NOT NULL,
    topic_id     TEXT,              -- 现在该谁回（跟着转交走）
    reason       TEXT,              -- 没送进会话时的原因
    recorded_at  TEXT NOT NULL,
    stage_at     TEXT,              -- 进这个阶段的时间
    delivered_at TEXT,              -- 送到的时间（批次 sent_at）
    batch_id     TEXT,
    priority     INTEGER DEFAULT 0, -- 1 = 随主人插话批次送到的
    settled_at   TEXT,
    settled_by   TEXT,
    alerted      TEXT DEFAULT '',
    relay_batch   TEXT,             -- 会话交了哪一批的回执、等着带回（回执带回，7.52）
    relay_at      TEXT,             -- 那次回执收到的时间
    relay_status  TEXT,
    relay_summary TEXT
);
CREATE INDEX IF NOT EXISTS idx_owner_echoes_open ON owner_echoes(settled_at, topic_id);

-- 群话题归属（7.45 第二节，思路来自 PR #1 @Jerry030-1，issue #2）：群里一个话题（chat_id + thread_id）归哪个会话。
-- 认出来一次就记下，之后同一个话题的回复直接查这里；每见到话题里一条新消息就更新最近活动时间，超过 [threads]
-- group_owner_hours 没人说话就不再自动归属（行留着，保留期后清理）
CREATE TABLE IF NOT EXISTS group_threads (
    thread_id        TEXT PRIMARY KEY,  -- omt_…
    chat_id          TEXT NOT NULL,
    topic_id         TEXT NOT NULL,     -- 归属的会话
    source           TEXT NOT NULL,     -- delivered（开头那条送到过它）| sent（开头那条是 sheepdog 替它发的）| forwarded（话题里的
                                        -- 消息被转交给了它）
    root_message_id  TEXT,              -- 话题开头那条
    root_summary     TEXT,              -- 开头那条的摘要（送达时附「这是你处理过的 … 下面的话题回复」）
    created_at       TEXT NOT NULL,
    last_activity_at TEXT NOT NULL      -- 话题里最近一条消息的时间（过期按它，保留期清理也按它）
);
CREATE INDEX IF NOT EXISTS idx_group_threads_chat ON group_threads(chat_id);

-- 找话题开头那条时 mget 过、当时还没有话题的候选（7.45）：一条消息 mget 时没有话题，就不可能是在那之前已经有回复的话题的
-- 开头，同一批候选不再反复 mget
CREATE TABLE IF NOT EXISTS thread_root_checks (
    message_id TEXT PRIMARY KEY,
    checked_at TEXT NOT NULL
);

-- 机器人不在、代回退回主人本人的群（7.26）：sheepdog doctor 列出来，建议把机器人拉进来
CREATE TABLE IF NOT EXISTS bot_absent_chats (
    chat_id     TEXT PRIMARY KEY,
    chat_name   TEXT,
    fallbacks   INTEGER DEFAULT 0,      -- 退回了几次
    last_reason TEXT,                   -- not_in_chat（群列表里没有）| 230002（发送时飞书报不在群里）| list_failed（群列表查不到）
    first_at    TEXT NOT NULL,
    last_at     TEXT NOT NULL
);

-- 跟踪事项（7.51 话题群）：一件事一行、一个话题。编号 item_no 对外写成 T-<item_no>。不跟 7 天保留期走：归档满 30 天才删
CREATE TABLE IF NOT EXISTS tracked_items (
    item_no            INTEGER PRIMARY KEY AUTOINCREMENT,
    title              TEXT NOT NULL,
    owner_topic        TEXT,              -- 负责会话（转交后跟着改）
    opened_by          TEXT,              -- 谁开的：会话 topic_id | owner（主人）
    origin             TEXT NOT NULL,     -- track（会话 --track）| owner_move（主人「放群里」）| owner_topic（主人在群里新开）
    state              TEXT NOT NULL,     -- active | done | archived | removed（只在明确的事件上改）
    status             TEXT,              -- 上一次对账按证据算出的显示状态（变化时在话题里发一行）
    chat_id            TEXT,              -- 话题群
    root_message_id    TEXT,              -- 群里这个话题开头那条（标题卡，或主人开的那条）；还没发出去是空
    thread_id          TEXT,              -- omt_…；发出时可能不知道，mget / 拉取以后补
    dm_root_message_id TEXT,              -- 私聊里原来那个话题的开头那条（「放群里」搬进来的；owner_threads 里这行跟着不清）
    related_item       INTEGER,           -- 可能相关：同一把钥匙归档超过 14 天又用到时新开的，指向老的那件
    done_by            TEXT,              -- 办完 / 移出是谁说的：会话 topic_id | owner
    done_why           TEXT,
    handoff_to         TEXT,              -- 转交给了谁、话题里还没说「已交给 …」（说完清空）
    handoff_at         TEXT,
    opened_at          TEXT NOT NULL,
    last_activity_at   TEXT,              -- 最近动静（会话往里发、主人在里面说话）
    done_at            TEXT,
    archived_at        TEXT,
    reopened_at        TEXT,
    removed_at         TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_items_root ON tracked_items(root_message_id) WHERE root_message_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_items_thread ON tracked_items(thread_id);
CREATE INDEX IF NOT EXISTS idx_items_dm_root ON tracked_items(dm_root_message_id);

-- 事项的钥匙（7.51）：编号本身（kind = number）、会话的钥匙（key）、别名（alias）。norm_key = topicgroup.track_norm
CREATE TABLE IF NOT EXISTS tracked_keys (
    norm_key  TEXT NOT NULL,
    item_no   INTEGER NOT NULL,
    key       TEXT,
    kind      TEXT NOT NULL,
    topic_id  TEXT,
    added_at  TEXT NOT NULL,
    PRIMARY KEY (norm_key, item_no)
);

-- 羔羊（Subagent / Lamb，7.63）：隶属母羊（parent_topic_id）的短命执行体，不进名册、不建独立 topic、禁直连飞书
CREATE TABLE IF NOT EXISTS lambs (
    lamb_id         TEXT PRIMARY KEY,
    parent_topic_id TEXT NOT NULL,
    kind            TEXT NOT NULL,     -- native_subagent | bash_worker
    task_title      TEXT NOT NULL,
    conversation_id TEXT,
    pid             INTEGER,
    state           TEXT NOT NULL,     -- running | done | failed | timeout | cancelled
    started_at      TEXT NOT NULL,
    last_act_at     TEXT,
    finished_at     TEXT,
    result_summary  TEXT,
    log_path        TEXT,
    reported_at     TEXT,              -- 已向母羊投递 lamb_report 批次的时间
    report_msg_id   TEXT               -- 投递回母羊的消息号（或 native_system_message）
);
CREATE INDEX IF NOT EXISTS idx_lambs_parent ON lambs(parent_topic_id, state);
CREATE INDEX IF NOT EXISTS idx_lambs_cid ON lambs(conversation_id);
"""

# sheepdog 自己生成、投给会话的消息（交接回执、等待提醒、总线转达）用这个 chat_type 和 message_id 前缀
SYSTEM_CHAT = "sheepdog"
SYSTEM_ID_PREFIX = "sd_"

# 回音账本里「等着带回」的那份回执（7.52）：只经 echo_bind_relay 写一次；echo_update 只许把它们清空（这句被重新送了一次）
ECHO_RELAY_FIELDS = ("relay_batch", "relay_at", "relay_status", "relay_summary")


# 老库升级：CREATE TABLE IF NOT EXISTS 不会补列，这里按需 ALTER（只加不删）
MIGRATIONS = {
    "messages": [("note", "TEXT"), ("sender_tenant_key", "TEXT"), ("security_tags", "TEXT"),
                 ("security_action", "TEXT"), ("message_position", "TEXT"), ("thread_position", "TEXT"),
                 ("received_by", "TEXT"), ("root_id", "TEXT")],
    "topics": [("onboarded_at", "TEXT"), ("receipt_missed", "INTEGER DEFAULT 0"), ("spawned_at", "TEXT"),
               ("origin_note", "TEXT"), ("rules_hash", "TEXT"), ("host", "TEXT DEFAULT 'antigravity'"),
               ("model", "TEXT"), ("effort", "TEXT"), ("compacted_at", "TEXT"), ("attention_at", "TEXT"),
               ("attention_reason", "TEXT"), ("lifecycle", "TEXT"), ("group_id", "TEXT NOT NULL DEFAULT ''")],
    "acks": [("identity", "TEXT")],
    "watches": [("status", "TEXT"), ("last_reply_at", "TEXT"), ("last_reply_message_id", "TEXT")],
    "duty_cards": [("idle_since", "TEXT"), ("no_record", "INTEGER DEFAULT 0")],
    "work_items": [("declared_at", "TEXT")],
    "audit_hits": [("alert_id", "INTEGER"), ("merged", "INTEGER DEFAULT 0")],
    "sends": [("identity", "TEXT"), ("sources_origin", "TEXT"), ("floor", "TEXT")],
    "owner_notices": [("reply_to", "TEXT"), ("in_thread", "INTEGER DEFAULT 0")],
    "escalations": [("resolved_by", "TEXT"), ("resolve_note", "TEXT"), ("possibly_handled_at", "TEXT"),
                    ("possibly_handled_message_id", "TEXT"), ("possibly_handled_reason", "TEXT"),
                    ("possibly_handled_notified_at", "TEXT"), ("expiry_reminded_at", "TEXT")],
    "lambs": [("parent_topic_id", "TEXT"), ("kind", "TEXT"), ("task_title", "TEXT"),
              ("conversation_id", "TEXT"), ("pid", "INTEGER"), ("state", "TEXT"),
              ("started_at", "TEXT"), ("last_act_at", "TEXT"), ("finished_at", "TEXT"),
              ("result_summary", "TEXT"), ("log_path", "TEXT"), ("reported_at", "TEXT"),
              ("report_msg_id", "TEXT")],
}


# 向飞书查到的群名（机器人事件那条路补群名）记在 meta 里：chat_name:<chat_id> → 群名
CHAT_NAME_META = "chat_name:"

# 动态会话生命周期（7.58 + 一人一会话）
STANDING = "standing"
ONESHOT = "oneshot"
LIFECYCLES = (STANDING, ONESHOT)
STANDING_TITLE_PREFIXES = ("职能·", "人·", "项目·")


def infer_topic_lifecycle(title: str, explicit: str | None = None) -> str:
    """推断动态会话生命周期（7.58）：显式指定优先；否则去掉 [managed] 等前缀后看标题是否以「职能·」「人·」「项目·」开头。"""
    if explicit:
        v = explicit.strip()
        if v not in LIFECYCLES:
            raise ValueError(f"lifecycle 只能是 standing 或 oneshot，收到 {explicit!r}")
        return v
    raw = (title or "").strip()
    if raw.startswith("[") and "]" in raw:
        raw = raw.split("]", 1)[1].strip()
    if any(raw.startswith(p) for p in STANDING_TITLE_PREFIXES):
        return STANDING
    return ONESHOT


def is_standing_topic(t) -> bool:
    """这个会话是不是常设（standing）会话：回执 done 时转为 handled 而不关闭（7.58）。"""
    if t is None:
        return False
    keys = t.keys()
    kind = t["kind"] if "kind" in keys else None
    tid = t["topic_id"] if "topic_id" in keys else None
    if kind == "adopted" or tid == "tp_bus":
        return True
    if kind == "dynamic":
        lc = (t["lifecycle"] if "lifecycle" in keys else None) or ""
        if lc in LIFECYCLES:
            return lc == STANDING
        return infer_topic_lifecycle(t["title"] if "title" in keys else "") == STANDING
    return False


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path | str):
        self.path = path
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        for table, cols in MIGRATIONS.items():
            have = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols:
                if name not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        self.conn.commit()

    @classmethod
    def snapshot(cls, path: Path) -> "Store":
        """--dry-run 用：把账本复制进内存，之后的写入都不落盘。

        否则 dry-run 会把假的 conversation_id、onboarded_at、水位线写进真实账本，正式运行时漏发 onboarding。
        """
        mem = cls(":memory:")
        if Path(path).exists():
            src = sqlite3.connect(str(path))
            try:
                src.backup(mem.conn)
            finally:
                src.close()
            # backup 把内存库整个换成了老账本：新加的表（如在岗表，7.20）要再建一次，新加的列再补一次
            mem.conn.executescript(SCHEMA)
            mem._migrate()
        return mem

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ---------- meta ----------
    def get_meta(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def meta_items(self, prefix: str) -> list[tuple[str, str]]:
        """key 以 prefix 开头的 meta（按 key 排）。prefix 里的 % _ 按字面算。"""
        esc = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = self.conn.execute("SELECT key, value FROM meta WHERE key LIKE ? ESCAPE '\\' ORDER BY key",
                                 (esc + "%",)).fetchall()
        return [(r["key"], r["value"]) for r in rows]

    def delete_meta(self, key: str) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM meta WHERE key=?", (key,))

    def sends_after(self, topic_id: str, after: datetime) -> int:
        """这个会话在 after 之后经 sheepdog 发出去的消息（sends 表：ask / report / reply 等）有几条。"""
        n = 0
        for r in self.conn.execute("SELECT sent_at FROM sends WHERE topic_id=?", (topic_id,)).fetchall():
            try:
                at = datetime.fromisoformat(r["sent_at"] or "")
            except ValueError:
                continue
            if at.tzinfo is not None and at > after:
                n += 1
        return n

    # ---------- messages ----------
    def get_message(self, message_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM messages WHERE message_id=?", (message_id,)).fetchone()

    def is_my_message(self, message_id: str, self_open_id: str) -> bool:
        row = self.conn.execute("SELECT sender_id FROM messages WHERE message_id=?", (message_id,)).fetchone()
        return bool(row and self_open_id and row["sender_id"] == self_open_id)

    def upsert_message(self, m: Message, route: str, reason: str, tags: list[str], topic_id: str | None = None,
                       received_by: str | None = None) -> None:
        ts = now_iso()
        mentions = json.dumps([asdict(x) for x in m.mentions], ensure_ascii=False)
        dispatch_state = "pending" if route == "dispatch" else None
        with self.tx() as c:
            c.execute(
                """INSERT INTO messages(message_id,chat_id,chat_name,chat_type,sender_id,sender_name,sender_type,
                       sender_tenant_key,content,msg_type,create_time,mentions_json,reply_to,thread_id,link,deleted,
                       updated,update_time,route,reason,tags_json,topic_id,dispatch_state,first_seen,last_seen,
                       message_position,thread_position,received_by,root_id)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (m.message_id, m.chat_id, m.chat_name, m.chat_type, m.sender_id, m.sender_name, m.sender_type,
                 m.sender_tenant_key,
                 m.content, m.msg_type, m.create_time, mentions, m.reply_to, m.thread_id, m.link,
                 int(m.deleted), int(m.updated), m.update_time, route, reason,
                 json.dumps(tags, ensure_ascii=False), topic_id, dispatch_state, ts, ts, m.message_position or None,
                 m.thread_position or None, received_by, m.root_id or None),
            )

    def merge_identity_copy(self, message_id: str, m: Message, received_by: str, fill_content: bool) -> None:
        """另一个身份又收到了同一条（7.26）：账本仍只有一条。received_by 记成 both；账本里空着的字段（群名、发送人名、
        租户、序号、话题）用这一份补上。fill_content：账本里那份是机器人实时接收的原样（正文渲染和搜索不一样），换成这一份的
        正文，不当成编辑。"""
        row = self.get_message(message_id)
        if row is None:
            return
        sets, vals = ["last_seen=?"], [now_iso()]
        prev = row["received_by"] or "user"   # 开关打开以前入账的（为空）都是主人身份搜到的
        if prev != received_by and prev != "both":
            sets.append("received_by='both'")
        for col, val in (("chat_name", m.chat_name), ("sender_name", m.sender_name),
                         ("sender_tenant_key", m.sender_tenant_key), ("message_position", m.message_position),
                         ("thread_position", m.thread_position), ("thread_id", m.thread_id), ("link", m.link),
                         ("reply_to", m.reply_to), ("root_id", m.root_id)):
            if val and not row[col]:
                sets.append(f"{col}=?")
                vals.append(val)
        if fill_content:
            sets += ["content=?", "chat_type=?", "msg_type=?", "mentions_json=?", "create_time=?"]
            vals += [m.content, m.chat_type or row["chat_type"], m.msg_type or row["msg_type"],
                     json.dumps([asdict(x) for x in m.mentions], ensure_ascii=False), m.create_time or row["create_time"]]
        vals.append(message_id)
        with self.tx() as c:
            c.execute(f"UPDATE messages SET {', '.join(sets)} WHERE message_id=?", vals)

    def known_chat_name(self, chat_id: str) -> str:
        """账本里这个聊天最近一次记过的非空群名：先看消息，再看查群名时记下的（meta chat_name:<chat_id>）。没有是空串。"""
        r = self.conn.execute("SELECT chat_name FROM messages WHERE chat_id=? AND chat_name IS NOT NULL AND chat_name != '' "
                              "ORDER BY create_time DESC LIMIT 1", (chat_id,)).fetchone()
        return r[0] if r is not None else self.get_meta(CHAT_NAME_META + chat_id)

    def remember_chat_name(self, chat_id: str, name: str) -> None:
        self.set_meta(CHAT_NAME_META + chat_id, name)

    def set_received_by(self, message_id: str, received_by: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE messages SET received_by=? WHERE message_id=?", (received_by, message_id))

    def sender_in(self, message_id: str, sender_ids: set[str]) -> bool:
        """账本里这条消息的发送人在不在 sender_ids 里（7.26：主人本人、机器人）。"""
        row = self.conn.execute("SELECT sender_id FROM messages WHERE message_id=?", (message_id,)).fetchone()
        return bool(row and row["sender_id"] and row["sender_id"] in sender_ids)

    # ---------- 7.26 别人私聊机器人、机器人不在的群 ----------
    def bot_dm_replied_since(self, sender_id: str, since_iso: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM bot_dm_replies WHERE sender_id=? AND replied_at >= ? ORDER BY replied_at DESC LIMIT 1",
            (sender_id, since_iso)).fetchone()

    def add_bot_dm_reply(self, sender_id: str, chat_id: str, message_id: str, reply_message_id: str) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO bot_dm_replies(sender_id,chat_id,message_id,reply_message_id,replied_at) "
                      "VALUES(?,?,?,?,?)", (sender_id, chat_id, message_id, reply_message_id, now_iso()))

    def list_bot_dm_replies(self, since_iso: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM bot_dm_replies WHERE replied_at >= ? ORDER BY replied_at DESC",
                                 (since_iso,)).fetchall()

    def note_bot_absent(self, chat_id: str, chat_name: str, reason: str) -> None:
        ts = now_iso()
        with self.tx() as c:
            c.execute("""INSERT INTO bot_absent_chats(chat_id,chat_name,fallbacks,last_reason,first_at,last_at)
                         VALUES(?,?,1,?,?,?)
                         ON CONFLICT(chat_id) DO UPDATE SET fallbacks=fallbacks+1, last_reason=excluded.last_reason,
                             last_at=excluded.last_at, chat_name=COALESCE(NULLIF(excluded.chat_name,''), chat_name)""",
                      (chat_id, chat_name or "", reason, ts, ts))

    def bot_absent_chats(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM bot_absent_chats ORDER BY last_at DESC").fetchall()

    def forget_bot_absent(self, chat_ids: set[str]) -> None:
        """机器人已经在这些群里了：从「机器人不在」的清单里去掉。"""
        if not chat_ids:
            return
        with self.tx() as c:
            c.executemany("DELETE FROM bot_absent_chats WHERE chat_id=?", [(x,) for x in chat_ids])

    def update_content(self, message_id: str, m: Message, route: str | None = None,
                       reason: str | None = None, dispatch_state: str | None = None,
                       tags: list[str] | None = None, topic_id: str | None = None) -> None:
        sets = ["content=?", "updated=?", "update_time=?", "deleted=?", "mentions_json=?", "last_seen=?"]
        vals: list = [m.content, int(m.updated), m.update_time, int(m.deleted),
                      json.dumps([asdict(x) for x in m.mentions], ensure_ascii=False), now_iso()]
        if route is not None:
            sets += ["route=?", "reason=?"]
            vals += [route, reason]
        if dispatch_state is not None:
            sets.append("dispatch_state=?")
            vals.append(dispatch_state)
        if tags is not None:
            sets.append("tags_json=?")
            vals.append(json.dumps(tags, ensure_ascii=False))
        if topic_id is not None:
            sets.append("topic_id=?")
            vals.append(topic_id)
        vals.append(message_id)
        with self.tx() as c:
            c.execute(f"UPDATE messages SET {', '.join(sets)} WHERE message_id=?", vals)

    def set_security(self, message_id: str, rule_names: list[str], action: str,
                     topic_id: str | None, tags: list[str]) -> None:
        with self.tx() as c:
            c.execute("UPDATE messages SET security_tags=?, security_action=?, topic_id=?, tags_json=? WHERE message_id=?",
                      (json.dumps(rule_names, ensure_ascii=False), action, topic_id,
                       json.dumps(tags, ensure_ascii=False), message_id))

    def security_log(self, since_iso: str, limit: int = 500) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT * FROM messages WHERE security_action IN ('tag','hold') AND first_seen >= ?
               ORDER BY first_seen DESC LIMIT ?""",
            (since_iso, limit),
        ).fetchall()

    def touch(self, message_id: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE messages SET last_seen=? WHERE message_id=?", (now_iso(), message_id))

    def set_read(self, status: dict[str, bool]) -> None:
        with self.tx() as c:
            for mid, r in status.items():
                c.execute("UPDATE messages SET is_read=? WHERE message_id=?", (int(r), mid))

    def pending_dispatch(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM messages WHERE route='dispatch' AND dispatch_state='pending' ORDER BY create_time"
        ).fetchall()

    def mark_batch(self, message_ids: list[str], batch_id: str, topic_id: str, state: str) -> None:
        with self.tx() as c:
            c.executemany(
                "UPDATE messages SET dispatch_state=?, batch_id=?, topic_id=? WHERE message_id=?",
                [(state, batch_id, topic_id, mid) for mid in message_ids],
            )

    def mark_batch_state(self, batch_id: str, state: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE messages SET dispatch_state=? WHERE batch_id=?", (state, batch_id))

    def forward_message(self, message_id: str, topic_id: str, tags: list[str]) -> None:
        """转交：改归属并回到待推，由 Dispatcher 下一轮按目标会话的状态投递（同样受人类接管约束）。"""
        with self.tx() as c:
            c.execute(
                """UPDATE messages SET route='dispatch', dispatch_state='pending', batch_id=NULL,
                       topic_id=?, tags_json=? WHERE message_id=?""",
                (topic_id, json.dumps(tags, ensure_ascii=False), message_id),
            )

    def add_system_message(self, topic_id: str, reason: str, content: str, tags: list[str] | None = None) -> str:
        """sheepdog 自己生成的待推消息，和 IM 消息一起按 topic 成批投递（同样受人类接管、排队约束）。"""
        ts = datetime.now().astimezone()
        mid = f"{SYSTEM_ID_PREFIX}{reason}_{uuid.uuid4().hex[:12]}"
        m = Message(message_id=mid, chat_id=SYSTEM_CHAT, chat_name=SYSTEM_CHAT, chat_type=SYSTEM_CHAT,
                    sender_id=SYSTEM_CHAT, sender_name=SYSTEM_CHAT, sender_type="system", content=content,
                    msg_type="text", create_time=ts.isoformat(timespec="seconds"))
        self.upsert_message(m, "dispatch", reason, tags or [], topic_id)
        return mid

    def requeue_batch(self, batch_id: str) -> None:
        # 主人本人的发言是背景，不随超时重投（7.13）
        with self.tx() as c:
            c.execute("UPDATE messages SET dispatch_state='pending', batch_id=NULL WHERE batch_id=? AND reason != 'owner_context'",
                      (batch_id,))

    def route_owner_context(self, message_id: str, topic_id: str, note: str) -> None:
        """把主人本人的一条发言排给 topic 作背景（7.13）。"""
        with self.tx() as c:
            c.execute("""UPDATE messages SET route='dispatch', reason='owner_context', dispatch_state='pending',
                             topic_id=?, note=? WHERE message_id=?""", (topic_id, note or None, message_id))

    def route_owner_instruction(self, message_id: str, topic_id: str, tags: list[str], note: str) -> None:
        """主人在群里 @ 机器人（7.45 A 类）：排给 topic 当指令（dispatch / owner_instruction / 待推）。"""
        with self.tx() as c:
            c.execute("""UPDATE messages SET route='dispatch', reason='owner_instruction', dispatch_state='pending',
                             topic_id=?, tags_json=?, note=? WHERE message_id=?""",
                      (topic_id, json.dumps(tags, ensure_ascii=False), note or None, message_id))

    def delivered_since(self, since_iso: str, limit: int = 500) -> list[sqlite3.Row]:
        """since 之后投递给会话的 IM 消息（不含 sheepdog 自己生成的、不含主人本人的背景）。"""
        return self.conn.execute(
            """SELECT DISTINCT m.* FROM messages m JOIN dispatches d ON d.batch_id = m.batch_id
               WHERE m.route='dispatch' AND m.dispatch_state IN ('delivered','acked','missed')
                 AND m.chat_type != 'sheepdog' AND m.reason != 'owner_context' AND d.sent_at >= ?
               ORDER BY d.sent_at DESC LIMIT ?""", (since_iso, limit)).fetchall()

    def owner_reaction_known(self, message_id: str, emoji_type: str, operator_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM owner_reactions WHERE message_id=? AND emoji_type=? AND operator_id=?",
            (message_id, emoji_type, operator_id)).fetchone() is not None

    def add_owner_reaction(self, message_id: str, emoji_type: str, operator_id: str, reaction_id: str,
                           reported: bool) -> None:
        with self.tx() as c:
            c.execute("""INSERT OR IGNORE INTO owner_reactions(message_id,emoji_type,operator_id,reaction_id,seen_at,reported)
                         VALUES(?,?,?,?,?,?)""", (message_id, emoji_type, operator_id, reaction_id, now_iso(), int(reported)))

    def ack_reaction_ids(self) -> set[str]:
        """sheepdog 以主人身份点的确认表情：查主人表情时要排除。"""
        return {r[0] for r in self.conn.execute(
            "SELECT reaction_id FROM acks WHERE reaction_id IS NOT NULL AND (identity IS NULL OR identity='user')")}

    # ---------- loop guard ----------
    def own_messages_in_chat(self, chat_id: str, self_open_id: str, limit: int = 200) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM messages WHERE chat_id=? AND sender_id=? ORDER BY first_seen DESC LIMIT ?",
            (chat_id, self_open_id, limit)).fetchall()

    def others_in_chat(self, chat_id: str, self_open_id: str, limit: int = 50) -> list[sqlite3.Row]:
        """聊天里非主人发来的消息，按发送时间倒序（sheepdog 自己生成的不算）。"""
        return self.conn.execute(
            """SELECT * FROM messages WHERE chat_id=? AND sender_id != ? AND chat_type != 'sheepdog'
               ORDER BY create_time DESC, first_seen DESC LIMIT ?""", (chat_id, self_open_id, limit)).fetchall()

    def add_loop_event(self, chat_id: str, chat_name: str, replies: int, until_iso: str) -> int:
        with self.tx() as c:
            cur = c.execute("INSERT INTO loop_events(chat_id,chat_name,triggered_at,replies,until) VALUES(?,?,?,?,?)",
                            (chat_id, chat_name, now_iso(), replies, until_iso))
            return int(cur.lastrowid)

    def open_loop_events(self) -> list[sqlite3.Row]:
        """没被手动解除的熔断记录（是否还在冷却由调用方按 until 判断）。"""
        return self.conn.execute("SELECT * FROM loop_events WHERE cleared_at IS NULL ORDER BY id DESC").fetchall()

    def list_loop_events(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM loop_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def clear_loop(self, chat_id: str) -> int:
        with self.tx() as c:
            return c.execute("UPDATE loop_events SET cleared_at=? WHERE chat_id=? AND cleared_at IS NULL",
                             (now_iso(), chat_id)).rowcount

    def last_delivered_topic(self, chat_id: str, since_iso: str) -> str | None:
        """since 之后最近一次把这个聊天（别人发的）消息投递出去的 topic。"""
        row = self.conn.execute(
            """SELECT m.topic_id FROM messages m JOIN dispatches d ON d.batch_id = m.batch_id
               WHERE m.chat_id=? AND m.route='dispatch' AND m.reason != 'owner_context' AND d.sent_at >= ?
               ORDER BY d.sent_at DESC LIMIT 1""", (chat_id, since_iso)).fetchone()
        return row["topic_id"] if row else None

    # ---------- inbox ----------
    def inbox_summary(self, self_open_id: str = "") -> list[sqlite3.Row]:
        """按群汇总 Inbox；owner_last 是主人在该群最近一次发言的时间（7.13，没有为空）。"""
        return self.conn.execute(
            """SELECT m.chat_id, m.chat_name, m.reason,
                      COUNT(*) AS total,
                      SUM(CASE WHEN m.is_read IS NULL OR m.is_read=0 THEN 1 ELSE 0 END) AS unread,
                      MAX(m.create_time) AS last_time,
                      (SELECT MAX(s.create_time) FROM messages s
                        WHERE s.chat_id = m.chat_id AND ? != '' AND s.sender_id = ?) AS owner_last
               FROM messages m WHERE m.route='inbox' AND m.inbox_cleared=0 AND m.deleted=0
               GROUP BY m.chat_id ORDER BY last_time DESC""",
            (self_open_id, self_open_id),
        ).fetchall()

    def inbox_messages(self, chat_query: str, limit: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT * FROM messages WHERE route='inbox' AND inbox_cleared=0 AND deleted=0
                 AND (chat_id=? OR chat_name LIKE ?) ORDER BY create_time DESC LIMIT ?""",
            (chat_query, f"%{chat_query}%", limit),
        ).fetchall()

    def inbox_clear(self, chat_query: str | None) -> int:
        with self.tx() as c:
            if chat_query is None:
                cur = c.execute("UPDATE messages SET inbox_cleared=1 WHERE route='inbox' AND inbox_cleared=0")
            else:
                cur = c.execute(
                    "UPDATE messages SET inbox_cleared=1 WHERE route='inbox' AND inbox_cleared=0 AND (chat_id=? OR chat_name LIKE ?)",
                    (chat_query, f"%{chat_query}%"),
                )
            return cur.rowcount

    def unread_inbox_ids(self, limit: int = 500) -> list[str]:
        rows = self.conn.execute(
            "SELECT message_id FROM messages WHERE route='inbox' AND inbox_cleared=0 AND (is_read IS NULL OR is_read=0) ORDER BY create_time DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [r["message_id"] for r in rows]

    # ---------- topics ----------
    def get_topic(self, topic_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM topics WHERE topic_id=?", (topic_id,)).fetchone()

    def list_topics(self, kind: str | None = None) -> list[sqlite3.Row]:
        if kind:
            return self.conn.execute("SELECT * FROM topics WHERE kind=? ORDER BY last_active DESC", (kind,)).fetchall()
        return self.conn.execute("SELECT * FROM topics ORDER BY last_active DESC").fetchall()

    def create_topic(self, topic_id: str, title: str, kind: str, duty: str, state: str,
                     host: str = "antigravity", model: str = "", effort: str = "",
                     lifecycle: str | None = None, group_id: str = "") -> None:
        ts = now_iso()
        with self.tx() as c:
            c.execute(
                """INSERT INTO topics(topic_id,title,kind,duty,state,host,model,effort,lifecycle,group_id,created_at,last_active)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (topic_id, title, kind, duty, state, host or "antigravity", model or None, effort or None,
                 lifecycle or None, group_id or "", ts, ts),
            )

    def count_topics_since(self, kind: str, since_iso: str) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM topics WHERE kind=? AND created_at >= ?",
                                 (kind, since_iso)).fetchone()[0]

    def update_topic(self, topic_id: str, **fields) -> None:
        fields.pop("last_error", None)
        if not fields:
            return
        fields.setdefault("last_active", now_iso())
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE topics SET {cols} WHERE topic_id=?", (*fields.values(), topic_id))

    def upsert_topic(self, topic_id: str, **fields) -> None:
        existing = self.get_topic(topic_id)
        if existing is None:
            title = fields.pop("title", topic_id)
            kind = fields.pop("kind", "dynamic")
            duty = fields.pop("duty", "")
            state = fields.pop("state", "active")
            host = fields.pop("host", "antigravity")
            model = fields.pop("model", "") or ""
            effort = fields.pop("effort", "") or ""
            lifecycle = fields.pop("lifecycle", None)
            group_id = fields.pop("group_id", "") or ""
            self.create_topic(topic_id, title, kind, duty, state, host, model, effort,
                              lifecycle=lifecycle, group_id=group_id)
        if fields:
            self.update_topic(topic_id, **fields)

    def get_group_topics(self, group_id: str, include_closed: bool = False) -> list[sqlite3.Row]:
        """按羊圈（group_id，7.49）列出组内会话：#1（不带 #）排第一，其余按 #2, #3 升序。"""
        gid = (group_id or "").strip()
        if not gid:
            return []
        if include_closed:
            rows = self.conn.execute("SELECT * FROM topics WHERE group_id=?", (gid,)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM topics WHERE group_id=? AND state != 'closed'", (gid,)
            ).fetchall()

        def _sort_key(r: sqlite3.Row) -> tuple[int, int, str]:
            tid = r["topic_id"]
            if "#" not in tid:
                return (0, 1, tid)
            _, _, tail = tid.partition("#")
            return (1, int(tail) if tail.isdigit() else 999999, tid)

        return sorted(rows, key=_sort_key)

    def find_recent_thread_topic_in_group(
        self,
        group_id: str,
        chat_id: str = "",
        root_id: str = "",
        parent_id: str = "",
        reply_to_msg_id: str = "",
        thread_id: str = "",
        sender_id: str = "",
        window_minutes: int = 30,
    ) -> str | None:
        """羊圈内话题/回复链/watch/等待回复亲和性查询（7.49 Step 1）：
        同一回复链、引用回复或未结 watch 绝不拆到别的成员。"""
        gid = (group_id or "").strip()
        if not gid:
            return None
        chain_ids = [x for x in dict.fromkeys([root_id, parent_id, reply_to_msg_id, thread_id]) if x]
        if chain_ids:
            placeholders = ",".join("?" for _ in chain_ids)
            row = self.conn.execute(
                f"""SELECT b.topic_id FROM bot_outbox b
                    JOIN topics t ON t.topic_id = b.topic_id
                    WHERE t.group_id = ? AND t.state != 'closed'
                      AND b.message_id IN ({placeholders})
                    ORDER BY b.sent_at DESC LIMIT 1""",
                (gid, *chain_ids),
            ).fetchone()
            if row is not None:
                return row["topic_id"]
            row = self.conn.execute(
                f"""SELECT e.topic_id FROM escalations e
                    JOIN topics t ON t.topic_id = e.topic_id
                    WHERE t.group_id = ? AND t.state != 'closed' AND e.closed_at IS NULL
                      AND e.message_id IN ({placeholders})
                    ORDER BY e.asked_at DESC LIMIT 1""",
                (gid, *chain_ids),
            ).fetchone()
            if row is not None:
                return row["topic_id"]
            row = self.conn.execute(
                f"""SELECT m.topic_id FROM messages m
                    JOIN topics t ON t.topic_id = m.topic_id
                    WHERE t.group_id = ? AND t.state != 'closed'
                      AND m.route = 'dispatch' AND m.topic_id IS NOT NULL
                      AND (
                          m.message_id IN ({placeholders})
                          OR m.root_id IN ({placeholders})
                          OR m.reply_to IN ({placeholders})
                          OR m.thread_id IN ({placeholders})
                      )
                    ORDER BY m.first_seen DESC, m.rowid DESC LIMIT 1""",
                (gid, *chain_ids, *chain_ids, *chain_ids, *chain_ids),
            ).fetchone()
            if row is not None:
                return row["topic_id"]
        if sender_id or chat_id:
            row = self.conn.execute(
                """SELECT w.topic_id FROM watches w
                   JOIN topics t ON t.topic_id = w.topic_id
                   WHERE t.group_id = ? AND t.state != 'closed' AND w.closed_at IS NULL
                     AND (? = '' OR w.person_id = '' OR w.person_id = ?)
                     AND (? = '' OR w.chat_id IS NULL OR w.chat_id = '' OR w.chat_id = ?)
                   ORDER BY w.started_at DESC, w.id DESC LIMIT 1""",
                (gid, sender_id, sender_id, chat_id, chat_id),
            ).fetchone()
            if row is not None:
                return row["topic_id"]
        if chat_id and sender_id and window_minutes > 0:
            cutoff = (datetime.now().astimezone() - timedelta(minutes=window_minutes)).isoformat(timespec="seconds")
            row = self.conn.execute(
                """SELECT m.topic_id FROM messages m
                   JOIN topics t ON t.topic_id = m.topic_id
                   WHERE t.group_id = ? AND t.state = 'waiting_human'
                     AND m.route = 'dispatch' AND m.chat_id = ? AND m.sender_id = ?
                     AND m.first_seen >= ?
                   ORDER BY m.first_seen DESC, m.rowid DESC LIMIT 1""",
                (gid, chat_id, sender_id, cutoff),
            ).fetchone()
            if row is not None:
                return row["topic_id"]
        return None

    # ---------- dispatches ----------
    def dispatch_exists(self, batch_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM dispatches WHERE batch_id=?", (batch_id,)).fetchone() is not None

    def record_dispatch(self, batch_id: str, topic_id: str, message_ids: list[str], state: str = "sent") -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO dispatches(batch_id,topic_id,message_ids,sent_at,state) VALUES(?,?,?,?,?)",
                (batch_id, topic_id, json.dumps(message_ids), now_iso(), state),
            )

    def get_dispatch(self, batch_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM dispatches WHERE batch_id=?", (batch_id,)).fetchone()

    def dispatches_in_state(self, state: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM dispatches WHERE state=? ORDER BY sent_at", (state,)).fetchall()

    def set_dispatch_state(self, batch_id: str, state: str, error: str | None = None) -> None:
        with self.tx() as c:
            c.execute("UPDATE dispatches SET state=?, error=COALESCE(?, error) WHERE batch_id=?", (state, error, batch_id))

    def timed_out_dispatches(self, topic_id: str, error_prefix: str) -> list[sqlite3.Row]:
        """这个会话判过回执超时的批次（failed / missed，error 以 error_prefix 开头），按发送时间排。"""
        return self.conn.execute(
            "SELECT * FROM dispatches WHERE topic_id=? AND state IN ('failed','missed') AND error LIKE ? ORDER BY sent_at",
            (topic_id, error_prefix + "%")).fetchall()

    def ack_late_messages(self, batch_id: str, message_ids: list[str]) -> int:
        """晚到回执（7.21）：这批里超时后退回排队、还没再投出去的消息，会话其实已经处理过了——改记为这一批已回执，
        不再重投；已经随别的批次再投出去的不动。超时没重投的（adopted 的 missed）一并改成 acked。返回改了几条。"""
        with self.tx() as c:
            n = 0
            for mid in message_ids:
                n += c.execute(
                    """UPDATE messages SET dispatch_state='acked', batch_id=?
                       WHERE message_id=? AND reason != 'owner_context'
                         AND ((dispatch_state='pending' AND batch_id IS NULL) OR (dispatch_state='missed' AND batch_id=?))""",
                    (batch_id, mid, batch_id)).rowcount
            return n

    def last_receipt(self, topic_id: str) -> sqlite3.Row | None:
        """这个会话最近一次交了回执的批次（含晚到的）。"""
        return self.conn.execute(
            """SELECT * FROM dispatches WHERE topic_id=? AND state IN ('acked','acked_late') AND receipt IS NOT NULL
                 AND receipt != '' ORDER BY sent_at DESC LIMIT 1""", (topic_id,)).fetchone()

    def count_pending_signals(self, topic_id: str) -> int:
        """排给这个会话、还没投出去的信号（不含主人发言的背景）。"""
        return self.conn.execute(
            """SELECT COUNT(*) FROM messages WHERE route='dispatch' AND dispatch_state='pending'
                 AND reason != 'owner_context' AND topic_id=?""", (topic_id,)).fetchone()[0]

    def count_pending_messages_for_topic(self, topic_id: str) -> int:
        """排给这个会话、还没投出去的待处理消息数（7.49 羊圈繁忙度判定）。"""
        return self.count_pending_signals(topic_id)

    def requeue_messages(self, message_ids: list[str]) -> None:
        """人工确认后重发（redeliver）：把这些消息放回待推。"""
        with self.tx() as c:
            c.executemany("UPDATE messages SET dispatch_state='pending', batch_id=NULL WHERE message_id=?",
                          [(m,) for m in message_ids])

    def finish_dispatch(self, batch_id: str, state: str, receipt: str = "", error: str = "") -> None:
        with self.tx() as c:
            c.execute("UPDATE dispatches SET state=?, receipt=?, error=? WHERE batch_id=?", (state, receipt, error, batch_id))

    def set_reason(self, message_id: str, route: str, reason: str, tags: list[str]) -> None:
        """改一条已入账消息的去向（7.48：找主人聊天里机器人身份先收到的卡片，主人身份搜到正文后重新认出来）。不碰投递状态。"""
        with self.tx() as c:
            c.execute("UPDATE messages SET route=?, reason=?, tags_json=? WHERE message_id=?",
                      (route, reason, json.dumps(tags, ensure_ascii=False), message_id))

    def set_note(self, message_id: str, note: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE messages SET note=? WHERE message_id=?", (note, message_id))

    # ---------- escalations ----------
    def add_escalation(self, message_id: str, topic_id: str, chat_id: str, text: str, asked_at: str) -> None:
        with self.tx() as c:
            c.execute("""INSERT OR IGNORE INTO escalations(message_id,topic_id,chat_id,text,asked_at)
                         VALUES(?,?,?,?,?)""", (message_id, topic_id, chat_id, text, asked_at))

    def add_bot_outbox(self, message_id: str, topic_id: str, is_question: bool, text: str, sent_at: str) -> None:
        with self.tx() as c:
            c.execute("INSERT OR IGNORE INTO bot_outbox(message_id,topic_id,is_question,text,sent_at) VALUES(?,?,?,?,?)",
                      (message_id, topic_id, int(is_question), text, sent_at))

    def get_bot_outbox(self, message_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM bot_outbox WHERE message_id=?", (message_id,)).fetchone()

    def get_escalation(self, message_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM escalations WHERE message_id=?", (message_id,)).fetchone()

    def open_escalations(self, topic_id: str | None = None) -> list[sqlite3.Row]:
        sql, args = "SELECT * FROM escalations WHERE closed_at IS NULL", []
        if topic_id:
            sql += " AND topic_id=?"
            args.append(topic_id)
        return self.conn.execute(sql + " ORDER BY asked_at DESC", args).fetchall()

    def list_escalations(self, include_closed: bool = False, limit: int = 200) -> list[sqlite3.Row]:
        where = "" if include_closed else "WHERE closed_at IS NULL"
        return self.conn.execute(f"SELECT * FROM escalations {where} ORDER BY asked_at DESC LIMIT ?", (limit,)).fetchall()

    def answer_escalation(self, message_id: str, answer_message_id: str) -> bool:
        ts = now_iso()
        with self.tx() as c:
            cur = c.execute("""UPDATE escalations SET answered_at=?, answer_message_id=?, closed_at=?, close_reason='answered'
                               WHERE message_id=? AND closed_at IS NULL""", (ts, answer_message_id, ts, message_id))
            return cur.rowcount > 0

    def expire_escalation(self, message_id: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE escalations SET closed_at=?, close_reason='expired' WHERE message_id=? AND closed_at IS NULL",
                      (now_iso(), message_id))

    def resolve_escalation(self, message_id: str, topic_id: str, note: str, resolved_by: str = "") -> bool:
        """会话结案（7.36）：只结 topic_id（提问的会话）名下、还开着的。resolved_by：谁结的，默认就是提问的会话；
        原会话已收掉时总线替它结，记总线（7.38）。返回是否结了。"""
        ts = now_iso()
        with self.tx() as c:
            return c.execute("""UPDATE escalations SET closed_at=?, close_reason='resolved', resolved_by=?, resolve_note=?
                                WHERE message_id=? AND topic_id=? AND closed_at IS NULL""",
                             (ts, resolved_by or topic_id, note, message_id, topic_id)).rowcount > 0

    def mark_possibly_handled(self, message_id: str, trigger_message_id: str, reason: str, at: str = "") -> bool:
        """标「可能已处理」（7.36）：只标还开着、没标过的。返回是否标了。"""
        with self.tx() as c:
            return c.execute("""UPDATE escalations SET possibly_handled_at=?, possibly_handled_message_id=?,
                                       possibly_handled_reason=?
                                WHERE message_id=? AND closed_at IS NULL AND possibly_handled_at IS NULL""",
                             (at or now_iso(), trigger_message_id, reason, message_id)).rowcount > 0

    def set_escalation_mark(self, message_id: str, column: str, at: str = "") -> bool:
        """记一次提醒（possibly_handled_notified_at / expiry_reminded_at）：只记空的。返回是否记了。"""
        if column not in ("possibly_handled_notified_at", "expiry_reminded_at"):
            raise ValueError(column)
        with self.tx() as c:
            return c.execute(f"UPDATE escalations SET {column}=? WHERE message_id=? AND {column} IS NULL",
                             (at or now_iso(), message_id)).rowcount > 0

    def add_escalation_sources(self, ask_message_id: str, topic_id: str, sources: list[tuple[str, str]],
                               origin: str) -> int:
        """记一条提问的来源消息（(消息号, 聊天) 列表）。同一条提问同一条来源只记一次。返回新记了几条。"""
        ts = now_iso()
        n = 0
        with self.tx() as c:
            for mid, chat in sources:
                n += c.execute("""INSERT OR IGNORE INTO escalation_sources(ask_message_id,about_message_id,about_chat_id,
                                      origin,topic_id,created_at) VALUES(?,?,?,?,?,?)""",
                               (ask_message_id, mid, chat or None, origin, topic_id, ts)).rowcount
        return n

    def escalation_sources(self, ask_message_id: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM escalation_sources WHERE ask_message_id=? ORDER BY rowid",
                                 (ask_message_id,)).fetchall()

    def sources_about(self, about_message_ids: list[str]) -> list[sqlite3.Row]:
        """这些来源消息被哪些提问记过。"""
        if not about_message_ids:
            return []
        marks = ",".join("?" * len(about_message_ids))
        return self.conn.execute(f"SELECT * FROM escalation_sources WHERE about_message_id IN ({marks}) ORDER BY rowid",
                                 list(about_message_ids)).fetchall()

    # ---------- 一事一串（7.22） ----------
    def thread_by_key(self, topic_id: str, norm_key: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM owner_threads WHERE topic_id=? AND norm_key=?",
                                 (topic_id, norm_key)).fetchone()

    def thread_by_root(self, root_message_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM owner_threads WHERE root_message_id=?", (root_message_id,)).fetchone()

    def thread_by_thread_id(self, thread_id: str) -> sqlite3.Row | None:
        if not thread_id:
            return None
        return self.conn.execute("SELECT * FROM owner_threads WHERE thread_id=? ORDER BY created_at LIMIT 1",
                                 (thread_id,)).fetchone()

    def add_thread(self, root_message_id: str, topic_id: str, key: str, norm_key: str, title: str, source: str,
                   thread_id: str = "") -> bool:
        """记一个话题；开头那条已经记过、或同一会话同一把钥匙已经有话题时不动，返回 False。"""
        ts = now_iso()
        with self.tx() as c:
            return c.execute(
                """INSERT OR IGNORE INTO owner_threads(root_message_id,thread_id,topic_id,key,norm_key,title,source,
                       created_at,last_post_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                (root_message_id, thread_id or None, topic_id, key or None, norm_key or None, title, source, ts, ts),
            ).rowcount > 0

    def set_thread_id(self, root_message_id: str, thread_id: str) -> bool:
        """补上话题编号（只补空的，已有的不改）。返回是否补了。"""
        if not thread_id:
            return False
        with self.tx() as c:
            return c.execute("UPDATE owner_threads SET thread_id=? WHERE root_message_id=? AND thread_id IS NULL",
                             (thread_id, root_message_id)).rowcount > 0

    def touch_thread(self, root_message_id: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE owner_threads SET last_post_at=? WHERE root_message_id=?", (now_iso(), root_message_id))

    def get_send(self, idem_key: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM sends WHERE idem_key=?", (idem_key,)).fetchone()

    def send_by_message(self, message_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM sends WHERE message_id=? ORDER BY sent_at LIMIT 1",
                                 (message_id,)).fetchone()

    def sends_in_thread(self, root_message_id: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM sends WHERE root_message_id=? ORDER BY sent_at, rowid",
                                 (root_message_id,)).fetchall()

    def add_send(self, idem_key: str, kind: str, topic_id: str, message_id: str, text: str, *,
                 root_message_id: str = "", key: str = "", norm_key: str = "", target_message_id: str = "",
                 identity: str | None = None) -> None:
        with self.tx() as c:
            c.execute(
                """INSERT OR IGNORE INTO sends(idem_key,kind,topic_id,message_id,root_message_id,key,norm_key,
                       target_message_id,text,sent_at,identity) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (idem_key, kind, topic_id, message_id, root_message_id or None, key or None, norm_key or None,
                 target_message_id or None, text, now_iso(), identity))

    def set_send_sources_origin(self, message_id: str, origin: str) -> None:
        """ask 发出时定下的来源口径（7.36）：记了以后补来源不再动它，没记来源也是定过的。"""
        with self.tx() as c:
            c.execute("UPDATE sends SET sources_origin=? WHERE message_id=? AND kind='ask'", (origin, message_id))

    def set_send_floor(self, message_id: str, floor: str) -> None:
        """ask 带的 --floor（7.61）：只写还空着的（重试不改第一次记下的那句）；floor 为空不写。"""
        if not floor:
            return
        with self.tx() as c:
            c.execute("UPDATE sends SET floor=? WHERE message_id=? AND kind='ask' AND COALESCE(floor,'')=''",
                      (floor, message_id))

    def thread_messages_in_ledger(self, thread_id: str, root_message_id: str = "") -> list[sqlite3.Row]:
        """账本里这个话题的消息（含开头那条），按发送时间排。只读。"""
        return self.conn.execute(
            """SELECT * FROM messages WHERE (? != '' AND thread_id=?) OR (? != '' AND message_id=?)
               ORDER BY create_time, first_seen, message_id""",
            (thread_id or "", thread_id or "", root_message_id or "", root_message_id or "")).fetchall()

    def fill_thread_fields(self, message_id: str, thread_id: str, thread_position: str) -> bool:
        """账本里这条消息后来有了话题（开头那条被再次拉到时带上 thread_id）：只补空的，已有的不改。"""
        if not thread_id:
            return False
        with self.tx() as c:
            return c.execute("UPDATE messages SET thread_id=?, thread_position=? WHERE message_id=? "
                             "AND (thread_id IS NULL OR thread_id='')",
                             (thread_id, thread_position or None, message_id)).rowcount > 0

    def root_candidates(self, chat_id: str) -> list[str]:
        """话题开头那条的候选（7.22）：sheepdog 在这个聊天里发过、还没记 thread_id 的消息——话题表里没有 thread_id 的
        开头那条，加上这个聊天里（保留期内）的「需要你定」和机器人发给主人的消息（ask / report 当场记的也算）。
        排除：账本里已经知道属于别的话题的、发进话题里的那些（不是开头）。新的在前。"""
        rows = self.conn.execute(
            """SELECT mid, MAX(at) AS at FROM (
                   SELECT root_message_id AS mid, created_at AS at FROM owner_threads WHERE thread_id IS NULL
                   UNION ALL
                   SELECT message_id, asked_at FROM escalations WHERE chat_id=?
                   UNION ALL
                   SELECT b.message_id, b.sent_at FROM bot_outbox b
                     LEFT JOIN messages m ON m.message_id = b.message_id
                     LEFT JOIN sends s ON s.message_id = b.message_id
                    WHERE m.chat_id=? OR s.kind IN ('ask','report'))
               GROUP BY mid ORDER BY at DESC""", (chat_id, chat_id)).fetchall()
        out = []
        for r in rows:
            mid = r["mid"]
            known = self.conn.execute("SELECT thread_id FROM messages WHERE message_id=?", (mid,)).fetchone()
            if known is not None and known["thread_id"]:
                continue
            thr = self.thread_by_root(mid)
            if thr is not None and thr["thread_id"]:
                continue
            s = self.send_by_message(mid)
            if s is not None and s["root_message_id"] and s["root_message_id"] != mid:
                continue
            out.append(mid)
        return out

    def add_seen_before_send(self, topic_id: str, message_id: str, target_message_id: str) -> bool:
        """记「发送前已看到」；同一会话同一条已经记过就不动（返回 False）。"""
        with self.tx() as c:
            return c.execute("INSERT OR IGNORE INTO seen_before_send(topic_id,message_id,target_message_id,seen_at) "
                             "VALUES(?,?,?,?)", (topic_id, message_id, target_message_id, now_iso())).rowcount > 0

    def seen_before_send_ids(self, topic_id: str, message_ids: list[str]) -> set[str]:
        if not message_ids:
            return set()
        marks = ",".join("?" * len(message_ids))
        return {r[0] for r in self.conn.execute(
            f"SELECT message_id FROM seen_before_send WHERE topic_id=? AND message_id IN ({marks})",
            (topic_id, *message_ids))}

    # ---------- actions ----------
    # ---------- 回音账本（7.52） ----------
    def echo_record(self, message_id: str, chat_id: str, kind: str, said_at: str, stage: str, topic_id: str,
                    reason: str = "", at: str = "") -> None:
        """记（或重记）一笔欠账。同一条消息再记（主人改口 / 撤回了已送出的那句）：重新开账，阶段、报过的都清掉。
        at：记账时间（牧羊犬的「现在」），默认本机现在。"""
        ts = at or now_iso()
        with self.tx() as c:
            c.execute(
                """INSERT INTO owner_echoes(message_id,chat_id,kind,said_at,stage,topic_id,reason,recorded_at,stage_at)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(message_id) DO UPDATE SET kind=excluded.kind, stage=excluded.stage,
                       topic_id=excluded.topic_id, reason=excluded.reason, recorded_at=excluded.recorded_at,
                       stage_at=excluded.stage_at, delivered_at=NULL, batch_id=NULL, priority=0, settled_at=NULL,
                       settled_by=NULL, alerted='', relay_batch=NULL, relay_at=NULL, relay_status=NULL,
                       relay_summary=NULL""",
                (message_id, chat_id, kind, said_at, stage, topic_id or None, reason or None, ts, ts))

    def echo_get(self, message_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM owner_echoes WHERE message_id=?", (message_id,)).fetchone()

    def echo_open(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM owner_echoes WHERE settled_at IS NULL ORDER BY said_at, message_id"
                                 ).fetchall()

    def echo_update(self, message_id: str, **fields) -> None:
        """改一笔欠账。等着带回的那份回执（ECHO_RELAY_FIELDS）这里只能清空，绑定走 echo_bind_relay。"""
        if not fields:
            return
        if bound := [k for k in ECHO_RELAY_FIELDS if fields.get(k) is not None]:
            raise ValueError(f"回执带回只能经 echo_bind_relay 绑一次，echo_update 不能写 {', '.join(bound)}")
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE owner_echoes SET {cols} WHERE message_id=?", (*fields.values(), message_id))

    def echo_bind_relay(self, message_id: str, batch_id: str, at: str, status: str, summary: str) -> bool:
        """把一份回执绑到这笔欠账上，等着带回（7.52）。只写还没绑、还没还上的：绑上了，后来的回执改不了它
        （10-17 04:59：推迟期间每来一份回执都改记一次，卡片正文成了另一件事）。返回这次绑上了没有。"""
        with self.tx() as c:
            return c.execute(
                """UPDATE owner_echoes SET relay_batch=?, relay_at=?, relay_status=?, relay_summary=?
                   WHERE message_id=? AND settled_at IS NULL AND relay_batch IS NULL""",
                (batch_id, at, status, summary, message_id)).rowcount == 1

    def echo_settle(self, message_ids: list[str], by: str) -> None:
        ts = now_iso()
        with self.tx() as c:
            c.executemany("UPDATE owner_echoes SET stage='settled', settled_at=?, settled_by=? "
                          "WHERE message_id=? AND settled_at IS NULL", [(ts, by, i) for i in message_ids])

    def echo_mark_alerted(self, message_ids: list[str], stage: str) -> None:
        with self.tx() as c:
            c.executemany("UPDATE owner_echoes SET alerted = CASE WHEN COALESCE(alerted,'')='' THEN ? "
                          "ELSE alerted || ',' || ? END WHERE message_id=?", [(stage, stage, i) for i in message_ids])

    def owner_sends_since(self, topic_id: str, since_iso: str) -> int:
        """这个会话在 since 之后（严格晚于，口径同 sends_after）经 sheepdog 给主人发过几条 ask / report（7.52：「回主人」
        只认这两种，回群里的同事 reply 不算）。"""
        try:
            since = datetime.fromisoformat(since_iso or "")
        except ValueError:
            return 0
        n = 0
        for r in self.conn.execute("SELECT sent_at FROM sends WHERE topic_id=? AND kind IN ('ask','report')", (topic_id,)):
            try:
                at = datetime.fromisoformat(r["sent_at"] or "")
            except ValueError:
                continue
            if at.tzinfo is not None and since.tzinfo is not None and at > since:
                n += 1
        return n

    # ---------- 群话题归属（7.45） ----------
    def group_thread(self, thread_id: str) -> sqlite3.Row | None:
        if not thread_id:
            return None
        return self.conn.execute("SELECT * FROM group_threads WHERE thread_id=?", (thread_id,)).fetchone()

    def add_group_thread(self, thread_id: str, chat_id: str, topic_id: str, source: str, root_message_id: str,
                         root_summary: str, last_activity_at: str) -> None:
        """记下（或改记）一个群话题的归属。"""
        with self.tx() as c:
            c.execute(
                """INSERT INTO group_threads(thread_id,chat_id,topic_id,source,root_message_id,root_summary,created_at,
                       last_activity_at) VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(thread_id) DO UPDATE SET topic_id=excluded.topic_id, source=excluded.source,
                       root_message_id=COALESCE(excluded.root_message_id, group_threads.root_message_id),
                       root_summary=COALESCE(excluded.root_summary, group_threads.root_summary),
                       last_activity_at=MAX(group_threads.last_activity_at, excluded.last_activity_at)""",
                (thread_id, chat_id, topic_id, source, root_message_id or None, root_summary or None, now_iso(),
                 last_activity_at or now_iso()))

    def touch_group_thread(self, thread_id: str, at: str) -> bool:
        """话题里又有人说话：最近活动时间往后挪（只往后不往前）。没记过这个话题返回 False。"""
        if not thread_id or not at:
            return False
        with self.tx() as c:
            return c.execute("UPDATE group_threads SET last_activity_at=? WHERE thread_id=? AND last_activity_at < ?",
                             (at, thread_id, at)).rowcount > 0

    def repoint_group_thread(self, thread_id: str, root_message_id: str, from_topics: set[str], topic_id: str) -> bool:
        """话题里的消息（或开头那条）被转交（forward）：原来归 from_topics 之一的话题改归 topic_id（source = forwarded）。"""
        if not (thread_id or root_message_id) or not from_topics:
            return False
        with self.tx() as c:
            return c.execute(
                "UPDATE group_threads SET topic_id=?, source='forwarded' WHERE (thread_id=? OR root_message_id=?) "
                "AND topic_id IN (%s) AND topic_id != ?" % ",".join("?" * len(from_topics)),
                (topic_id, thread_id or "", root_message_id or "", *sorted(from_topics), topic_id)).rowcount > 0

    def thread_last_activity(self, thread_id: str, exclude_message_id: str = "") -> str:
        """账本里这个话题（含已知的开头那条）最近一条消息的时间；没有是空串。"""
        r = self.conn.execute("SELECT MAX(create_time) FROM messages WHERE thread_id=? AND message_id != ?",
                              (thread_id, exclude_message_id or "")).fetchone()
        return (r[0] or "") if r else ""

    def group_root_candidates(self, chat_id: str, before_iso: str, reply_since_iso: str, limit: int = 50) -> list[str]:
        """群话题开头那条的候选（7.45）：这个群里（保留期内）投出去过的消息——送到过某个会话的、sheepdog 替会话发的——
        账本里还没记话题编号、不晚于这条回复、也没在这个话题已经有回复以后 mget 核过的。新的在前，最多 limit 条。"""
        rows = self.conn.execute(
            """SELECT m.message_id FROM messages m
               WHERE m.chat_id=? AND COALESCE(m.thread_id,'')='' AND m.create_time <= ?
                 AND ((m.route='dispatch' AND m.dispatch_state IN ('delivered','acked') AND m.reason != 'owner_context')
                      OR m.message_id IN (SELECT message_id FROM sends))
                 AND NOT EXISTS (SELECT 1 FROM thread_root_checks k WHERE k.message_id=m.message_id AND k.checked_at >= ?)
               ORDER BY m.create_time DESC, m.first_seen DESC LIMIT ?""",
            (chat_id, before_iso, reply_since_iso, limit)).fetchall()
        return [r[0] for r in rows]

    def mark_root_checked(self, message_ids: list[str]) -> None:
        ts = now_iso()
        with self.tx() as c:
            c.executemany("INSERT INTO thread_root_checks(message_id,checked_at) VALUES(?,?) "
                          "ON CONFLICT(message_id) DO UPDATE SET checked_at=excluded.checked_at",
                          [(i, ts) for i in message_ids])

    def thread_first_seen(self, thread_id: str) -> str:
        """账本里最早见到这个话题的回复是什么时候（first_seen，本地时间）：在那之后 mget 过、没有话题的候选不可能是它的开头。"""
        r = self.conn.execute("SELECT MIN(first_seen) FROM messages WHERE thread_id=?", (thread_id,)).fetchone()
        return (r[0] or "") if r else ""

    def add_action(self, kind: str, args: dict, requested_by: str) -> int:
        with self.tx() as c:
            cur = c.execute("INSERT INTO actions(kind,args_json,requested_by,requested_at) VALUES(?,?,?,?)",
                            (kind, json.dumps(args, ensure_ascii=False), requested_by, now_iso()))
            return int(cur.lastrowid)

    def pending_actions(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM actions WHERE done_at IS NULL ORDER BY id").fetchall()

    def finish_action(self, action_id: int, result: str = "", error: str = "") -> None:
        with self.tx() as c:
            c.execute("UPDATE actions SET done_at=?, result=?, error=? WHERE id=?",
                      (now_iso(), result or None, error or None, action_id))

    def list_actions(self, include_done: bool = False, recent_done: int = 10) -> list[sqlite3.Row]:
        if include_done:
            return self.conn.execute("SELECT * FROM actions ORDER BY id DESC").fetchall()
        pending = self.conn.execute("SELECT * FROM actions WHERE done_at IS NULL ORDER BY id DESC").fetchall()
        done = self.conn.execute("SELECT * FROM actions WHERE done_at IS NOT NULL ORDER BY id DESC LIMIT ?",
                                 (recent_done,)).fetchall()
        return list(pending) + list(done)

    # ---------- acks ----------
    def get_ack(self, message_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM acks WHERE message_id=?", (message_id,)).fetchone()

    def add_ack(self, message_id: str, chat_id: str, chat_type: str, sender_id: str, reason: str,
                reaction_id: str | None, error: str | None, identity: str = "user") -> None:
        with self.tx() as c:
            c.execute("""INSERT OR IGNORE INTO acks(message_id,chat_id,chat_type,sender_id,reason,identity,reaction_id,
                                                    added_at,error)
                         VALUES(?,?,?,?,?,?,?,?,?)""",
                      (message_id, chat_id, chat_type, sender_id, reason, identity, reaction_id, now_iso(), error))

    def removable_acks(self, chat_id: str) -> list[sqlite3.Row]:
        """同一聊天里点成功、还没撤、撤的时候也没失败过的表情（失败过的不再重试）。"""
        return self.conn.execute(
            """SELECT * FROM acks WHERE chat_id=? AND reaction_id IS NOT NULL AND removed_at IS NULL AND error IS NULL
                 AND (identity IS NULL OR identity='user')
               ORDER BY added_at""", (chat_id,)).fetchall()

    def mark_ack_removed(self, message_id: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE acks SET removed_at=? WHERE message_id=?", (now_iso(), message_id))

    def set_ack_error(self, message_id: str, error: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE acks SET error=? WHERE message_id=?", (error, message_id))

    def list_acks(self, open_only: bool = False, limit: int = 200) -> list[sqlite3.Row]:
        where = "WHERE a.reaction_id IS NOT NULL AND a.removed_at IS NULL" if open_only else ""
        return self.conn.execute(
            f"""SELECT a.*, m.chat_name, m.sender_name, m.content FROM acks a
                LEFT JOIN messages m ON m.message_id = a.message_id {where}
                ORDER BY a.added_at DESC LIMIT ?""", (limit,)).fetchall()

    # ---------- watches ----------
    def add_watch(self, topic_id: str, person_id: str, chat_id: str, note: str) -> int:
        with self.tx() as c:
            cur = c.execute("INSERT INTO watches(topic_id,person_id,chat_id,note,started_at) VALUES(?,?,?,?,?)",
                            (topic_id, person_id, chat_id or None, note, now_iso()))
            return int(cur.lastrowid)

    def get_watch(self, watch_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM watches WHERE id=?", (watch_id,)).fetchone()

    def open_watches(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM watches WHERE closed_at IS NULL ORDER BY started_at, id").fetchall()

    def list_watches(self, include_closed: bool = False, limit: int = 100) -> list[sqlite3.Row]:
        where = "" if include_closed else "WHERE closed_at IS NULL"
        return self.conn.execute(f"SELECT * FROM watches {where} ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def match_watch(self, person_id: str, chat_id: str) -> sqlite3.Row | None:
        """同一人有多条有效等待时取最新的一条。"""
        if not person_id:
            return None
        return self.conn.execute(
            """SELECT * FROM watches WHERE closed_at IS NULL AND person_id=? AND (chat_id IS NULL OR chat_id=?)
               ORDER BY started_at DESC, id DESC LIMIT 1""",
            (person_id, chat_id),
        ).fetchone()

    def update_watch(self, watch_id: int, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE watches SET {cols} WHERE id=?", (*fields.values(), watch_id))

    def mark_watch_replied(self, watch_id: int, message_id: str) -> None:
        self.update_watch(watch_id, status="replied", last_reply_at=now_iso(), last_reply_message_id=message_id)

    def reset_watch_waiting(self, watch_id: int) -> None:
        """回复没被确认：回到 waiting，提醒档位清零，从最后一次回复时间重新计时。"""
        self.update_watch(watch_id, status="waiting", nudged_15_at=None, nudged_30_at=None)

    def close_watch(self, watch_id: int, reason: str) -> None:
        self.update_watch(watch_id, closed_at=now_iso(), close_reason=reason)

    # ---------- runs（7.18） ----------
    RUN_ACTIVE = ("running", "retry_wait")

    def add_run(self, batch_id: str, topic_id: str, host: str, conversation_id: str, kind: str, title: str,
                model: str, effort: str) -> None:
        with self.tx() as c:
            c.execute("""INSERT OR REPLACE INTO runs(batch_id,topic_id,host,conversation_id,state,attempts,kind,title,
                                                    model,effort,started_at)
                         VALUES(?,?,?,?,'running',0,?,?,?,?,?)""",
                      (batch_id, topic_id, host, conversation_id or None, kind, title, model, effort, now_iso()))

    def get_run(self, batch_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM runs WHERE batch_id=?", (batch_id,)).fetchone()

    def update_run(self, batch_id: str, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE runs SET {cols} WHERE batch_id=?", (*fields.values(), batch_id))

    def runs_in_state(self, host: str, *states: str) -> list[sqlite3.Row]:
        marks = ",".join("?" * len(states))
        return self.conn.execute(f"SELECT * FROM runs WHERE host=? AND state IN ({marks}) ORDER BY started_at",
                                 (host, *states)).fetchall()

    def active_runs_for(self, conversation_id: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM runs WHERE conversation_id=? AND state IN ('running','retry_wait')",
                                 (conversation_id,)).fetchall()

    def runs_for_conversation(self, conversation_id: str, limit: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM runs WHERE conversation_id=? ORDER BY started_at DESC, rowid DESC "
                                 "LIMIT ?", (conversation_id, limit)).fetchall()

    def host_of_conversation(self, conversation_id: str) -> str:
        """这个会话编号属于哪个宿主：先看 topics，再看 runs（换宿主后旧编号只留在 runs 里）；都没有就是空串。"""
        r = self.conn.execute("SELECT host FROM topics WHERE conversation_id=? AND host IS NOT NULL LIMIT 1",
                              (conversation_id,)).fetchone()
        if r is None:
            r = self.conn.execute("SELECT host FROM runs WHERE conversation_id=? ORDER BY started_at DESC LIMIT 1",
                                  (conversation_id,)).fetchone()
        return r[0] if r is not None else ""

    def latest_run_for_topic(self, topic_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM runs WHERE topic_id=? ORDER BY started_at DESC, rowid DESC LIMIT 1",
                                 (topic_id,)).fetchone()

    # ---------- 会话记录的增量读与命令审计（7.19） ----------
    def get_cursor(self, topic_id: str, purpose: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM transcript_cursors WHERE topic_id=? AND purpose=?",
                                 (topic_id, purpose)).fetchone()

    def set_cursor(self, topic_id: str, purpose: str, conversation_id: str, path: str, offset: int) -> None:
        with self.tx() as c:
            c.execute("""INSERT INTO transcript_cursors(topic_id,purpose,conversation_id,path,byte_offset,updated_at)
                         VALUES(?,?,?,?,?,?) ON CONFLICT(topic_id,purpose) DO UPDATE SET
                         conversation_id=excluded.conversation_id, path=excluded.path, byte_offset=excluded.byte_offset,
                         updated_at=excluded.updated_at""",
                      (topic_id, purpose, conversation_id, path, offset, now_iso()))

    def add_audit_hit(self, topic_id: str, conversation_id: str, step: str, command: str, pattern: str,
                      ran_at: str) -> bool:
        """记一条命中；同一会话同一 step 已记过就返回 False。"""
        with self.tx() as c:
            cur = c.execute("""INSERT OR IGNORE INTO audit_hits(topic_id,conversation_id,step,command,pattern,ran_at,
                               detected_at) VALUES(?,?,?,?,?,?,?)""",
                            (topic_id, conversation_id, step, command, pattern, ran_at, now_iso()))
            return cur.rowcount > 0

    def pending_audit_hits(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM audit_hits WHERE notified_at IS NULL ORDER BY id").fetchall()

    def handled_audit_hits(self, topic_id: str, limit: int = 500) -> list[sqlite3.Row]:
        """这个会话已经处理过（报了，或并进了之前的报警）的命中，新的在前。"""
        return self.conn.execute("SELECT * FROM audit_hits WHERE topic_id=? AND notified_at IS NOT NULL "
                                 "ORDER BY id DESC LIMIT ?", (topic_id, limit)).fetchall()

    def mark_audit_notified(self, ids: list[int], alert_id: int | None = None, merged: bool = False) -> None:
        """记下这些命中已经处理：alert_id 是它们属于的那次报警；merged = 并进了之前的报警、没有单独报。"""
        ts = now_iso()
        with self.tx() as c:
            c.executemany("UPDATE audit_hits SET notified_at=?, alert_id=?, merged=? WHERE id=?",
                          [(ts, alert_id, int(merged), i) for i in ids])

    # ---------- 给主人的通知 ----------
    def add_owner_notice(self, kind: str, topic_id: str, body: bytes, idem_key: str, reply_to: str = "",
                         in_thread: bool = False) -> int:
        """落账本（待发）：正文字节、防重编号、回复哪一条此后不变。"""
        ts = now_iso()
        with self.tx() as c:
            cur = c.execute("INSERT INTO owner_notices(kind,topic_id,body,idem_key,state,created_at,window_at,next_at,"
                            "reply_to,in_thread) VALUES(?,?,?,?,'pending',?,?,?,?,?)",
                            (kind, topic_id or None, body, idem_key, ts, ts, ts, reply_to or None, int(bool(in_thread))))
            return int(cur.lastrowid)

    def owner_notice_by_key(self, idem_key: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM owner_notices WHERE idem_key=?", (idem_key,)).fetchone()

    def get_owner_notice(self, notice_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM owner_notices WHERE id=?", (notice_id,)).fetchone()

    def pending_owner_notices(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM owner_notices WHERE state='pending' ORDER BY id").fetchall()

    def unsent_owner_notices(self) -> list[sqlite3.Row]:
        """没发出去、也没人处理的：还在重试的，和标了放弃、没 --ack 的。"""
        return self.conn.execute("SELECT * FROM owner_notices WHERE state='pending' "
                                 "OR (state='abandoned' AND acked_at IS NULL) ORDER BY id").fetchall()

    def list_owner_notices(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM owner_notices ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def owner_notice_start(self, notice_id: int) -> None:
        """开始发：先记下「正在发」（提交了再调发送），有结果后由 sent / failed 清掉。"""
        ts = now_iso()
        with self.tx() as c:
            c.execute("UPDATE owner_notices SET inflight_at=?, attempt_at=?, attempts=COALESCE(attempts,0)+1 "
                      "WHERE id=? AND state='pending'", (ts, ts, notice_id))

    def owner_notice_sent(self, notice_id: int) -> None:
        with self.tx() as c:
            c.execute("UPDATE owner_notices SET state='sent', sent_at=?, inflight_at=NULL, next_at=NULL WHERE id=?",
                      (now_iso(), notice_id))

    def owner_notice_failed(self, notice_id: int, error: str, next_at: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE owner_notices SET inflight_at=NULL, last_error=?, next_at=? WHERE id=?",
                      (error, next_at, notice_id))

    def owner_notice_abandon(self, notice_id: int, error: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE owner_notices SET state='abandoned', closed_at=?, inflight_at=NULL, next_at=NULL, "
                      "last_error=? WHERE id=?", (now_iso(), error, notice_id))

    def owner_notice_retry(self, notice_id: int) -> bool:
        """人工重发：没发出去的（放弃的、还在重试的）回到待发，2 小时从现在重新算、马上可以发。已发的不动。"""
        ts = now_iso()
        with self.tx() as c:
            cur = c.execute("UPDATE owner_notices SET state='pending', window_at=?, next_at=?, attempts=0, "
                            "inflight_at=NULL, closed_at=NULL, acked_at=NULL "
                            "WHERE id=? AND state IN ('pending','abandoned')", (ts, ts, notice_id))
            return cur.rowcount > 0

    def owner_notice_ack(self, notice_id: int) -> bool:
        """人工确认已处理：不再提示、不再重发（还在重试的也停下，记成放弃）。已发的不动。"""
        ts = now_iso()
        with self.tx() as c:
            cur = c.execute("UPDATE owner_notices SET state='abandoned', closed_at=COALESCE(closed_at,?), acked_at=?, "
                            "inflight_at=NULL, next_at=NULL WHERE id=? AND state IN ('pending','abandoned')",
                            (ts, ts, notice_id))
            return cur.rowcount > 0


    # ---------- 在岗表（7.20） ----------
    def get_card(self, topic_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM duty_cards WHERE topic_id=?", (topic_id,)).fetchone()

    def list_cards(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM duty_cards").fetchall()

    def upsert_card(self, topic_id: str, **fields) -> None:
        fields["updated_at"] = now_iso()
        cols = ["topic_id", *fields]
        sets = ", ".join(f"{k}=excluded.{k}" for k in fields)
        with self.tx() as c:
            c.execute(f"INSERT INTO duty_cards({','.join(cols)}) VALUES({','.join('?' * len(cols))}) "
                      f"ON CONFLICT(topic_id) DO UPDATE SET {sets}", (topic_id, *fields.values()))

    def get_work(self, topic_id: str, norm_key: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM work_items WHERE topic_id=? AND norm_key=?",
                                 (topic_id, norm_key)).fetchone()

    def work_by_ref(self, source_ref: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM work_items WHERE source_ref=? ORDER BY id DESC LIMIT 1",
                                 (source_ref,)).fetchone()

    def add_work(self, topic_id: str, key: str, norm_key: str, state: str, source: str, declared: bool,
                 **fields) -> int:
        ts = now_iso()
        cols = ["topic_id", "key", "norm_key", "state", "source", "declared", "created_at", "updated_at", *fields]
        vals = [topic_id, key, norm_key, state, source, int(declared), ts, ts, *fields.values()]
        with self.tx() as c:
            cur = c.execute(f"INSERT INTO work_items({','.join(cols)}) VALUES({','.join('?' * len(cols))})", vals)
            return int(cur.lastrowid)

    def update_work(self, work_id: int, **fields) -> None:
        fields["updated_at"] = now_iso()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE work_items SET {cols} WHERE id=?", (*fields.values(), work_id))

    def open_work(self, topic_id: str | None = None) -> list[sqlite3.Row]:
        sql, args = "SELECT * FROM work_items WHERE closed_at IS NULL", []
        if topic_id:
            sql += " AND topic_id=?"
            args.append(topic_id)
        return self.conn.execute(sql + " ORDER BY updated_at DESC, id DESC", args).fetchall()

    def open_work_by_key(self, norm_key: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM work_items WHERE norm_key=? AND closed_at IS NULL ORDER BY id",
                                 (norm_key,)).fetchall()

    def open_auto_work(self, source: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM work_items WHERE source=? AND closed_at IS NULL ORDER BY id",
                                 (source,)).fetchall()

    def closed_work(self, topic_id: str, limit: int = 3) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM work_items WHERE topic_id=? AND closed_at IS NOT NULL "
                                 "ORDER BY closed_at DESC, id DESC LIMIT ?", (topic_id, limit)).fetchall()

    def add_mail(self, from_topic: str, to_topic: str, key: str, body: str) -> int:
        with self.tx() as c:
            cur = c.execute("INSERT INTO mailbox(from_topic,to_topic,key,body,created_at) VALUES(?,?,?,?,?)",
                            (from_topic, to_topic, key or None, body, now_iso()))
            return int(cur.lastrowid)

    def set_mail_message(self, mail_id: int, message_id: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE mailbox SET message_id=? WHERE id=?", (message_id, mail_id))

    def get_mail(self, mail_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            """SELECT b.*, m.dispatch_state, m.batch_id FROM mailbox b LEFT JOIN messages m ON m.message_id = b.message_id
               WHERE b.id=?""", (mail_id,)).fetchone()

    def mails_between(self, a: str, b: str, since_iso: str) -> int:
        """since 之后 a、b 之间（两个方向）的留言条数。"""
        return self.conn.execute(
            """SELECT COUNT(*) FROM mailbox WHERE created_at >= ?
               AND ((from_topic=? AND to_topic=?) OR (from_topic=? AND to_topic=?))""",
            (since_iso, a, b, b, a)).fetchone()[0]

    def unread_mail(self, to_topic: str) -> list[sqlite3.Row]:
        """给这个会话、还没被回执确认的留言（没送出去的、送出去还没回执的）。"""
        return self.conn.execute(
            """SELECT b.*, m.dispatch_state FROM mailbox b LEFT JOIN messages m ON m.message_id = b.message_id
               WHERE b.to_topic=? AND (m.dispatch_state IS NULL OR m.dispatch_state != 'acked') ORDER BY b.id""",
            (to_topic,)).fetchall()

    def add_history(self, topic_id: str, kind: str, text: str, at: str = "") -> None:
        with self.tx() as c:
            c.execute("INSERT INTO duty_history(topic_id,kind,text,at) VALUES(?,?,?,?)",
                      (topic_id, kind, text, at or now_iso()))

    def history(self, topic_id: str, kind: str, limit: int = 5) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM duty_history WHERE topic_id=? AND kind=? ORDER BY id DESC LIMIT ?",
                                 (topic_id, kind, limit)).fetchall()

    # ---------- 路由事实（7.20 第二轮） ----------
    def add_route(self, topic_id: str, key: str, norm_key: str, title: str, message_id: str, at: str) -> bool:
        """记一条路由事实：带这把钥匙的消息送到了这个会话。同一条消息（重投）不重复计数。返回是不是新的一条。"""
        with self.tx() as c:
            fresh = c.execute("INSERT OR IGNORE INTO duty_route_seen(topic_id,norm_key,message_id,at) VALUES(?,?,?,?)",
                              (topic_id, norm_key, message_id, at)).rowcount > 0
            if not fresh:
                return False
            c.execute(
                """INSERT INTO duty_routes(topic_id,norm_key,key,title,first_at,last_at,count,last_message_id)
                   VALUES(?,?,?,?,?,?,1,?)
                   ON CONFLICT(topic_id,norm_key) DO UPDATE SET key=excluded.key, title=excluded.title,
                       last_at=excluded.last_at, count=count+1, last_message_id=excluded.last_message_id""",
                (topic_id, norm_key, key, title, at, at, message_id))
            return True

    def close_route(self, topic_id: str, norm_key: str, reason: str, at: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE duty_routes SET closed_at=?, closed_reason=? WHERE topic_id=? AND norm_key=?",
                      (at, reason, topic_id, norm_key))

    def active_routes(self, since_iso: str, topic_id: str | None = None, norm_key: str | None = None) -> list[sqlite3.Row]:
        """还算数的路由事实：since 之后有消息送到，且送到在转交 / 自报做完之后。新的在前。"""
        sql = ("SELECT * FROM duty_routes WHERE last_at >= ? AND (closed_at IS NULL OR last_at > closed_at)")
        args: list = [since_iso]
        if topic_id is not None:
            sql += " AND topic_id=?"
            args.append(topic_id)
        if norm_key is not None:
            sql += " AND norm_key=?"
            args.append(norm_key)
        return self.conn.execute(sql + " ORDER BY last_at DESC, topic_id", args).fetchall()

    def latest_batch_id(self, topic_id: str, skip_prefix: str = "") -> str:
        """这个会话最近一批的批次号（按送出时间，同一秒按记录先后）；没有就是空串。
        skip_prefix：跳过这个前缀的批次（主人插话批次 p…，7.38 第 8 条）。"""
        sql = "SELECT batch_id FROM dispatches WHERE topic_id=?"
        args: list = [topic_id]
        if skip_prefix:
            sql += " AND substr(batch_id, 1, ?) != ?"
            args += [len(skip_prefix), skip_prefix]
        r = self.conn.execute(sql + " ORDER BY sent_at DESC, rowid DESC LIMIT 1", args).fetchone()
        return r[0] if r is not None else ""

    def recent_dispatches(self, topic_id: str, limit: int = 30) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM dispatches WHERE topic_id=? ORDER BY sent_at DESC LIMIT ?",
                                 (topic_id, limit)).fetchall()

    def person_name(self, person_id: str) -> str:
        """账本里这个人最近一次发言用的名字；没有就是空串。"""
        r = self.conn.execute("SELECT sender_name FROM messages WHERE sender_id=? AND sender_name != '' "
                              "ORDER BY create_time DESC LIMIT 1", (person_id,)).fetchone()
        return r[0] if r else ""

    # ---------- 跟踪事项（7.51） ----------
    def add_item(self, *, title: str, owner_topic: str, opened_by: str, origin: str, chat_id: str,
                 dm_root_message_id: str = "", related_item: int | None = None, root_message_id: str = "",
                 thread_id: str = "") -> int:
        ts = now_iso()
        with self.tx() as c:
            cur = c.execute(
                """INSERT INTO tracked_items(title,owner_topic,opened_by,origin,state,chat_id,root_message_id,thread_id,
                       dm_root_message_id,related_item,opened_at,last_activity_at) VALUES(?,?,?,?,'active',?,?,?,?,?,?,?)""",
                (title, owner_topic or None, opened_by or None, origin, chat_id or None, root_message_id or None,
                 thread_id or None, dm_root_message_id or None, related_item, ts, ts))
            return int(cur.lastrowid)

    def get_item(self, item_no: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM tracked_items WHERE item_no=?", (item_no,)).fetchone()

    def item_by_root(self, message_id: str) -> sqlite3.Row | None:
        if not message_id:
            return None
        return self.conn.execute("SELECT * FROM tracked_items WHERE root_message_id=?", (message_id,)).fetchone()

    def item_by_thread(self, thread_id: str) -> sqlite3.Row | None:
        if not thread_id:
            return None
        return self.conn.execute("SELECT * FROM tracked_items WHERE thread_id=? ORDER BY item_no DESC LIMIT 1",
                                 (thread_id,)).fetchone()

    def item_by_dm_root(self, message_id: str) -> sqlite3.Row | None:
        if not message_id:
            return None
        return self.conn.execute("SELECT * FROM tracked_items WHERE dm_root_message_id=? ORDER BY item_no DESC LIMIT 1",
                                 (message_id,)).fetchone()

    def list_items(self, states: tuple[str, ...]) -> list[sqlite3.Row]:
        marks = ",".join("?" * len(states))
        return self.conn.execute(f"SELECT * FROM tracked_items WHERE state IN ({marks}) ORDER BY item_no",
                                 states).fetchall()

    def update_item(self, item_no: int, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE tracked_items SET {cols} WHERE item_no=?", (*fields.values(), item_no))

    def delete_item(self, item_no: int) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM tracked_keys WHERE item_no=?", (item_no,))
            c.execute("DELETE FROM tracked_items WHERE item_no=?", (item_no,))

    def add_item_key(self, item_no: int, key: str, norm_key: str, kind: str, topic_id: str = "") -> bool:
        if not norm_key:
            return False
        with self.tx() as c:
            return c.execute("INSERT OR IGNORE INTO tracked_keys(norm_key,item_no,key,kind,topic_id,added_at) "
                             "VALUES(?,?,?,?,?,?)", (norm_key, item_no, key, kind, topic_id or None, now_iso())).rowcount > 0

    def item_keys(self, item_no: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM tracked_keys WHERE item_no=? ORDER BY added_at, rowid", (item_no,)).fetchall()

    def items_by_norm_key(self, norm_key: str) -> list[sqlite3.Row]:
        """这把钥匙（track_norm 以后）对应的事项，新的在前。"""
        return self.conn.execute(
            "SELECT i.* FROM tracked_items i JOIN tracked_keys k ON k.item_no = i.item_no WHERE k.norm_key=? "
            "ORDER BY i.item_no DESC", (norm_key,)).fetchall()

    # ---------- 羔羊（Subagent / Lamb，7.63） ----------
    LAMB_FIELDS = (
        "parent_topic_id", "kind", "task_title", "conversation_id", "pid", "state",
        "started_at", "last_act_at", "finished_at", "result_summary", "log_path", "reported_at",
        "report_msg_id",
    )

    def create_lamb(
        self,
        lamb_id: str,
        parent_topic_id: str = "",
        kind: str = "native_subagent",
        task_title: str = "",
        *,
        topic_id: str = "",
        title: str = "",
        conversation_id: str | None = None,
        pid: int | None = None,
        state: str = "running",
        started_at: str | None = None,
        last_act_at: str | None = None,
        finished_at: str | None = None,
        result_summary: str | None = None,
        log_path: str | None = None,
        reported_at: str | None = None,
        report_msg_id: str | None = None,
    ) -> dict:
        ptid = (parent_topic_id or topic_id).strip()
        ttitle = (task_title or title or lamb_id).strip()
        ts = started_at or now_iso()
        act = last_act_at if last_act_at is not None else ts
        cid = (conversation_id or "").strip() or None
        with self.tx() as c:
            c.execute(
                """INSERT INTO lambs(lamb_id, parent_topic_id, kind, task_title, conversation_id, pid, state,
                                     started_at, last_act_at, finished_at, result_summary, log_path, reported_at, report_msg_id)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(lamb_id) DO UPDATE SET
                       parent_topic_id=excluded.parent_topic_id,
                       kind=excluded.kind,
                       task_title=excluded.task_title,
                       conversation_id=COALESCE(excluded.conversation_id, lambs.conversation_id),
                       pid=COALESCE(excluded.pid, lambs.pid),
                       state=excluded.state,
                       last_act_at=excluded.last_act_at,
                       finished_at=COALESCE(excluded.finished_at, lambs.finished_at),
                       result_summary=COALESCE(excluded.result_summary, lambs.result_summary),
                       log_path=COALESCE(excluded.log_path, lambs.log_path),
                       report_msg_id=COALESCE(excluded.report_msg_id, lambs.report_msg_id)""",
                (lamb_id, ptid, kind, ttitle, cid, pid, state, ts, act, finished_at, result_summary, log_path, reported_at, report_msg_id),
            )
        return self.get_lamb(lamb_id) or {}

    register_lamb = create_lamb

    def get_lamb(self, lamb_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM lambs WHERE lamb_id=?", (lamb_id,)).fetchone()
        return dict(r) if r is not None else None

    def update_lamb(self, lamb_id: str, **fields) -> dict | None:
        allowed = {k: v for k, v in fields.items() if k in self.LAMB_FIELDS}
        if not allowed:
            return self.get_lamb(lamb_id)
        cols = ", ".join(f"{k}=?" for k in allowed)
        with self.tx() as c:
            c.execute(f"UPDATE lambs SET {cols} WHERE lamb_id=?", (*allowed.values(), lamb_id))
        return self.get_lamb(lamb_id)

    def get_active_lambs(self, parent_topic_id: str | None = None) -> list[dict]:
        sql = "SELECT * FROM lambs WHERE state='running'"
        args: list = []
        if parent_topic_id:
            sql += " AND parent_topic_id=?"
            args.append(parent_topic_id)
        rows = self.conn.execute(sql + " ORDER BY started_at ASC, rowid ASC, lamb_id ASC", args).fetchall()
        return [dict(r) for r in rows]

    def list_lambs(
        self,
        parent_topic_id: str | None = None,
        state: str | None = None,
        *,
        include_finished: bool = True,
    ) -> list[dict]:
        sql = "SELECT * FROM lambs WHERE 1=1"
        args: list = []
        if parent_topic_id:
            sql += " AND parent_topic_id=?"
            args.append(parent_topic_id)
        if state:
            sql += " AND state=?"
            args.append(state)
        elif not include_finished:
            sql += " AND state='running'"
        rows = self.conn.execute(sql + " ORDER BY started_at ASC, rowid ASC, lamb_id ASC", args).fetchall()
        return [dict(r) for r in rows]

    def find_lamb_by_cid(self, conversation_id: str) -> dict | None:
        cid = (conversation_id or "").strip()
        if not cid:
            return None
        r = self.conn.execute(
            "SELECT * FROM lambs WHERE conversation_id=? ORDER BY (state='running') DESC, started_at DESC, lamb_id DESC LIMIT 1",
            (cid,),
        ).fetchone()
        return dict(r) if r is not None else None

    get_lamb_by_conversation = find_lamb_by_cid

    def get_unreported_finished_lambs(self, parent_topic_id: str | None = None) -> list[dict]:
        sql = "SELECT * FROM lambs WHERE state IN ('done','failed','timeout','cancelled') AND (reported_at IS NULL OR reported_at='')"
        args: list = []
        if parent_topic_id:
            sql += " AND parent_topic_id=?"
            args.append(parent_topic_id)
        rows = self.conn.execute(sql + " ORDER BY COALESCE(finished_at, started_at) ASC, lamb_id ASC", args).fetchall()
        return [dict(r) for r in rows]

    def mark_lamb_reported(self, lamb_id: str, reported_at: str | None = None) -> None:
        self.update_lamb(lamb_id, reported_at=reported_at or now_iso())

    # ---------- 保留期清理 ----------
    # 保留期清理（7.16）：每张表按自己的时间字段；未结 / 进行中的不删
    PRUNE_SQL = {
        "messages": """DELETE FROM messages WHERE first_seen < ?
                       AND (route != 'dispatch' OR dispatch_state IN ('acked','failed','missed','context'))""",
        "dispatches": """DELETE FROM dispatches WHERE sent_at < ? AND state NOT IN ('sending','uncertain')
                         AND batch_id NOT IN (SELECT pending_batch_id FROM topics WHERE pending_batch_id IS NOT NULL)""",
        "escalations": "DELETE FROM escalations WHERE closed_at IS NOT NULL AND closed_at < ?",
        "bot_outbox": "DELETE FROM bot_outbox WHERE sent_at < ?",
        "acks": """DELETE FROM acks WHERE added_at < ?
                   AND (removed_at IS NOT NULL OR identity = 'bot' OR reaction_id IS NULL)""",
        "owner_reactions": "DELETE FROM owner_reactions WHERE seen_at < ?",
        "watches": "DELETE FROM watches WHERE closed_at IS NOT NULL AND closed_at < ?",
        "actions": "DELETE FROM actions WHERE done_at IS NOT NULL AND done_at < ?",
        "loop_events": "DELETE FROM loop_events WHERE until < ?",
        "runs": "DELETE FROM runs WHERE ended_at IS NOT NULL AND ended_at < ? AND state NOT IN ('running','retry_wait')",
        "audit_hits": "DELETE FROM audit_hits WHERE notified_at IS NOT NULL AND notified_at < ?",
        # 给主人的通知：已发的、人工确认过的才删；还在待发、放弃了没人处理的留着
        "owner_notices": """DELETE FROM owner_notices WHERE COALESCE(acked_at, sent_at) < ?
                            AND (state = 'sent' OR acked_at IS NOT NULL)""",
        # 一事一串（7.22）：话题按最近一次往里发的时间；清掉以后同一把钥匙再发就新起一个话题
        # 7.51：跟踪事项指向的私聊话题（「放群里」以前那个）不清，事项删了才跟着清
        "owner_threads": """DELETE FROM owner_threads WHERE COALESCE(last_post_at, created_at) < ? AND root_message_id NOT IN
                            (SELECT dm_root_message_id FROM tracked_items WHERE dm_root_message_id IS NOT NULL)""",
        "sends": "DELETE FROM sends WHERE sent_at < ?",
        "seen_before_send": "DELETE FROM seen_before_send WHERE seen_at < ?",
        "bot_dm_replies": "DELETE FROM bot_dm_replies WHERE replied_at < ?",
        # 7.45 群话题归属：按最近活动时间；候选核过的记录按核的时间
        "group_threads": "DELETE FROM group_threads WHERE last_activity_at < ?",
        # 7.52 回音账本：跟着保留期清（还上、报过、没还上的都一样，按记账时间）
        "owner_echoes": "DELETE FROM owner_echoes WHERE recorded_at < ?",
        "thread_root_checks": "DELETE FROM thread_root_checks WHERE checked_at < ?",
        # 7.36：提问的来源；提问本身还开着的不删
        "escalation_sources": """DELETE FROM escalation_sources WHERE created_at < ? AND ask_message_id NOT IN
                                 (SELECT message_id FROM escalations WHERE closed_at IS NULL)""",
        "lambs": "DELETE FROM lambs WHERE finished_at IS NOT NULL AND finished_at < ? AND state != 'running'",
    }

    # 在岗表的履历（7.20）：按 [duty] history_days；进行中的活、还没送达或没回执的留言不删
    PRUNE_DUTY_SQL = {
        "duty_history": "DELETE FROM duty_history WHERE at < ?",
        "duty_routes": "DELETE FROM duty_routes WHERE last_at < ?",
        "duty_route_seen": "DELETE FROM duty_route_seen WHERE at < ?",
        "work_items": "DELETE FROM work_items WHERE closed_at IS NOT NULL AND closed_at < ?",
        "mailbox": """DELETE FROM mailbox WHERE created_at < ? AND (message_id IS NULL OR message_id NOT IN
                      (SELECT message_id FROM messages WHERE dispatch_state IN ('pending','sending','delivered','uncertain')))""",
    }

    # 跟踪事项（7.51）归档满多少天删
    ITEM_PURGE_DAYS = 30

    def prune(self, retention_days: int, receipts_dir: Path | None = None,
              runs_dir: Path | None = None, history_days: int | None = None,
              media_dir: Path | None = None) -> dict[str, int]:
        """清理超过 retention_days 的记录与回执文件、超过 history_days（默认同 retention_days）的在岗表履历；
        media_dir（7.54）：送进会话的图片，消息已经清出账本、目录又超过保留期的整个删。返回每类删了多少。"""
        now = datetime.now().astimezone()
        cutoff = (now - timedelta(days=retention_days)).isoformat(timespec="seconds")
        hcut = (now - timedelta(days=history_days if history_days is not None else retention_days)
                ).isoformat(timespec="seconds")
        out: dict[str, int] = {}
        # 7.51：跟踪事项归档满 ITEM_PURGE_DAYS 天才删（和它的钥匙一起），不跟 retention_days
        icut = (now - timedelta(days=self.ITEM_PURGE_DAYS)).isoformat(timespec="seconds")
        with self.tx() as c:
            gone = [r[0] for r in c.execute("SELECT item_no FROM tracked_items WHERE state='archived' "
                                            "AND archived_at IS NOT NULL AND archived_at < ?", (icut,))]
            for no in gone:
                c.execute("DELETE FROM tracked_keys WHERE item_no=?", (no,))
                c.execute("DELETE FROM tracked_items WHERE item_no=?", (no,))
            out["tracked_items"] = len(gone)
            for table, sql in self.PRUNE_SQL.items():
                out[table] = c.execute(sql, (cutoff,)).rowcount
            for table, sql in self.PRUNE_DUTY_SQL.items():
                out[table] = c.execute(sql, (hcut,)).rowcount
        if receipts_dir is not None and receipts_dir.exists():
            keep = {r[0] for r in self.conn.execute(
                "SELECT pending_batch_id FROM topics WHERE pending_batch_id IS NOT NULL")}
            limit = (now - timedelta(days=retention_days)).timestamp()
            n = 0
            for f in receipts_dir.glob("*/*.json"):
                if f.stem not in keep and f.stat().st_mtime < limit:
                    f.unlink()
                    n += 1
            for d in receipts_dir.iterdir():
                if d.is_dir() and not any(d.iterdir()):
                    d.rmdir()
            out["receipt_files"] = n
        if runs_dir is not None and runs_dir.exists():
            # 命令行宿主的输入输出文件：账本里已经没有、或已结束的运行，超过保留期就删
            active = {r[0] for r in self.conn.execute("SELECT batch_id FROM runs WHERE state IN ('running','retry_wait')")}
            limit = (now - timedelta(days=retention_days)).timestamp()
            n = 0
            for f in runs_dir.iterdir():
                if f.is_file() and f.stem not in active and f.stat().st_mtime < limit:
                    f.unlink()
                    n += 1
            out["run_files"] = n
        if media_dir is not None:
            keep = {r[0] for r in self.conn.execute("SELECT message_id FROM messages")}
            out["media_dirs"] = media.prune(media_dir, keep, (now - timedelta(days=retention_days)).timestamp())
        return out


def row_to_message(row: sqlite3.Row) -> Message:
    mentions = [Mention(**x) for x in json.loads(row["mentions_json"] or "[]")]
    return Message(
        message_id=row["message_id"], chat_id=row["chat_id"], chat_name=row["chat_name"] or "",
        chat_type=row["chat_type"] or "", sender_id=row["sender_id"] or "", sender_name=row["sender_name"] or "",
        sender_type=row["sender_type"] or "", sender_tenant_key=row["sender_tenant_key"] or "",
        content=row["content"] or "", msg_type=row["msg_type"] or "",
        create_time=row["create_time"] or "", mentions=mentions, reply_to=row["reply_to"] or "",
        thread_id=row["thread_id"] or "", link=row["link"] or "", deleted=bool(row["deleted"]),
        updated=bool(row["updated"]), update_time=row["update_time"] or "",
        message_position=row["message_position"] or "",
        thread_position=row["thread_position"] or "",
        root_id=(row["root_id"] if "root_id" in row.keys() else "") or "",
    )
