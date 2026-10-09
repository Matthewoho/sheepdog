> sheepdog 配置与命令的完整参考（中文）。项目介绍见 [README](../README.md) · [简体中文](../README.zh-CN.md) · [日本語](../README.ja.md)。

# sheepdog 参考手册

**sheepdog 是你的工作助理。** 它的第一项工作是：接收你在 IM 上收到的工作，分给负责的 Agent session。

> 你是牧羊人，AI Agent session 是羊群，sheepdog 是牧羊犬。
>
> 它盯着你的 IM，把需要你知道的信号赶到负责的 session 里，把 session 管在该待的状态里，只把需要你拍板的事带回给你。你负责**观察、指导、决策**，Agent 负责**收集、分类、处理、起草**。

`sheepdog` 持续增量拉取你在 IM（目前是飞书 / Lark）里收到的所有消息，按规则分成三类：

| 消息 | 去向 |
|---|---|
| 私聊、群里 @我 / @所有人、回复我的消息、关键人发言、命中关键词 | **直推** `[managed]` Agent session |
| 其他群消息 | **Inbox**（按群分区，@我时附带摘要） |
| 免打扰群（未 @我）、自己发的消息 | 忽略（自己的消息仅入账，用于「回复我」判定） |

直推的消息先看**名册**（`roster.toml`）里有没有会话负责这个聊天：有就直接推给那个会话，没有就推给总线 session。名册里可以登记你已经在用的会话（`managed`，sheepdog 往里推）或只让总线知道它管什么（`known`，从不投递）；`all_messages = true` 的聊天连普通群消息和免打扰也会推给负责的会话。总线知道名册，遇到属于别人的事会用 `sheepdog forward` 转交。

已读不影响路由：你看过的消息照样推给 session，并标注「✓已读」。编辑 / 撤回按原消息的路由级别处理：已推送的会通知 session，Inbox 中的静默更新，编辑后新增 @我 会升级直推。

## 架构

```
Source (lark-cli, user 身份)
   └─► Collector ──► Ledger (SQLite) ──► Router (纯规则)
                                            ├─ drop
                                            ├─ inbox（按群）
                                            └─ dispatch ──► Dispatcher ──► Sink (Antigravity agentapi)
                                                               ▲                 │
                                                               └── 回执 ◄────────┘  sheepdog receipt
```

- **Session 状态机**：`active → running → (active | waiting_human | blocked | closed)`，以及 `human_attached`（你在 session 里发言后暂停投递）、`failed → attention`（重试耗尽）。状态由 session 每批提交的回执驱动。
- **可插拔**：`Source`（消息源）、`Sink`（session 投递）、`Store`（账本）都是接口，当前实现分别为 lark-cli、Antigravity `agentapi`、SQLite。

## 安装

要求 Python ≥ 3.11、已登录的 [`lark-cli`](https://github.com/larksuite)（user 身份）、Antigravity App。

```bash
pip install -e .
mkdir -p ~/.config/sheepdog
cp examples/config.example.toml ~/.config/sheepdog/config.toml   # 填 self_open_id 等
cp examples/roster.example.toml ~/.config/sheepdog/roster.toml   # 登记你现有的会话（可选）
cp -r examples/playbook ~/.config/sheepdog/playbook              # 业务规则 md，按自己的工作方式改写
cp examples/security.example.toml ~/.config/sheepdog/security.toml  # 安全规则，按自己的情况改写
sheepdog doctor
sheepdog init --dry-run        # 预览：名册同步、总线、给每个 managed 会话的 onboarding
sheepdog init                  # 正式登记（可重复执行，已 onboarding 的跳过）
```

## 使用

```bash
sheepdog poll --dry-run        # 拉一次、只打印将要投递的内容
sheepdog run                   # 常驻（推荐由 Antigravity App 以 sidecar 托管，见 examples/sidecar.example.json）
sheepdog inbox                 # Inbox 按群汇总
sheepdog inbox --chat 某群      # 查看某个群的 Inbox
sheepdog sessions              # session 注册表与状态（mode、职责、负责的聊天、onboarding、未回执次数）
sheepdog forward --topic tp_x --message-ids a,b --note "..."   # 总线把消息转交给 managed 会话
sheepdog forward --topic tp_x --quote "主人原话" --note "总线补充"  # 主人在总线里的回答转达过去（原话与备注分开标注）
sheepdog spawn --key k         # 名册里 conversation_id 留空的条目：新建会话（可接手前任：先发退休通知、再新建、转交接回执）
sheepdog watch --topic tp_x --person ou_x --note "在等什么"   # 等别人回复：对方回复直推，按 [watch] 配置提醒与到期
sheepdog watches               # 等待列表；sheepdog unwatch --id N 取消
sheepdog security-log --since 24h   # 被安全规则标记 / 拦截的消息
sheepdog escalations [--all]   # 会话找你拍板的「需要你定」：未结 / 全部
sheepdog acks [--open]         # 替你点过的确认表情及是否已撤
sheepdog new-session --key k --title "短标题" --duty "职责与边界" [--chat oc_x[:all]]... [--message-ids a,b] [--note "..."]
                               # 总线临时新开一个专属会话（只在账本里，不写名册）
sheepdog close-session --topic tp_x   # 收掉总线新开的会话
sheepdog push-rules [--topic tp_x] [--dry-run]   # 立即给会话发一次完整现行规则（不等新消息）
sheepdog retire --topic <tp_x 或 conversation_id> [--successor-title "..."]   # 给会话补发 / 手动发退休通知
sheepdog actions [--all]       # 排队 / 完成 / 失败的动作
```

## 在飞书上直接回复「需要你定」

会话拿不准时以 bot 身份私聊你（开头约定写在 playbook/common.md，带 topic_id）。把这个私聊配进 `[escalation] chat_ids`（不要放 `ignore_chat_ids`），sheepdog 会先于普通路由处理它：

- 机器人发的、正文匹配 `header_regex` 的提问：登记为一条「需要你定」，消息本身不投递；
- 你的回复：引用了哪条就送回哪条的会话；没引用时，未结的全部属于同一个会话（含只有一条）就送给它、算回答其中最近一条；未结分属不同会话或没有未结时送总线并附未结列表，由总线 `sheepdog forward --message-ids` 转交。送达时标「✅ 主人在飞书的回复（已核对：发送人是主人本人账号）」并附问题摘要，不走安全闸；
- 其他消息一律丢弃。超过 `open_hours` 未答的自动关闭。

回执 anchors 里 `project:` 开头的条目会存进会话记录，`sheepdog sessions` 显示为关联项目。

`agentapi` 依赖 Antigravity App 注入的环境变量（`ANTIGRAVITY_AGENTAPI_EXE` / `ANTIGRAVITY_LS_ADDRESS` / `ANTIGRAVITY_CSRF_TOKEN`），因此 `run` 需要在 App 托管的 sidecar 或 App 内终端中运行。

## 总线只做轻活：没有合适的会话就新开

总线负责分派、转交、简单确认和汇总；需要查云、读代码、跑命令、改文件、多步排查的事交给专属会话。名册里没有合适的，总线用 `sheepdog new-session` 临时新开一个（具体判断规则写在 playbook/bus.md）：

- 新开的会话类型是 `dynamic`，只记在账本 topics 表，**不写名册文件**；名册同步只关 adopted / known 里缺失的条目，不碰它。名册后来加了同名 key 会报错跳过，先 `close-session` 再登记。
- key 规则同名册（`[a-z0-9_-]`，不能是 bus），不能和名册或账本里已有的 topic 重复（含已收掉的）；`--chat` 的聊天不能已归属其他会话。
- 标题 = `title_prefix` + `--title`；开场同 spawn：security.md、onboarding.md（授权用 authority_default.md）、common.md、接口说明。
- `--message-ids` 和 `--note` 按 forward 的规则排进它的队列，下一轮投递；`--note` 同时记为创建原因。被安全规则 hold 的消息不能随 new-session 转，要先新开再 `forward --quote`。
- `--chat` 写进它的 anchors，之后这些聊天按归属直接推给它（`:all` = 全部消息）。名册里的归属优先。
- 配额：`[bus] max_new_sessions_per_day`（默认 5，按本地日历日计，收掉的也算）；超了拒绝并提示找你；`0` = 禁止总线新开。
- 它回执 `done` 就收掉（adopted 的 done 不收）；也可以 `sheepdog close-session --topic tp_x` 手动收（只能关 dynamic）。收掉后聊天归属释放，再来的消息回总线，还没投出去的也退回总线。
- 回执 anchors 不能改聊天归属：`oc_` 开头的锚点会被忽略。
- 总线看到的名册（`{{roster}}` 和「名册更新」）包含这些会话，标注「总线新开」；新开或收掉后总线下一批会附最新名册。`sheepdog sessions` 显示创建时间和创建原因。

## 要调 agentapi 的命令交给常驻进程执行

实测只有 sidecar 常驻进程能给别的项目里的会话投递；会话终端里直接调 agentapi 会因为 project_id 不匹配被拒。所以：

- `spawn`、`new-session`、`push-rules`、`init`、`retire` 不在常驻进程里执行时，不调 agentapi，只在账本 `actions` 表里入队，打印「已提交，sheepdog 下一轮执行，结果见 `sheepdog actions`」，退出码 0。入队前会先做能做的校验（key、聊天冲突、配额、topic 是否存在），不合法当场报错。`--dry-run` 一律本地执行、不入队。
- 「是不是常驻进程」：`sheepdog run` 启动时在本进程内打标记，其他进程一律不是。App 注入的环境变量里没有可靠、可核实的 sidecar 标识，会话终端里同样有 agentapi 的环境变量，分不出来。sidecar 启动命令里的 `sheepdog init` 也会入队，紧接着由 run 第一轮执行。
- `run` 每轮开始先按 id 顺序执行排队的动作，单条失败记 `error`，不阻塞后续，也不重试；然后再拉取和投递。
- `sheepdog retire --topic <tp_x 或 conversation_id>`：给一个会话补发退休通知（retire.md）。已关闭的前任（`tp_<key>.prev`）照原记录补发；名册里某条的 `predecessor_conversation_id` 记到 `tp_<key>.prev`；交接回执都转给接手会话。账本里其他会话记到 `<topic>.retired`、回执转总线（dynamic 会话同时收掉；名册里的会话要停止投递得从名册删）；完全不认识的 id 记到 `tp_retired.<前 12 位>`、回执转总线。总线不能退休。
- `forward` 本来就是入队、由常驻进程投递，不变。

## 规则改了自动同步给已有会话

playbook 的常驻规则原本只在会话建立时发一次。现在每个会话在账本里记一份「上次送达的规则指纹」（topics 表 `rules_hash`，sha256）：

- 指纹覆盖的内容：总线 = security.md + bus.md + common.md + overlay + 接口说明；专属会话（名册 managed、总线新开的）= security.md + onboarding.md + common.md + 接口说明。successor.md 这类一次性指令不算。
- 总线算指纹时 `{{roster}}` 用固定占位：名册变化照旧走「名册更新」，不会触发整份规则重发；真正发出去的规则里名册是现行的。
- 建会话、onboarding、spawn、new-session 时记下当时的指纹。
- 每次给会话投批次前重新渲染比对：不同就在这批最前面加「📌 规则已更新（以下为现行完整规则，取代之前的版本）」和完整规则，并更新指纹；没有待投消息时不单独发，下次有消息再一起带。
- 没有指纹（老账本第一次上线新代码）视为不同，所以上线后每个会话的下一批都会带上现行规则。
- 接口说明（命令用法）也在指纹里：代码升级加了新命令时，已有会话同样会收到一次。
- `sheepdog sessions` 显示每个会话的规则是「最新」「待更新」还是「未记录」；`sheepdog push-rules [--topic tp_x] [--dry-run]` 不等新消息立即发（对总线也可以；known、已关闭、还没 onboarding 的跳过）。

## 确认表情：看到就点，回复后撤下

配置 `[ack]` 段后开启，没有这一段就完全不动：

```toml
[ack]
emoji_type = "Get"                          # 飞书表情类型
reasons = ["p2p", "at_me", "at_all"]        # 投递原因在其中的消息，送达会话后以你的身份点表情
remove_on_reply_reasons = ["p2p", "at_me"]  # 这些原因的表情，在你回复后撤下；@所有人 只点不撤
```

- **点**：消息实际投递给会话成功之后才点；排队中（你在会话里接管、等 spawn）不点；被安全规则 hold 的消息不点（tag 的照常点）。每条消息只点一次，记进账本 `acks` 表（含 `reaction_id`）。
- **撤**：账本入账一条你自己发的消息时（包括会话以你身份代回的）：私聊里，同一聊天中点表情早于这条消息的全部撤下；群里，回复了那条消息或 @ 了它的发送人才撤下对应的。只撤 `remove_on_reply_reasons` 里的原因。
- 用 lark-cli 的 user 身份调 `im reactions create` / `im reactions delete`。失败只记日志和 `error` 列，不影响投递和路由，也不重试。
- `--dry-run` 不调接口，只打印将要点 / 撤什么。
- 这是 sheepdog 唯一的 IM 写操作。

## 业务规则：playbook

代码只放机制（名册、路由、投递、回执、等待计时、spawn、forward，以及自动生成的「sheepdog 接口说明」）。总线职责、工作方式、代回前缀、拿不准找你、等别人回复、需求归属项目、退休与接手说明、提醒文字，全部写在配置目录的 md 文件里：

```bash
cp -r examples/playbook ~/.config/sheepdog/playbook   # 拷贝通用示例后按自己的工作方式改写
sheepdog doctor                                         # 列出缺哪些文件（缺的那一段在 prompt 里为空）
```

| 文件 | 用在哪 | 专用占位符 |
|---|---|---|
| `common.md` | 所有会话（总线、新建、接管、接手） | — |
| `bus.md` | 总线 bootstrap | `{{roster}}` |
| `onboarding.md` | 接管现有会话（新建的会话也用它） | `{{duty}}` `{{chats}}` `{{authority}}` `{{self_polling_section}}` |
| `retire_self_polling.md` | `retire_self_polling = true` 时填进 `{{self_polling_section}}` | （额外可用 `{{self_polling}}`） |
| `authority_default.md` | authority 为空时代替 `{{authority}}` | — |
| `retire.md` | 给前任的退休通知 | `{{successor_title}}` `{{batch_id}}` |
| `successor.md` | 接手会话开场附加 | `{{predecessor_id}}` `{{predecessor_transcript}}` `{{predecessor_dir}}` |
| `nudge_remind.md` / `nudge_expire.md` | 等待提醒 / 到期 | `{{note}}` `{{person}}` `{{minutes}}` |
| `batch_footer.md` | 每批信号末尾 | — |
| `security.md` | 所有开场的最前面（在 common.md 之前） | — |
| `security_banner.md` | 被安全规则标记的消息正文前的警示 | `{{tags}}` `{{notes}}` `{{action}}` |
| `security_footer.md` | 每批信号末尾，batch_footer 之前 | — |

通用占位符：`{{session_title}}` `{{topic_id}}` `{{reply_prefix}}` `{{watch_remind_minutes}}` `{{watch_expire_minutes}}`。占位符是简单字符串替换，不认识的原样保留。文件每次组装 prompt 时现读，改完下一批生效，不用重启。`prompt_overlay_path` 照旧拼在总线的 common.md 之后。

相关参数在 `config.toml`：`session.reply_prefix`、`[watch] remind_minutes / expire_minutes`、`routing.drop_bot_message_prefixes`（机器人消息以这些前缀开头就丢弃，防止会话找你的私聊被推回总线）。

## 安全闸

有人在 IM 里发危险请求（要密钥、要权限、删东西、跑脚本、转账、冒充你「已经同意了」、让 agent 忽略规则）时，sheepdog 在投递前先过一道规则：

- 规则写在 `security.toml`（`security_path`，示例见 `examples/security.example.toml`），每条是正则 `patterns` 加可选条件 `external_sender`（发送方租户不在 `own_tenant_keys` 里）、`sender_types`。代码里没有任何具体关键词。
- `tag`：照常投递，正文前插 `security_banner.md` 警示。`hold`：不投给任何专职会话（覆盖名册、等待、all_messages），改投总线并带警示。
- 被 hold 的消息只能由总线 `sheepdog forward --message-ids ... --quote "<你的原话>"` 转交；`--quote` 会对照总线会话 transcript 里你亲口说过的话核对（回看 `quote_max_age_hours`），核对不过就拒绝。
- 编辑过的消息会重新判定；`sheepdog security-log` 查看记录，`sheepdog doctor` 显示规则条数和 `own_tenant_keys` 是否配置。

**已知局限**：

- 规则匹配挡不住所有变形话术；最后一道防线仍是会话自己遵守 `security.md`，以及 Antigravity 的命令权限和云权限本身。
- 消息是会话自己用 lark-cli 发的、命令是会话自己执行的，sheepdog 不在执行路径上，只能在投递前标记和拦截。

## 数据与隐私

| 层 | 位置 | 进仓库 |
|---|---|---|
| 引擎代码 | 本仓库 | ✅ |
| 个人配置 / 名册 / playbook / 安全规则 / Prompt 覆盖层 | `~/.config/sheepdog/` | ❌ |
| 运行数据（账本、Inbox、回执） | `~/.local/state/sheepdog/`，默认保留 7 天 | ❌ |
| 业务产出 | 由各 session 自行写入你的知识库 / 任务系统 | ❌ |

除了加、撤确认表情（`[ack]`），sheepdog 对 IM 只读。

提交前运行 `python3 scripts/check_private_data.py`（或启用 `.pre-commit-config.yaml`）拦截真实 IM ID、家目录路径与凭据。测试只使用 `*_test_*` 合成数据。

## 开发

```bash
python3 -m unittest discover -s tests -v
```

## License

Apache-2.0
