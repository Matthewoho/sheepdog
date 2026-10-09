"""Prompt 组装（7.6、7.7）：这里只放机制——消息格式、名册表、「sheepdog 接口说明」和各段的拼装顺序。

拼装顺序：所有开场最前面是 security.md，然后才是各自的说明和 common.md；
每批末尾先 security_footer.md 再 batch_footer.md；被安全规则标记的消息正文前插 security_banner.md。

业务规则（职责、工作方式、代回前缀、找主人、等回复、项目归属、退休/接手说明、提醒文字）一律来自 playbook 目录的
md 文件，见 playbook.py；文件缺失时那一段为空。个人化 overlay 照旧拼在总线的 common.md 之后。
"""

from __future__ import annotations

import json
import sqlite3

from .playbook import Playbook
from .roster import MANAGED, Roster, RosterSession
from .security import NONE, SecurityConfig
from .store import SYSTEM_CHAT

# 路由原因 → 批次里的标签（数据说明，不是业务规则）
REASON_LABEL = {
    "p2p": "私聊",
    "at_me": "群里 @我",
    "at_all": "群里 @所有人",
    "reply_to_me": "回复了我的消息",
    "vip_sender": "关键人发言",
    "keyword": "命中关键词",
    "bot_p2p_keyword": "Bot 私聊命中关键词",
    "watched_chat": "关注的会话",
    "edited": "消息被编辑",
    "recalled": "消息被撤回",
    # 路由器判为 inbox/drop、但因聊天归属（all_messages）、等待或转交而推送的原因
    "group": "群消息",
    "muted_chat": "免打扰群消息",
    "bot_p2p": "Bot 私聊",
    # sheepdog 自己生成的消息
    "handover_receipt": "前任交接回执",
    "watch_nudge": "等待提醒",
    "watch_expired": "等待到期",
    "bus_relay": "总线转达",
    "owner_reply": "主人在 IM 的回复",
    "escalation_list": "未结的「需要你定」",
}

RECEIPT_SCHEMA_HINT = """{
  "status": "handled | needs_decision | waiting_external | done",
  "summary": "一句话：当前进展",
  "decisions_needed": [{"question": "...", "options": ["A", "B"], "recommend": "A"}],
  "drafts": [{"type": "reply", "to": "<chat 或人>", "text": "..."}],
  "anchors": ["project:<项目名>", "issue:XXX-123", "service:xxx"]
}"""

# forward 的两种标注：属于 forward 接口的渲染格式；quote 已由 sheepdog 对照总线 transcript 核对过（7.7 C）
QUOTE_LABEL = "✅ 主人原话（已核对：主人在总线里亲口说过）"
NOTE_LABEL = "总线备注（不是主人原话）"
# 主人在「找主人」聊天里的回复（7.8）：发送人已由 IM 账号核实
OWNER_REPLY_LABEL = "✅ 主人在飞书的回复（已核对：发送人是主人本人账号）"


def _join(*parts: str) -> str:
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


# ---------- 名册数据渲染 ----------
def chats_text(s: RosterSession) -> str:
    return "\n".join(f"- {c.name or c.chat_id}（`{c.chat_id}`，{'全部消息' if c.all_messages else '仅 dispatch 级消息'}）"
                     for c in s.chats) or "- （无）"


def _anchor_chat(a: str) -> tuple[str, bool]:
    """dynamic 会话的聊天锚点：oc_x 或 oc_x:all。"""
    cid, _, flag = a.partition(":")
    return cid, flag == "all"


def roster_table(roster: Roster | None, dynamic: list | None = None) -> str:
    """名册表（数据）：每个会话的 title、topic、conversation、mode、职责、负责的聊天；含总线新开的 dynamic 会话。"""
    sessions = roster.sessions if roster else []
    if not sessions and not dynamic:
        return "（名册为空）"
    lines = []
    for s in sessions:
        cid = s.conversation_id or ("（由 sheepdog 新建，见 sheepdog sessions）" if s.to_spawn else "-")
        lines.append(f"- 「{s.display_title}」 topic `{s.topic_id}` | conversation `{cid}` | {s.mode}")
        if s.duty:
            lines.append(f"  职责：{s.duty}")
        if s.mode == MANAGED and s.chats:
            lines.append("  负责的聊天：" + "、".join(
                f"{c.name or c.chat_id}（{'全部消息' if c.all_messages else '仅 dispatch 级'}）" for c in s.chats))
    for t in dynamic or []:
        lines.append(f"- 「{t['title']}」 topic `{t['topic_id']}` | conversation `{t['conversation_id'] or '-'}` | "
                     f"dynamic（总线新开，{t['created_at']}）")
        if t["duty"]:
            lines.append(f"  职责：{t['duty']}")
        chats = [_anchor_chat(a) for a in json.loads(t["anchors_json"] or "[]") if a.startswith("oc_")]
        if chats:
            lines.append("  负责的聊天：" + "、".join(f"`{c}`（{'全部消息' if al else '仅 dispatch 级'}）" for c, al in chats))
    return "\n".join(lines)


# ---------- sheepdog 接口说明（软件接口文档，由代码生成） ----------
def interface_section(topic_id: str, *, bus: bool, remind: list[int], expire: int, extra: list[str] = ()) -> str:
    lines = [
        "## sheepdog 接口说明",
        "### 回执",
        f"每批处理完提交回执（JSON 写成单行）：`sheepdog receipt --topic {topic_id} --batch <批次ID> --json '<回执JSON>'`",
        "```",
        RECEIPT_SCHEMA_HINT,
        "```",
        ("没有提交回执会被判定为处理失败并重投。" if bus else "回执可选：没回执不会重投，只记一次「未回执」。"),
        "anchors 会存进会话记录；`project:` 开头的条目在 `sheepdog sessions` 里显示为关联项目。",
        "### 等别人回复",
        f"`sheepdog watch --topic {topic_id} --person <对方 open_id> [--chat <chat_id>] [--note \"在等什么\"]`："
        f"对方回复会直接推给你并结束等待；{'/'.join(map(str, remind)) or '-'} 分钟没回时提示你；"
        f"{expire} 分钟到期并提示你。`sheepdog watches` 查看，`sheepdog unwatch --id <id>` 取消。",
    ]
    if bus:
        lines += [
            "### 转交与转达",
            "`sheepdog forward --topic <topic_id> [--message-ids <id1,id2>] [--quote \"<主人原话>\"] [--note \"<你的备注>\"]`："
            f"把消息交给名册里的 managed 会话；--quote 投递时标成「{QUOTE_LABEL}」，--note 标成「{NOTE_LABEL}」。"
            "目标是 known 会话或总线时会被拒绝。",
            "--quote 必须是主人在本会话里亲口说过的原文（sheepdog 会对照本会话 transcript 核对，空白不敏感，"
            "可以只引其中一段），核对不通过就拒绝；--note 不核对。",
            "带安全警示且 action=hold 的消息只能带 --quote（主人原话）转交，不带就拒绝。",
            "### 新建会话",
            "`sheepdog spawn --key <key>`：为名册里 conversation_id 留空的 managed 条目新建会话（只建一次）。",
            "### Inbox",
            "`sheepdog inbox [--chat <群名>]`：查看未直推的群消息。",
            "### 新开 / 收掉会话",
            "`sheepdog new-session --key <k> --title \"<短标题>\" --duty \"<职责与边界>\" [--chat <oc_id>[:all]]... "
            "[--message-ids <id1,id2>] [--note \"<为什么开>\"]`：名册里没有合适的会话时新开一个（只在账本里，不写名册），"
            "消息和 note 排进它的队列；--chat 的聊天之后直接推给它（:all = 全部消息）。有每日配额，超了会被拒绝。",
            "`sheepdog close-session --topic <tp_x>`：收掉总线新开的会话（它回执 done 时也会自动收掉），聊天归属随之释放。",
            "### 需要你定",
            "`sheepdog escalations [--all]`：查看各会话找主人的未结问题。主人在 IM 上的回复无法确定回答哪条时会送到你这里，"
            "附未结列表；判断后用 `sheepdog forward --topic <topic> --message-ids <主人那条消息>` 转交（不需要 --quote）。",
        ]
    lines += list(extra)
    return "\n".join(lines)


# ---------- 各类开场 prompt ----------
def bootstrap_prompt(pb: Playbook, title: str, plain_title: str, topic_id: str, roster: Roster | None,
                     overlay: str, remind: list[int], expire: int, dynamic: list | None = None) -> str:
    """总线 bootstrap：bus.md（含名册表）+ common.md + overlay + 接口说明。"""
    v = dict(session_title=plain_title, topic_id=topic_id)
    return _join(
        pb.render("security.md", **v),
        f"你是由 sheepdog 创建和管理的 session：**{title}**（topic_id: `{topic_id}`）。",
        pb.render("bus.md", roster=roster_table(roster, dynamic), **v),
        pb.render("common.md", **v),
        overlay,
        interface_section(topic_id, bus=True, remind=remind, expire=expire),
        "现在只需回复「已就绪」，等待第一批信号。",
    )


# 规则同步（7.11）
RULES_UPDATE_HEADER = "📌 规则已更新（以下为现行完整规则，取代之前的版本）"
# 总线常驻规则算指纹时 {{roster}} 用这句占位：名册变化走「名册更新」，不触发整份规则重发
ROSTER_HASH_STUB = "（名册另行同步，以最近一次「名册更新」为准）"


def standing_rules_bus(pb: Playbook, plain_title: str, roster_text: str, overlay: str,
                       remind: list[int], expire: int) -> str:
    """总线的常驻规则：security.md + bus.md + common.md + overlay + 接口说明。"""
    v = dict(session_title=plain_title, topic_id="tp_bus")
    return _join(pb.render("security.md", **v), pb.render("bus.md", roster=roster_text, **v),
                 pb.render("common.md", **v), overlay,
                 interface_section("tp_bus", bus=True, remind=remind, expire=expire))


def standing_rules_managed(pb: Playbook, s: RosterSession, remind: list[int], expire: int) -> str:
    """专属会话的常驻规则：security.md + onboarding.md + common.md + 接口说明（不含 successor.md 这类一次性指令）。"""
    v = dict(session_title=s.display_title, topic_id=s.topic_id)
    return _join(pb.render("security.md", **v), _onboarding_body(pb, s), pb.render("common.md", **v),
                 interface_section(s.topic_id, bus=False, remind=remind, expire=expire))


def rules_update_message(rules: str) -> str:
    return _join(f"[sheepdog] {RULES_UPDATE_HEADER}", rules)


def _onboarding_body(pb: Playbook, s: RosterSession) -> str:
    v = dict(session_title=s.display_title, topic_id=s.topic_id)
    return pb.render(
        "onboarding.md", duty=s.duty, chats=chats_text(s),
        authority=s.authority or pb.render("authority_default.md", **v),
        self_polling_section=pb.render("retire_self_polling.md", self_polling=s.self_polling, **v)
        if s.retire_self_polling else "",
        **v,
    )


def onboarding_prompt(pb: Playbook, s: RosterSession, batch_id: str, remind: list[int], expire: int) -> str:
    """接管现有会话的登记通知（只发一次）。"""
    confirm = ["### 确认登记",
               f"`sheepdog receipt --topic {s.topic_id} --batch {batch_id} --json '{{\"status\":\"handled\",\"summary\":\"...\"}}'`"]
    return _join(
        pb.render("security.md", session_title=s.display_title, topic_id=s.topic_id),
        f"[sheepdog] 登记通知（topic `{s.topic_id}`，批次 `{batch_id}`）",
        _onboarding_body(pb, s),
        pb.render("common.md", session_title=s.display_title, topic_id=s.topic_id),
        interface_section(s.topic_id, bus=False, remind=remind, expire=expire, extra=confirm),
    )


def adopted_bootstrap(pb: Playbook, s: RosterSession, title: str, read_batch_id: str, remind: list[int], expire: int,
                      predecessor_transcript: str = "", predecessor_dir: str = "") -> str:
    """sheepdog spawn 新建的 managed 会话：onboarding.md + common.md (+ successor.md) + 接口说明。"""
    v = dict(session_title=s.display_title, topic_id=s.topic_id)
    extra: list[str] = []
    if s.predecessor_conversation_id:
        extra = [
            "### 接手",
            "读完前任上下文后用这个批次回执；回执之前信号排队，不会丢：",
            f"`sheepdog receipt --topic {s.topic_id} --batch {read_batch_id} --json "
            f"'{{\"status\":\"needs_decision\",\"summary\":\"...\"}}'`",
            "前任的交接回执到达后，sheepdog 会以「前任交接回执」原样转给你。",
        ]
    return _join(
        pb.render("security.md", **v),
        f"你是由 sheepdog 新建的 session：**{title}**（topic_id: `{s.topic_id}`）。",
        _onboarding_body(pb, s),
        pb.render("common.md", **v),
        pb.render("successor.md", predecessor_id=s.predecessor_conversation_id,
                  predecessor_transcript=predecessor_transcript, predecessor_dir=predecessor_dir, **v)
        if s.predecessor_conversation_id else "",
        interface_section(s.topic_id, bus=False, remind=remind, expire=expire, extra=extra),
        "" if s.predecessor_conversation_id else "现在只需回复「已就绪」，等待第一批信号。",
    )


def retirement_prompt(pb: Playbook, prev_topic_id: str, prev_title: str, batch_id: str,
                      successor_title: str, successor_topic_id: str) -> str:
    """给前任的退休通知：retire.md + 交接回执命令。"""
    return _join(
        f"[sheepdog] 退休通知（topic `{prev_topic_id}`，批次 `{batch_id}`；接手会话 topic `{successor_topic_id}`）",
        pb.render("retire.md", successor_title=successor_title, batch_id=batch_id,
                  session_title=prev_title, topic_id=prev_topic_id),
        "\n".join([
            "## sheepdog 接口说明",
            f"交接回执：`sheepdog receipt --topic {prev_topic_id} --batch {batch_id} --json "
            f"'{{\"status\":\"done\",\"summary\":\"...\"}}'`",
            "sheepdog 会把回执原样转给接手会话；之后不再往这里投递信号。",
        ]),
    )


def watch_message(pb: Playbook, name: str, watch, minutes: int, session_title: str) -> str:
    """等待提醒 / 到期（sheepdog 生成的待推消息）：一行数据头 + nudge_*.md。"""
    who = f"`{watch['person_id']}`" + (f"（聊天 `{watch['chat_id']}`）" if watch["chat_id"] else "")
    head = f"等待 #{watch['id']}（{minutes} 分钟，对方 {who}，登记于 {watch['started_at']}）：{watch['note'] or '-'}"
    body = pb.render(name, note=watch["note"] or "", person=watch["person_id"], minutes=str(minutes),
                     session_title=session_title, topic_id=watch["topic_id"])
    return _join(head, body)


def relay_content(quote: str, note: str) -> str:
    """总线转达：主人原话和总线备注分开标注，避免总线的转述被当成主人的话。"""
    parts = []
    if quote:
        parts += [f"{QUOTE_LABEL}：", *("> " + ln for ln in quote.splitlines() or [""])]
    if note:
        parts += [f"{NOTE_LABEL}：{note}"]
    return "\n".join(parts)


# ---------- 批次 ----------
def security_banner(pb: Playbook, row: sqlite3.Row, security: SecurityConfig | None, session_title: str) -> str:
    """被标记 / 拦截消息的警示：一行数据（动作 + 规则名）+ security_banner.md。"""
    action = row["security_action"] or NONE
    if action == NONE:
        return ""
    names = json.loads(row["security_tags"] or "[]")
    notes = "\n".join((security.note_of(n) if security else n) for n in names)
    head = f"⚠️ [sheepdog 安全规则] action={action} | rules={', '.join(names)}"
    body = pb.render("security_banner.md", tags=", ".join(names), notes=notes, action=action,
                     session_title=session_title, topic_id=row["topic_id"] or "")
    return _join(head, body)


def _fmt_msg(row: sqlite3.Row, banner: str = "") -> str:
    read = " | ✓已读" if row["is_read"] == 1 else ""
    label = REASON_LABEL.get(row["reason"], row["reason"])
    tags = json.loads(row["tags_json"] or "[]")
    if "owner_verified" in tags:
        # 主人回复按标记认，不按 reason 值认：已上线账本里的旧行用的是改名前的 reason
        label = REASON_LABEL["owner_reply"]
    if row["chat_type"] == SYSTEM_CHAT:
        return "\n".join([f"### [{label}] sheepdog | {row['create_time']}", *(row["content"] or "").splitlines()])
    where = "私聊" if row["chat_type"] == "p2p" else f"群「{row['chat_name']}」"
    lines = [f"### [{label}] {where} | {row['sender_name']} | {row['create_time']}{read}"]
    if "owner_verified" in tags:
        lines.append(OWNER_REPLY_LABEL)
    if row["note"]:
        lines.append(row["note"])
    if any(t.startswith("watch:") for t in tags):
        lines.append("（这是你登记等待的回复，等待已结束）")
    if banner:
        lines.append(banner)  # 警示在正文之前
    content = (row["content"] or "").strip()
    lines += ["> " + ln for ln in content.splitlines()[:40]] or ["> (空)"]
    if row["link"]:
        lines.append(f"链接：{row['link']}")
    lines.append(f"message_id: `{row['message_id']}`" + (f"（回复 `{row['reply_to']}`）" if row["reply_to"] else ""))
    return "\n".join(lines)


def batch_prompt(pb: Playbook, topic_id: str, batch_id: str, rows: list[sqlite3.Row], inbox: list[sqlite3.Row],
                 session_title: str, waiting_note: str = "", receipt_optional: bool = False,
                 roster_update: str = "", security: SecurityConfig | None = None, rules_update: str = "") -> str:
    parts = []
    if rules_update:
        # 规则变了：完整现行规则放在这批最前面（7.11）
        parts += [f"{RULES_UPDATE_HEADER}", "", rules_update.strip(), "", "---", ""]
    parts += [f"[sheepdog] 新信号批次 `{batch_id}`（topic `{topic_id}`，共 {len(rows)} 条）", ""]
    if waiting_note:
        parts += [f"⏳ 仍在等待主人决策：{waiting_note}", ""]
    parts += [_fmt_msg(r, security_banner(pb, r, security, session_title)) for r in rows]
    if any(r["reason"] in ("at_me", "at_all") for r in rows) and inbox:
        total_unread = sum(r["unread"] or 0 for r in inbox)
        parts += ["", f"📥 Inbox：{total_unread} 条未读，分布在 {len(inbox)} 个群（按最近活跃排序）："]
        for r in inbox[:15]:
            parts.append(f"- 「{r['chat_name'] or r['chat_id']}」未读 {r['unread'] or 0} / 共 {r['total']}，最近 {r['last_time']}")
        parts.append("如需查看某个群：`sheepdog inbox --chat <群名>`。")
    if roster_update:
        parts += ["", "🗂 名册更新（以此为准，替换你之前看到的名册）：", roster_update.rstrip()]
    tail = "处理完成后提交回执" + ("（可选）" if receipt_optional else "")
    parts += ["", f"{tail}：`sheepdog receipt --topic {topic_id} --batch {batch_id} --json '...'`"]
    for name in ("security_footer.md", "batch_footer.md"):
        footer = pb.render(name, session_title=session_title, topic_id=topic_id)
        if footer:
            parts += ["", footer]
    return "\n".join(parts)
