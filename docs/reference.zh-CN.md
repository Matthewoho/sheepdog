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
                                            └─ dispatch ──► Dispatcher ──► Sink（按会话宿主：Antigravity agentapi /
                                                                                 Codex app-server / Claude 命令行）
                                                               ▲                 │
                                                               └── 回执 ◄────────┘  sheepdog receipt
```

- **Session 状态机**：`active → running → (active | waiting_human | blocked | closed)`，以及 `human_attached`（你在 session 里发言后暂停投递）、`failed → attention`（重试耗尽，attention 期间信号只排队；见下文「会话卡住（attention）」）。状态由 session 每批提交的回执驱动。
- **可插拔**：`Source`（消息源）、`Sink`（session 投递）、`Store`（账本）都是接口，当前实现分别为 lark-cli、SQLite，以及三种会话宿主：Antigravity `agentapi`（默认）、Codex（`codex app-server`）、Claude（`claude -p`）。后两种默认关闭，见下文「Codex 和 Claude 会话」。

## 安装

要求 Python ≥ 3.11、已登录的 [`lark-cli`](https://github.com/larksuite)（user 身份）、Antigravity App。要用 Codex / Claude 会话，还要装好并登录对应的命令行（`codex`、`claude`）。

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
                               [--host antigravity|codex|claude] [--model M] [--effort E]
                               # 总线临时新开一个专属会话（只在账本里，不写名册）
sheepdog close-session --topic tp_x   # 收掉总线新开的会话
sheepdog push-rules [--topic tp_x] [--dry-run]   # 立即给会话发一次完整现行规则（不等新消息）
sheepdog retire --topic <tp_x 或 conversation_id> [--successor-title "..."]   # 给会话补发 / 手动发退休通知
sheepdog actions [--all]       # 排队 / 完成 / 失败的动作
sheepdog show <message_id>     # 账本里这条消息的完整正文（送进会话的长消息会截断）
sheepdog show --topic tp_x     # 这个会话最近几轮的问答（读对应宿主的记录），最后一行告诉你去哪里接着看
sheepdog inject --text "..." [--chat oc_x] [--sender ou_x] [--at-me]   # 测试环境（[source] enabled = false）：放一条合成消息进账本
sheepdog redeliver --batch b   # 发送中断、不确定是否送达的批次：人工确认后重发
sheepdog watch-done --id N     # 会话确认真的等到了对方的回复
sheepdog loops [--clear <chat_id>]   # 疑似和 Agent 循环而冷却中的聊天；手动解除
sheepdog board [--topic tp_x] [--json]   # 羊群看板：各会话的职责、运行状态、此刻 / 最近动作、名下的活、未读留言
sheepdog notices [--all] [--show N]      # 给你的通知：没发出去的（还在重试的、放弃了的）；--all 连已发的
sheepdog notices --retry N|all           # 没发出去的回到待发（2 小时重新算），常驻进程下一轮重发，防重编号不变
sheepdog notices --ack N|all             # 确认已处理：不再提示、不再重发
sheepdog work set --topic tp_x --key K --title "..." --state doing [--waiting-on 谁] [--until 几点] [--expect "..."] [--to tp_y]
sheepdog work done --topic tp_x --key K --result "..."   # 会话登记 / 收掉名下的活（立即写入）
sheepdog tell --topic tp_x --to tp_y "话" [--key K] [--quote "主人原话"]   # 会话给别的会话留言，下一批送到
sheepdog ask --topic tp_x --title "短标题" --text "..." [--key K]   # 会话找你「需要你定」（sheepdog 加开头一行；同一把钥匙归到一个话题里）
sheepdog report --topic tp_x --title "短标题" --text "..." [--key K]   # 会话向你汇报（参数同 ask）
sheepdog reply --topic tp_x --to om_x --card-text "..."   # 会话代回：发前先查你有没有在这个聊天里说过话，说过就不发（退出码 3）
sheepdog thread --topic tp_x (--key K | --thread omt_x)   # 只读：列出一个话题的全部消息
```

## 在飞书上直接回复「需要你定」

会话拿不准时用 `sheepdog ask` 经机器人私聊你（开头一行由 sheepdog 加，带 topic_id，见下文「一事一串」）。把这个私聊配进 `[escalation] chat_ids`（不要放 `ignore_chat_ids`），sheepdog 会先于普通路由处理它：

- 机器人发的、正文匹配 `header_regex` 的提问：登记为一条「需要你定」，消息本身不投递；
- 你的回复：引用了哪条就送回哪条的会话（引用认不出来源的送总线，见下文「引用回复按引用走」）；没引用时，未结的全部属于同一个会话（含只有一条）就送给它、算回答其中最近一条；未结分属不同会话或没有未结时送总线并附未结列表，由总线 `sheepdog forward --message-ids` 转交。送达时标「✅ 主人在飞书的回复（已核对：发送人是主人本人账号）」并附问题摘要，不走安全闸；
- 其他消息一律丢弃。超过 `open_hours` 未答的自动关闭。

回执 anchors 里 `project:` 开头的条目会存进会话记录，`sheepdog sessions` 显示为关联项目。

`agentapi` 依赖 Antigravity App 注入的环境变量（`ANTIGRAVITY_AGENTAPI_EXE` / `ANTIGRAVITY_LS_ADDRESS` / `ANTIGRAVITY_CSRF_TOKEN`），因此 `run` 需要在 App 托管的 sidecar 或 App 内终端中运行（Codex / Claude 会话不依赖这些变量，但总线和默认宿主的会话仍然走 agentapi）。

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

## Codex 和 Claude 会话

每个会话有一个宿主（`host`）：`antigravity`（默认，经 App 的 agentapi）、`codex`、`claude`。名册 `[[session]]` 可以写 `host`、`model`、`effort`，`new-session` 有同名参数；留空就用配置里这个宿主的默认值。已有会话不能换宿主，要换就退休旧的、新开一个（接手流程）。`max_new_sessions_per_day` 是所有宿主共用的一个额度。

- **新开先问主人**：`[hosts.*] ask_owner_before_new` 默认 true，这时 `new-session --host codex|claude` 必须带 `--quote "<主人原话>"`。核对同 forward：总线里你亲口说过，或你本人账号在飞书上的回复，回看时长同 `quote_max_age_hours`。不带或核对不通过就拒绝（在终端里入队的，入队时就拒）；通过了，原话会以「✅ 主人原话（已核对…）」写进新会话的开场。只管新开：`spawn`（名册里你亲手写的会话）和往已有会话 forward 都不受影响。改了下一轮生效。

- **配置**：`[hosts.codex]`、`[hosts.claude]`，默认 `enabled = false`；启用时 `workdir` 必填。`model`、`effort`、`timeout_minutes`、`max_concurrent` 下一轮生效，`enabled`、`command`、`workdir`、`settings` 要重启。没启用的宿主，new-session 和名册引用直接报错。
- **怎么跑**：一轮是一个后台进程（自成进程组），不卡主循环。Codex 由 sheepdog 自己的 runner 起一个 `codex app-server`（JSON-RPC over stdio）：`initialize` → `thread/start`（新建，带 `threadSource: "user"`，再 `thread/name/set` 设标题）或 `thread/resume`（续接）→ `turn/start` → 等 `turn/completed`，跑完关掉。Claude 是 `claude -p --output-format json`，新建用 `--session-id` 预定编号、`-n` 设标题，续接用 `--resume`。输入、输出、错误在 `state_dir/runs/<batch_id>.in/.out/.err`，运行记录在账本 `runs` 表。同一会话同一时间只跑一轮；超过 `max_concurrent` 的批次排队。
- **权限不归 sheepdog 管**：不传任何沙箱、审批、权限参数，全部来自宿主自己的配置（Codex 看 `~/.codex/config.toml`，`[hosts.codex] profile` 设了名字时，sheepdog 读 `~/.codex/<名字>.config.toml`，展开成 `-c` 参数传给自己起的 app-server（app-server 不接受 `-p`），你自己在 Codex 应用里的会话不受影响；配置档只对之后新开的 Codex 会话生效（2026-10-10 实测：Codex 建线程时把沙箱策略记在线程上，之后用新进程、带不同的 -c 续接也照样沿用旧策略），改了配置档，老会话要退休换新才会跟着变；配置档读不到或解析失败时这一轮直接失败、不重试，会话标 attention 并提示总线；示例见 `examples/codex-profile.example.toml`；Claude 看用户设置、workdir 下的设置，或 `settings` 指的文件，sheepdog 只传文件名）。Codex 发来的审批请求一律拒绝，方法名记进运行记录。`sheepdog doctor` 会列出每个宿主的命令、登录状态、Codex 的配置档名，以及（设了配置档就读配置档，没设读用户配置）`sandbox_mode`、网络开关、`writable_roots`；只读或网络关着时提示「能读、能分析，但发不了飞书」。
- **回执**：这两类会话多半写不了 sheepdog 的状态目录，所以给它们的批次脚注和接口说明里只写一种交法：在回答末尾附一个 ```` ```sheepdog-receipt ```` 块（内容同 `receipt --json` 的 JSON），不让它们先试回执命令（第三层实测：命令失败后会话把「交不了回执」当成受阻，回执写成 waiting_external）。一轮结束时：会话自己交过回执就以它为准；没交就取回执块；都没有就按回执超时处理。写法和「什么才算受阻」在 playbook 的 `cli_host.md`，只发给这两类会话。
- **出错**：按输出内容认，不靠退出码。容量满 / 5xx：同一批次 5、10、20 分钟后重试，最多 3 次，还不行会话标 attention、提示总线，不换模型。写锁冲突（你在 Codex 应用里看过这个线程：应用对看过的线程一直持写锁，切走也不会马上放；这条线程在应用里闲置满 3 小时，或之后又看了 10 条以上别的线程才自动放开，想马上放开就关掉应用；3 小时、10 条是 Codex 桌面版 26.1007.21159 里的常量，以后可能会变）：视同主人正在用，批次退回排队（一条不丢）；连续 60 分钟提示总线，并经机器人私聊提醒你关掉 Codex 应用（同一段冲突只提醒一次，同一会话 24 小时最多一次），说明文字来自 playbook 的 `host_locked.md`。登录失效：这个宿主整体排队，经机器人给你发一条「需要你重新登录」（24 小时一次）。超过 `timeout_minutes`：杀掉整个进程组，会话标 attention。Claude 的 `permission_denials` 记日志，并在 `sheepdog sessions` 里显示。
- **你在用时让路**：Codex 读线程的 rollout 文件（`~/.codex/sessions/**/rollout-*-<编号>.jsonl`），Claude 读 `~/.claude/projects/*/<编号>.jsonl`；sheepdog 自己发的（带 `[sheepdog]` 头的）和第一条开场不算，其他用户消息算你的发言。你的发言时间记在羊群看板卡片上（记录里来了新的用户消息才重新认一遍），投递要不要让路只看这个值。另外有交互式 `claude --resume <编号>` 在跑也算你在用（和写锁一样，每轮问宿主）。
- **重启**：`runs` 里还在跑的，进程还活着、命令行对得上就继续等；已经退出的按输出文件收尾；一律不重发。
- **查看**：`sheepdog sessions` 多了宿主、模型、档位三列和最近一轮的状态；`sheepdog show --topic tp_x` 打印最近几轮问答，并告诉你去哪接着看（Codex：应用里的同名线程；Claude：在 workdir 里跑 `claude --resume <编号>`）。
- **测试环境**：`[source] enabled = false` 时不拉 IM，用 `sheepdog inject` 把一条合成消息（编号以 `om_test_inject_` 开头）放进账本，走正常的路由和投递。
- **给你的通知不会丢（通知账本）**：经机器人私聊你的每一条通知（危险命令报警、羊群看板判中断 / 失联和已恢复、attention 和恢复、投递不出去 / 已恢复、写锁提醒、登录提醒）都先落账本（`owner_notices` 表：通知类型、会话、正文原样字节、防重编号、待发 / 已发 / 放弃、尝试次数、最近一次错误、时间），再发。发送带 `lark-cli im +messages-send --idempotency-key`（防重编号在落账本时定好，`sd-` 加 32 位十六进制，35 个字符），每次重试都用同一个编号、同一份正文字节，飞书 1 小时内同一个编号只发出一条。发不出去（比如飞书限流 429）留在待发，每一轮按退避重试（1、2、4、8 分钟，之后每 10 分钟一次），重启以后接着试；从落账本起 2 小时还没发出去就标放弃、记 ERROR 日志，`sheepdog doctor` 和 `sheepdog board` 的开头一直显示「⚠ 有 N 条给主人的通知没发出去」，直到你 `sheepdog notices --retry` 重发或 `--ack` 确认。进程在「发出去了、还没记成已发」之间没了：重启后 50 分钟以内用同一个编号补发（飞书去重）；更久了不敢补发，标放弃等你看。调用方「这件事已经通知过」的记账在发送之前做，重启以后不会为同一件事再拼一条新通知。`[notify] file` 模式照旧直接写文件，不落账本、不重试。
- **给你的通知写到文件（测试环境）**：不拉 IM 时没有私聊你的渠道，原来给你的通知（羊群看板判中断 / 失联、attention 和自动恢复、危险命令报警、写锁提醒、登录失效）只记日志。配置里设 `[notify] file = "路径"`（相对路径相对配置文件所在目录），这些通知一律追加写进这个文件，不发飞书；每条一段，头一行是 `### 2026-10-12 14:03:05（北京时间）· 羊群看板通知` 这样的时间和通知类型，下面是原文。通知类型：`羊群看板通知`、`attention 通知`、`恢复通知`、`投递失败通知`、`投递恢复通知`、`危险命令报警`、`写锁提醒`、`登录提醒`。`sheepdog.notify.read_notices()` 能把文件读回成列表。没设时行为不变。拉着飞书又设了它，写文件优先、不发飞书，`sheepdog doctor` 会提示「主人通知写到文件，不发飞书」，免得正式配置误设。`--dry-run` 只打印，不写文件。改了下一轮生效。

## 代回用卡片

会话以你的身份对外回复时发一张卡片：正文 + 右下角灰色小字标记（`session.reply_suffix`，默认 `[🐕Sheepdog Reply]`），别人一眼能看出是 Agent 代回。回复某条消息一律用 `sheepdog reply`（见上文「发前再看一眼」），它内部就用这里的卡片；只有先发起一段新的聊天时，才用 `reply-card` 加 lark-cli：

```bash
sheepdog reply --topic tp_x --to om_x --card-text '收到，周五前给你 <at id=ou_xxx></at>'
lark-cli im +messages-send --as user --chat-id oc_xxx --msg-type interactive \
  --content "$(sheepdog reply-card --text '你好，想问一下……')"
sheepdog reply-card --text-file body.md     # 正文从文件读；- 表示标准输入
```

- `sheepdog reply-card` 只打印卡片 JSON（Card 2.0：`config.width_mode` 取 `session.reply_card_width`，默认 compact；body 两个 markdown 元素：正文原样，支持卡片 markdown 与 `<at id=ou_xxx></at>`；后缀右对齐、notation 字号、灰色）。正文为空报错。
- sheepdog 拉回来的卡片内容形如 `<card>\n正文\n[🐕Sheepdog Reply]\n</card>`，所以识别代回用 `owner_context.agent_markers`（包含匹配），和旧的 `skip_prefixes`（开头匹配）并存，任一命中即算：主人发言背景不送回、防循环按它计数。

## 可靠性（外部评估后补的几处）

- **采集不漏**：每轮固定窗口 `[上次水位线 - overlap_seconds, 本轮开始]`，翻页上限 `max_pages`（默认 100，每页 50 条）。有 `has_more` 却缺 page_token、或翻到上限仍 `has_more` 时本轮记为没拉完：不推进水位线、下一轮从原起点重拉（按 message_id 去重）、同一缺口给总线提示一次；连续 3 轮 `doctor` 和 `sessions` 显示「采集不完整」。
- **投递不重不丢**：先在账本记批次 `sending` 再发送，成功后改 `sent`。进程在两者之间中断时，下一轮到会话 transcript 里找批次号：找到就补记已送达；找不到标 `uncertain`，不自动重发，`sessions` 显示，确认后 `sheepdog redeliver --batch <批次>`。
- **等别人回复要会话确认**：对方回复后等待进入「已回复、等确认」，回复照常送达，提醒和到期暂停；会话确认真等到了用 `sheepdog watch-done --id N` 或回执 anchors 写 `watch_done:N`，否则带这条回复的批次回执后回到等待、从回复时间重新计时。
- **免打扰群里回复你的消息照常直推**：「回复我」与「@我」同级，在免打扰判断之前；关键人发言仍受免打扰约束。
- **长消息**：每条最多带 `session.max_message_lines` 行（默认 200），超过的末尾提示「后面还有 N 行未显示，用 `sheepdog show <message_id>` 看全文」。
- **配置热加载**：`run` 每轮重读 config.toml；改坏了沿用上一份并记 WARNING；轮询间隔、关键词、路由、ack、watch、loop_guard、owner_context 等下一轮生效；`state_dir`、`sink` 改了只提示需要重启。名册和安全规则同样每轮重读，playbook 每次组装 prompt 时现读。

## 会话卡住（attention）：告诉你，能自己好的自动解开

会话连续失败满 3 次（总线回执超时、投递失败、Codex / Claude 这一轮失败或超时、Codex 配置档有问题、容量满重试用完）就进 attention：不再投递，新来的信号只排队，一条不丢。账本 topics 表记下进入的时间 `attention_at` 和原因 `attention_reason`（`receipt_timeout` / `no_receipt` / `send_failed` / `host_failed` / `host_timeout` / `config` / `capacity`；老账本启动时自动补这两列）。`sheepdog sessions` 和 `sheepdog doctor` 都会列出卡住的会话、从几点起、为什么、会不会自动恢复。

- **告诉你**：任何会话进入 attention（不论原因），经机器人私聊你一条：哪个会话、原因、从几点起没回应（最后一次回执或会话记录里最后一次动作，按 `timezone_offset` 显示，默认北京时间）、排了几条信号、会不会自动恢复。同一次 attention 只发一次。末尾附 playbook 的 `attention_notice.md`（可选，缺了只发事实）。原来给总线的系统提示照旧；卡住的就是总线自己时，这条提示不发给它自己（它收不到），只私聊你。
- **自动恢复，只限回执超时**：因回执超时卡住的会话，出现下面任一证据就自动回到可投递，同一轮补送排队的信号，日志写「会话恢复，自动解除 attention（依据：…）」：
  - 晚到的回执：它交了任何一批的回执，包括已经判超时的那批；
  - 会话又有新动作：进 attention 之后，会话记录里又出现了动作（复用护栏的增量读）。Antigravity 看 `PLANNER_RESPONSE` 和工具调用的执行行（`GENERIC`，source 是 MODEL），Codex 看 `item_completed`，时间都要晚于进 attention 的时间；断网时写的 `ERROR_MESSAGE` 不算。Claude 的记录格式没核实过，只认晚到的回执。

  如果之前给你发过 attention 通知，恢复时再发一条短的「已恢复，补送 N 条」，末尾附 `attention_recovered.md`（可选）。其他原因的 attention 不自动解，处理好以后 `sheepdog session-reset --topic <topic>` 复位（同时清掉 attention 的时间和原因）。
- **晚到的回执不丢**：判过回执超时的批次（总线的 failed、managed 会话的 missed）后来交了回执，回执存进那一行投递记录，状态标 `acked_late`（error 保留当时的「回执超时」）；回执里的等待确认（`watch_done:N`）和锚点照常处理。那批里超时后退回排队、还没再投出去的消息，会话其实已经处理过，改记为这一批已回执，不再重投；已经随后面的批次重投出去的不动。总线超时一两次、已经重投在等回执时收到前一批的晚到回执，失败计数清零（等同一次正常回执）。
- **已知局限**：恢复看的是「会话还活着」，不是「这一批处理完了」；会话恢复后补送的信号仍按正常回执和超时走。回执超时一律 20 分钟、3 次就 attention 的阈值没改。

## 羊群看板：会话之间实时感知、互相沟通

`sheepdog board` 给每个会话一张卡：职责、运行状态、最近一次工具调用的动作说明（截一句，附几分钟前；运行状态是「在做」时叫「此刻动作」，其他状态都叫「最近动作」，免得把 30 小时前的动作当成正在做的事）、手上的事、最近做完的 3 件、未读留言数，还有排队几条、是否空转、是否刚被压缩。会话和你都能查；`--topic tp_x` 只看一个会话，并附最近 3 条回执和 5 个动作；`--json` 给程序读（动作字段名仍是 `action` / `action_at`，另有 `action_label` 写明是「此刻动作」还是「最近动作」）。

**登记只让信息更全，不是正确性的前提。** 会话登不登记（`work set`）、登得对不对，都不改运行状态，也不改任何投递和通知的决定：运行状态只看会话记录、投递状态、回执、`watch`、「需要你定」这些事实；登记只出现在「手上的事」和撞车提醒的措辞里，并且标明是「会话自报」。这一点有差分测试守着：同一个随机剧本跑两遍，一遍会话乱登记、一遍完全不登记，每一轮投给每个会话的消息、给你的每条通知、每张卡片的运行状态都一模一样。

**手上的事** = 还开着的活 + 还算数的路由事实，每件标来源：
- 自动（牧羊犬看事实补的）：`自动·路由`（带这个编号的消息 `routed_active_hours`（默认 24）小时内送到了这里，写最近一次几点、共几条）、`自动·等人回复`（`watch`）、`自动·需要你定`、`自动·转交`（`forward`）；
- `会话自报`（`work set`、回执里的 `work`）。自报、还开着的活 `stale_hours`（默认 4）小时没更新、而且这段时间会话一直空闲，标「⚠ 可能过期（4 小时没更新）」，board、摘要、撞车提醒里都带；不替它关掉。

账本里新增五样（老账本启动时自动建表、补列）：

| 表 | 是什么 | 谁写 |
|---|---|---|
| `duty_cards` | 会话卡片 | sheepdog 每轮算 |
| `work_items` | 活：钥匙、标题、负责会话、状态（进行中 / 等别人 / 等主人 / 已完成 / 已转交）、等谁、到几点、答应交出什么、结果、更新时间、会话最后一次自报的时间 | 会话申报，sheepdog 自动补 |
| `duty_routes` | 路由事实：(会话, 编号) 一行，带这个编号的消息最近几点送到、共几条（同一条重投不重复算）；会话转交出去或自报做完时记收掉的时间，这之前的不再算，之后又送到就重新算 | sheepdog 投递时记 |
| `mailbox` | 信箱：发件会话、收件会话、钥匙、正文、时间；已读看带它的批次有没有回执 | `sheepdog tell` |
| `duty_history` | 履历：回执摘要、做完的活、最近动作 | sheepdog |

**每轮怎么算**（拉消息、路由不变，羊群看板不参与路由）：

1. **撞车检查**：用 `[duty] key_patterns` 从消息正文认编号（默认：审批单号 12 位数字、`PR #123`、Linear 单号 `ABC-123`）。以路由事实为主：这个编号的消息最近送到过别的会话，就附一行「🔀 撞车提醒：202610120022 的消息最近送到了「发版」（topic `tp_release`）：最近一次 12 分钟前，共 3 条；它现在在做，最近动作「…」5 分钟前」；别的会话手上还开着这把钥匙的活（自动补的、会话自报的）也算，写成「…在「发版」手上；会话自报：进行中（更新于 10 分钟前）」，可能过期的带上那几个字。后面接 playbook 的 `duty_collision.md`（多了 `{{basis}}`：路由 / 会话自报 / 自动）。只提醒不拦，送给谁不变。sheepdog 自己生成的消息（留言、提醒）不查、也不记路由事实。
2. **运行状态**，按顺序取第一个命中：
   - **被主人占着**：你在会话里亲自说话（会话状态 `human_attached`，或 `human_attach_minutes` 内说过话：Antigravity 看记录里的 `USER_INPUT`，Codex / Claude 看记录里不是牧羊犬发的、也不是第一条初始提示词的用户消息），或 Codex 应用开着它（写锁冲突）。「你说没说话」只有一个判断：卡片上记的那个时间；投递要不要暂停也看它，卡片和投递不会一个说占着、一个照投（只打印模式也一样）；
   - **失联**：后台进程没了还没交结果（Codex / Claude 这一轮「进程退出，没有结果」），或批次送出后会话记录 `lost_minutes`（默认 30）分钟完全不动（文件没写过），或**找不到会话记录**（Antigravity / Codex 的会话被删了、目录被挪走、找不到 rollout）而手上有没交差的批次、从这一批送出起满 `lost_minutes`（依据写「找不到会话记录（会话编号），批次 HH:MM 送出后无从判断」；新建的会话第一步之前可能还没有记录文件，所以不用 `stall_minutes`）。找不到记录、手上又没有没交差的批次时运行状态不变，卡片的最近动作写「（找不到会话记录）」；
   - **中断**：批次送出后 `stall_minutes`（默认 15）分钟没有新动作（会话没碰过它，或者动过以后停下了；动过以后停下的，从最后一次动作算起，只要那之后手上一直有没交差的批次——总线回执超时重投同一批不重新计时），或这一轮失败、超时、卡在 attention；
   - **在做**：批次在跑、`stall_minutes` 内有新动作；同一条命令 / 同一个工具调用连续 `spin_repeat`（默认 3）次以上标「空转」；
   - **等回复**：最近回执是 `waiting_external` / `needs_decision`，或登记了还开着的 `watch`（等别人回复），或有没答的「需要你定」。只看这些事实，会话自报的「等别人」「等主人」不算；
   - **空闲**：附上一件做完了什么（或上一批回执的摘要）。

   会话记录的读法复用护栏的增量读（`transcript_cursors` 新增用途 `duty`；第一次看某个会话时读记录末尾 4 MB，之后只读新写的行）。「动作」的认法同 7.21：Antigravity 看 `PLANNER_RESPONSE` 和 `GENERIC`（source 是 MODEL），Codex 看 `item_completed`；断网时写的 `ERROR_MESSAGE` 不算。卡片上的动作只取工具调用的动作说明：Antigravity 是 `tool_calls[].args.toolSummary`，没有就 `toolAction`（都是 JSON 编码过的字符串，先解一次），再没有就是工具名；Codex 是跑的命令（脱敏、截短）。不读模型的思考和回答正文。Claude 的记录格式没核实过，不认：Claude 会话只按投递状态判。
3. **自动补活**（只影响「手上的事」，不影响任何决定）：`watch` → 等别人（等谁、到几点按 `[watch] expire_minutes` 算）；「需要你定」→ 等主人；`forward` → 转出方名下「已转交」。钥匙先按编号规则从等待的备注、提问正文、转交的消息里认，认不出用 `watch-<id>` / `ask-<…>` / `fwd-<…>`。会话自己申报过同一把钥匙的，以申报为准，只补缺的字段；等待结束、问题答了或过期，自动补的活跟着收掉。
4. **打包批次**：撞车提醒附在对应消息上；给这个会话的留言作为「会话留言」排在批次里；总线批次在羊群看板有变化时再附一段摘要，标题来自 `duty_digest.md`：不空闲的会话（被占着、失联、中断、在做、等回复）每个一行，写运行状态、此刻 / 最近动作、手上的事（每件写「状态·自报 / 自动」，路由事实写「路由」，可能过期的加一句）、未读留言；空闲的合成一行「空闲 N 个：标题1、标题2……」（标题太长截短，整行超过约 120 字用「等」收尾）。最多 `digest_max_lines` 行，超了先留卡住的，砍掉的合成最后一行，写明省略了几个、各是什么状态。什么算变化：会话进出空闲；不空闲的会话运行状态、空转、手上的事（钥匙、状态、来源、有没有路由事实、可能过期）、未读留言变了。空闲会话的最近动作、任何会话的「几分钟前」和动作变了都不算。空闲、等回复 → 投递；在做、被主人占着 → 排队（原来的状态机就是这样）；**中断、失联 → 排队，并经机器人私聊你**（末尾附 `duty_stuck.md`）。「一段」是会话的一段沉默：从它最后一次动作（或 `session-reset`）算起，中断、失联各最多通知一次；这期间重投、回执超时以后又送的批次、卡片在中断和失联之间来回，都不再通知；会话一动（或 reset）才算新的一段。主人在会话里说话的 15 分钟里卡片是「被主人占着」；主人走的那一轮先按新状态重算卡片再决定投不投，还卡着就继续排队。会话记录里一有新动作就自动恢复投递；你确认处理好了也可以 `sheepdog session-reset --topic tp_x`，它同时清掉这次判定（在这之前送出、会话没碰过的批次不再算）。
   - 已经卡在 attention 的不另发。同一段羊群看板已经说过「中断 / 失联」再进 attention 的，attention 通知只补一句「🐕 汇报 · 会话仍卡着：……仍卡着，已停止重投」；没说过的照旧发完整的 attention 通知。
   - 说过「中断 / 失联」的这一段结束了（会话又有动作、卡片不再是中断 / 失联），补一条「🐕 汇报 · 会话已恢复」；进过 attention 的，由 7.21 的恢复通知说这一句，不重复。session-reset 结束的一段不说。所以一次断网最多 4 条：中断、失联、仍卡着、已恢复，最后一条一定是「已恢复」。
   - 总线的中断通知不写「暂不投递」：总线回执超时以后照样重投（failed）。会话 `failed`（这一轮失败、下一轮重投）卡片上显示中断，但照旧重投，不发通知；连续失败满 3 次进 attention 时由 7.21 通知。只有一种例外：failed 时卡片是「被主人占着」（你正在会话里说话），排队，等你走了再重投。
   - 投递本身失败（agentapi 发不出去、宿主没开）：同一会话连续 3 次，私聊你一次「🐕 汇报 · 投递不出去」，消息照常排队、每轮重试；之后发出去了再说一句「投递已恢复」。同一段失败只说一次；这一段里会话进了 attention 的，由 attention 通知说。

**会话一侧的命令**（立即写账本，不等下一轮）：

- `sheepdog work set --topic tp_x --key K --title T --state S [--waiting-on 谁] [--until 几点] [--expect 一句话] [--to 会话]`；`--state` 认 `doing / waiting_other / waiting_owner / done / handed_off`，也认中文（进行中 / 等别人 / 等主人 / 已完成 / 已转交）；`handed_off` 要写 `--to`。钥匙有外部编号用外部编号，没有就起短名；比较时不分大小写、不看空格（`PR #12` = `pr#12`）。同一把钥匙在别的会话名下没结束：提示「某某在做」，仍然登记。
- `sheepdog work done --topic tp_x --key K --result 一句话`。
- Codex 等写不了账本的会话：回执 JSON 带 `"work": [{"key": ..., "title": ..., "state": ..., ...}]`，效果同 work set。
- `sheepdog tell --topic tp_x --to tp_y "话" [--key K]`：存进信箱，作为一条系统消息排给对方，下一批送到（对方排队、被占着、离线都不丢）。
  - 对方看到的抬头固定是「💬 会话留言（不是主人的指令）· 来自「某某」」，正文按引用格式送，后面接 `duty_mail.md`。
  - 留言不能冒充主人：正文里出现「主人原话」「已核对」就拒绝，除非用 `--quote "<主人原话>"`，核对同 new-session（总线里亲口说过，或主人本人账号在飞书上的回复），通过了按已核对的标法送。
  - 同一对会话（两个方向合计）`mail_window_minutes`（默认 10）分钟内超过 `mail_pair_limit`（默认 6）条：这一条拒绝，这一对暂停 `mail_pause_minutes`（默认 30）分钟，并给总线排一条提示（附被拒的那条）。

```toml
[duty]
key_patterns = [ ... ]         # 不配就是代码里的默认一份（见 examples/config.example.toml）
stall_minutes = 15
lost_minutes = 30              # 不能小于 stall_minutes
spin_repeat = 3
digest_max_lines = 15
mail_pair_limit = 6
mail_window_minutes = 10
mail_pause_minutes = 30
compacted_recent_minutes = 60  # 多久内的压缩点标「刚被压缩」
routed_active_hours = 24       # 路由事实多久内算「手上的事」、参与撞车提醒
stale_hours = 4                # 自报的活多久没更新、期间一直空闲就标「可能过期」
history_days = 7               # 履历、已结束的活、已读留言保留几天
```

- `[duty]` 热加载，下一轮生效；`sheepdog doctor` 显示阈值和现在中断 / 失联的会话。
- 接口说明（每个会话的常驻规则）里加了「在岗表」一节，上线后各会话下一批会带一次完整现行规则（同「规则改了自动同步」）。
- 2026-10-12 改名「羊群看板」（原来叫「在岗表」）：只改给人和会话看的文字（board 标题、总线摘要抬头、通知类型「羊群看板通知」、日志、命令帮助、接口说明那一节的标题）；命令名 `board` / `work` / `tell`、配置段 `[duty]`、账本表名列名、`--json` 字段名都不变。接口说明的标题变了，上线后各会话下一批也会带一次完整现行规则。
- 演示：`python3 scripts/demo_duty_board.py`，在临时目录里用合成数据跑一轮（宿主 dryrun），打印 board、总线收到的批次、卡住的会话的判定和给你的通知；`--keep` 保留临时目录接着手动试。
- **已知局限**：只判「会话在不在动」，判不了「这一批做完没有」；Antigravity 会话安静地想很久（不调工具）会被判中断。这一版不做签名写入、epoch、足迹申报：sheepdog 一件活只交给一个会话。

## 引用回复按引用走

你在飞书里引用回复机器人的某条消息时，sheepdog 按被引用的那条送回，不会当成「没引用」去猜：

```toml
[escalation]
attribution_regex = '\[[^\[\]]*?(?P<topic>tp_[a-z0-9_.-]+)\]'   # 第一行里 [… tp_xxx]

[escalation.aliases]
"总线" = "tp_bus"
```

- 「找你」聊天里机器人发的每条消息（问题、汇报、回复）都记来源到账本 `bot_outbox`：第一行先用 `attribution_regex` 找 topic；找不到时把第一行 `[..]` 标签按「·」拆开，逐段先查 `[escalation.aliases]`，再和各会话标题比（去掉 title_prefix 后相等或互相包含），唯一命中才算。同时匹配 `header_regex` 的还照旧登记为「需要你定」。
- 你的回复：引用了问题或登记过来源的消息 → 送回那个会话（引用的是问题就关闭它）；引用了认不出来源的消息 → 送总线，附「你引用回复了一条认不出来源的消息」和原文摘要，**绝不**按「没引用」去匹配未结问题；没有引用 → 规则不变。
- 投递时，任何有引用的消息（你的、别人的）都在标题下附一行「↪ 回复的是：<发送人和摘要>」（账本里没有就写「原文不在账本里」）。

## 一事一串：找你的消息按事归到话题里

会话找你一律用 `sheepdog ask`（需要你定）/ `sheepdog report`（汇报），不再自己调 lark-cli。sheepdog 在正文前加开头一行（`[threads] ask_header` / `report_header`，默认 `🐕 需要你定 · <标题> [tp_xxx]` / `🐕 汇报 · <标题> [tp_xxx]`），以机器人身份私聊你；这一行照旧被 `[escalation] header_regex`、`attribution_regex` 认出来（`sheepdog doctor` 会核对 ask 的开头一行能不能被 header_regex 认出来）。

```bash
sheepdog ask --topic tp_x --key "PR #12" --title "发版时间" --text "周五还是下周一？建议周一"
sheepdog report --topic tp_x --key "PR #12" --title "进度" --file body.md     # - 表示标准输入
```

- **带 `--key`**（和羊群看板的活同一把钥匙，按 `norm_key` 比较：去空白、转大写）：同一会话同一把钥匙第一次发，在主聊天里新起一条（话题开头）；之后再发（问题、汇报都算），用 `+messages-reply --reply-in-thread` 回到开头那条的话题里，主聊天里不再并排。账本 `owner_threads` 表记「话题 ↔ 会话、钥匙、开头那条、话题编号」。`--reply-in-thread` 的返回里没有话题编号（实测只有 chat_id、create_time、message_id），所以往话题里发完一条，sheepdog 只读 mget 一次开头那条把话题编号补上；mget 失败不影响发送结果，之后从拉取里补。不同会话用同一把钥匙是各自的话题。ask 照旧登记「需要你定」，羊群看板自动补的「等主人」的活用的就是这把钥匙。
- **不带 `--key`**：和以前一样，每次主聊天里一条新消息。
- **防重**：每次发送带 `--idempotency-key`（由种类、会话、钥匙、批次、正文算出来，固定 44 个字符）。批次不用会话自己传：不带 `--batch` 时取这个会话当前在跑的批次（等回执的那批），没有就取它最近一批，都没有才留空；所以同一批里重跑只发一条，隔批再说同样的话照常会发。发成功的记进账本 `sends` 表，重试时先查它，发过的不再调飞书；超时、不确定发没发的，原样重试，带同一个编号，飞书只发一条（飞书文档说同一个编号 1 小时内只发一条）。发出去的同时记进 `bot_outbox`，你引用回复它照旧能送回。
- **失败**：发送失败或超时返回退出码 1 和原因，不假装成功，也不记账。

### 你在话题里回复：按话题送回

话题里的消息拉回来只有 `thread_id`，没有 `parent_id`。话题里的序号 `thread_message_position`（入账时记进 `messages.thread_position`）：普通聊天（p2p、group）开头那条是 `"-1"`、回复从 `"0"` 起；话题群（`chat_type = topic`）开头那条是 `"0"`。按聊天类型判断，没有写死。

sheepdog 拉到你在「找你」聊天里发的、带 `thread_id`、没有 `reply_to`、序号不是这个聊天类型开头序号的消息（也就是话题里的回复）时：

1. 先按话题表找会话和钥匙；
2. 再看账本里同一话题的消息：序号是开头序号的那条就是开头（老消息后来有了话题，再被拉到时 sheepdog 会给账本里那行补上话题编号和序号）；ask / report 发进话题里的，按发出时记的开头；
3. 再用 mget 查候选：sheepdog 在这个聊天里发过、还没记话题编号的话题开头，加上这个聊天里保留期内的「需要你定」和机器人发给你的消息，每次最多 50 条（最多看最近 200 条），看谁的 `thread_id` 对得上；
4. 找到开头那条以后按现有的识别认会话：「需要你定」、`bot_outbox`、第一行；认出来就补记进话题表（`source = recognized`，没有钥匙）；
5. 都认不出（或 mget 失败），照「认不出来源」交给总线，附开头那条（或查不到的原因）。

话题列表（`+threads-messages-list`）实测只返回话题里的回复、不含开头那条，所以只用来取回复，不用来找开头。

有引用（`reply_to`）的照旧按引用走，不变。话题就是一件事：你在话题里回复，这件事里没结的「需要你定」投递后都标已答。

送回会话的批次按条数渐进披露（参照 cc-connect 回复 Linear 评论的做法，「复杂」按条数判断，不让模型判断）：

- **一般情况**：你的回复，加「这件事开头那条」（截到 `[threads] root_chars`，默认 300 字）；话题里最近一条「需要你定」不是开头那条时，再附一行它的摘要。
- **复杂情况**：话题里除了开头那条已经有 `complex_min_replies`（默认 3）条以上（含你这一条，也就是话题里第 4 条起），再附最近 `recent_replies`（默认 5）条（每条截到 `reply_chars`，默认 120 字），并写一句：要全貌用 `sheepdog thread --topic tp_x --key K` 取整个话题；还不够就看羊群看板里这件活（`sheepdog board --topic tp_x`）和最初的来源消息。

`sheepdog thread --topic tp_x (--key K | --thread omt_x)` 只读：开头那条从账本、sheepdog 发出的记录或 mget 取，回复从账本和话题列表取，合在一起列出，标明每条来自账本、sheepdog 发出还是飞书；不写账本里的消息、不动拉取进度、不点表情。话题记在别的会话名下的不给看；话题表里没有的只有总线能看。

## 发前再看一眼：代回改由 sheepdog 发

会话代回别人一律用 `sheepdog reply`，不再自己调 `lark-cli im +messages-reply --as user`：

```bash
sheepdog reply --topic tp_x --to om_x --card-text "收到，今天下班前给结论"   # 卡片，同 reply-card
sheepdog reply --topic tp_x --to om_x --card-file body.md                    # - 表示标准输入
sheepdog reply --topic tp_x --to om_x --text "收到"                           # 纯文本，末尾也附代回标记
```

发之前**只读检查**：这个聊天里、在要回复的那条之后（它所在批次里同一聊天还有更晚的消息时，以最晚那条为准），你本人有没有新发言（带代回标记的会话代回、同一批里已经送到的不算）。先后优先按搜索结果里的 `message_position`（消息在这个聊天里的序号，入账时记进 `messages.message_position`）比：你的发言序号更大才算「之后」；有一边没有序号时，才退回按时间比（飞书时间只到分钟，同一分钟里的一律算「之后」，偏保守）。检查只用搜索接口（`+messages-search --chat-id --sender --start`，不会把消息标成已读）：不写账本里的消息、不动拉取进度、不点不撤表情、不改已读。账本仍然是唯一的入口，消息照样由每轮拉取进账本、照常路由。

| 退出码 | 意思 | 会话怎么做 |
|---|---|---|
| 0 | 发出去了；或同样的内容已经发过，这次没再发 | — |
| 1 | 发送失败或超时 | 原样重试（措辞不要改），防重编号保证只发一条 |
| 2 | 参数不对、会话不能发、账本里没有要回复的那条 | 改参数 |
| 3 | 你已经回过，没发；你的话（时间和内容）原样打印在输出里 | 先读你的话再决定；确实还要照样发，加 `--even-if-owner-replied`（跳过检查） |
| 4 | 检查本身失败（接口报错、超时），没发 | 过一会儿再试，不当成「你没说话」 |

- 退出码 3 时，sheepdog 同时在账本 `seen_before_send` 记「这个会话发送前已经看到过这条」（会话 + 消息号，重复记不变）。这条发言之后作为背景送到这个会话时注明「你发送前已经看到过这条」，只送一次，不重复提醒。
- 防重编号用「批次号 + 被回复的消息号 + 正文指纹」（账本里拿不到批次号时用会话 + 消息号 + 正文指纹）：内容一样才算重复。同一条先回「ok，稍等」、处理完再回结论，两条都发；同样的内容重试只发一条。代价：发送超时以后改了措辞再重试，可能多发一条。发出的记进 `sends` 和 `bot_outbox`。
- 测试环境（`[source] enabled = false`）：ask、report、reply 不发飞书，设了 `[notify] file` 就写进那个文件（不写账本），没设就拒绝。

### 真实飞书测试

一事一串要在真实飞书上看效果（话题回复的提醒、显示），又不能动你的机器人私聊（正式的牧羊犬也在读它）。测试时另起一个只打印投递的牧羊犬（`sink = "dryrun"`，用自己的配置和状态目录），配上两个**只给测试用**的开关，让它只碰一个测试群：

```toml
[source]
only_chat_ids = ["oc_测试群"]     # 只拉这个群：别的聊天的消息不入账本，也不点表情、不查已读

[threads]
owner_chat_id = "oc_测试群"       # ask / report 发进这个群（+messages-send --chat-id），不再私聊你
```

- `only_chat_ids`：拉到的消息只保留这些聊天的；其他聊天的消息直接扔掉、**不入账本**（不是入账本再丢），所以也不会点表情、查已读。没设时行为不变。
- `owner_chat_id`：ask / report 发进这个群，往话题里回照旧用 `+messages-reply --reply-in-thread`；这个群同时当成「找主人」聊天（等同于加进 `[escalation] chat_ids`），你在群里的回复、话题回复都按 owner_reply 送回会话。「你」仍按 `self_open_id` 认；机器人发的、带代回标记的会话代回，都不算你的发言。没设时照旧私聊 `self_open_id`。
- 校验：设了 `only_chat_ids`，`owner_chat_id` 就必须在里面；设了 `owner_chat_id`，`[escalation] header_regex` 不能为空。不满足就报配置错误。
- `sheepdog doctor` 设了就提示「⚠ 只拉这些聊天：…（测试用）」「⚠ 找主人改发到群 …（测试用）」。
- **正式环境不要设这两个开关。**

## 区分智能体身份和主人本人（7.26，`[identity]`，默认关）

同事反映有卡片标记也常分不清是主人本人还是机器人。打开 `[identity] enabled = true`（改了要重启 `sheepdog run`）以后：

- **两个身份都听，进同一个账本**：主人身份每分钟搜索照旧；`sheepdog run` 另起一个常驻子进程 `lark-cli event consume im.message.receive_v1 --as bot`，逐行读事件，主循环每轮入账。两边收到同一条按 message_id 只记一条，`messages.received_by` 记 `user` / `bot` / `both`（关着时为空）。机器人先收到的新消息先用主人身份 `+messages-mget` 取一份（群名、发送人名字、租户、序号齐全，正文渲染和搜索一样），取不到（主人不在的聊天、私聊机器人）才用事件里的；之后主人身份搜到同一条只合并，不当成编辑。子进程退出就退避重启（`restart_backoff_seconds` 起翻倍，最多 `restart_backoff_max_seconds`）。机器人的 open_id（`bot/v3/info`）和 app_id（`lark-cli config show`）启动时查一次记进账本，查不到可在 `[identity] bot_open_id` / `bot_app_id` 手填。`--dry-run` 不起接收。
- **@机器人 = @主人**：路由原因 `at_bot`，排在免打扰之前，其余规则和 `at_me` 一样（确认表情没单独配 `at_bot` 时按 `at_me` 算）。别人回复机器人发的消息和回复主人发的一样，原因仍是 `reply_to_me`。
- **机器人自己的消息认作自己**：路由 `self` / `self_bot`，不分派、不当成主人的发言（背景、发前检查都不算）。防循环把机器人身份的代回也算进次数，判「对方是不是 Agent」时不看机器人自己的消息。
- **别人私聊机器人不接**：主人在飞书里看不到这些私聊。账本记 `drop` / `bot_dm`，不分派；机器人回一句 `playbook/bot_dm.md`（可选文件，缺了用代码里的默认话），请对方直接私聊主人本人；同一个人 `bot_dm_reply_hours`（默认 24）小时内只回一次，别的机器人私聊过来不回。主人和机器人的私聊（找主人聊天）照旧。
- **代回自动选身份**（`sheepdog reply`，会话不用管）：

| 被回复的消息在哪 | 用谁 | 卡片标记 |
|---|---|---|
| 别人和主人本人的私聊 | 主人本人 | 加 |
| 群，机器人在群里 | 机器人 | 加 |
| 群，机器人不在群里（或查不到机器人的群列表） | 主人本人 | 加；机器人不在的群记下来，`sheepdog doctor` 列出 |

  机器人在不在群按机器人身份的群列表判断，缓存 `bot_chats_cache_minutes`（默认 10）分钟；机器人发送报 230002（不在群里）时作废缓存，这一条退回主人本人，内容和防重编号都不变。两个身份都带代回标记（Matthew 10-13：「这是我的logo 有广告意义」），防重编号和以前一样按带标记的正文算。发前再看一眼、防重编号两个身份都一样，`sends.identity` 记用的哪个身份。ask / report 照旧用机器人，主动新发照旧。
- `sheepdog doctor` 的「身份区分」一节：机器人 open_id / app_id、接收子进程在不在、最近一次收到事件的时间、重启次数、最近一次退出原因、机器人在几个群、机器人不在而退回本人的群、这段时间自动回了几次私聊。



主人自己发的消息原本只入账（供「回复我」判定和撤确认表情）。开启 `[owner_context]` 后，它们会作为背景送给负责的会话，避免各说各的：

```toml
[owner_context]
enabled = true
follow_hours = 24              # 没有归属时，送给这么久内最近处理过该聊天的会话（含总线）
skip_prefixes = ["🐕 [Agent 代回] "]   # 正文以这些开头的是会话代回（旧前缀），不送
agent_markers = ["[🐕Sheepdog Reply]"]   # 正文任意位置包含这些的也是会话代回（代回卡片的标记），不送
reaction_lookback_hours = 24
reaction_check_minutes = 5     # 0 = 不查表情
```

- 只处理你本人发的、不在 `[escalation] chat_ids`、不在 `ignore_chat_ids` 的消息；会话代回的跳过（正文以 `skip_prefixes` 开头，或任意位置包含 `agent_markers`）。
- 去向：聊天归名册 / 总线新开的会话 → 送它；否则送 `follow_hours` 内最近一次投递过这个聊天消息的会话（含总线）；都没有就不送。会话已关闭的不送。
- 以背景形式随批投递，reason `owner_context`，标「📝 主人本人在该聊天的发言（发给对方的，不是给你的指令）」，附它回复的是谁的哪句话。不点确认表情、不走安全闸、不算信号；只有背景的批次不要回执、不改会话状态，信号批次超时重投时背景也不跟着重投。受人类接管排队约束。
- 你编辑或撤回自己的发言：按同样的去向送「📝 主人编辑了他的发言：旧 → 新」/「📝 主人撤回了他的发言：原文」；原文或新文是会话代回的不送。
- 你点的表情：每 `reaction_check_minutes` 分钟，对 `reaction_lookback_hours` 内投递过的消息用 `im reactions batch_query`（user 身份，只读，每次 20 条、每条取前 10 个表情）查一次，找出你本人新加的（排除 sheepdog 以你身份点的确认表情），送「📝 主人对这条消息点了 <表情>」；已报过的不重复报，第一次检查只记基线不报。去向：名册 / 总线新开归属优先，否则投递过那条消息的会话。
- `sheepdog inbox` 和总线的 Inbox 摘要里，每个群显示「主人最近发言 <时间>」。

## 防止和 Agent 机器人来回循环

会话以你的身份回复了会自动回话的机器人，机器人又 @ 你 / 回复你，sheepdog 当成新信号再推给会话，就会来回循环。`[loop_guard]`（不配也按缺省值生效）：

```toml
[loop_guard]
agent_sender_names = []          # 发送人名字包含其一（不分大小写）即视为 Agent
agent_sender_ids = []            # 按 open_id / app_id 精确指定
treat_all_bots_as_agents = true  # sender_type 为 app / bot 的一律按 Agent
max_agent_replies = 4
window_minutes = 10
cooldown_minutes = 30
```

- **Agent 发送人**（三条任一）：照常路由投递（可能是真告警），消息上加一行标注，tags 加 `agent_sender`。标注文字来自 playbook 的 `agent_sender_label.md`（回不回、怎么回写在这里；示例是「礼貌回一次，不来回对话」）；没有这个文件就用代码里的默认值「🤖 来自机器人 / Agent」，只说是谁发的，不带回不回的指示。
- **熔断**：统计每个聊天里会话代回的条数（你本人身份、按 `owner_context` 的 `skip_prefixes` / `agent_markers` 识别）。`window_minutes` 内超过 `max_agent_replies`，**并且窗口内最近一条非你本人的消息来自 Agent 发送人** → 冷却 `cooldown_minutes`（对方是真人、或窗口内没有别人的消息时不熔断，只记 INFO「代回频繁（对方为真人，不熔断）」）：期间该聊天除你以外、本该直推或进 Inbox 的消息一律进 Inbox（reason `loop_guard`），不投给任何会话，等待也不算收到回复；被 drop 的照旧 drop。记 WARNING，给总线排一条「⚠️ 疑似循环」提示（每次触发一条，冷却中不重复）。冷却结束自动恢复；你本人的消息不受影响。`skip_prefixes` 和 `agent_markers` 都为空时识别不了代回，熔断不生效（doctor 会提示）。
- `sheepdog loops` 列出冷却中的聊天和最近触发记录；`sheepdog loops --clear <chat_id>` 手动解除。`sheepdog doctor` 显示配置。

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

## 护栏：压缩后重推规则、每批红线、危险命令报警

会话的上下文被宿主压缩以后，开场时给的规则可能整段丢掉，会话自己不知道。sheepdog 每轮增量读各会话的记录（每个会话、每种用途记一个读到的字节偏移，存在账本 `transcript_cursors` 表；只读新写的完整行，不整份重读），补三道护栏：

- **压缩后重推规则**：记录里出现新的压缩点，就清空这个会话的规则指纹，下一批带上完整现行规则（同「规则改了自动同步」），规则前加一句话说明为什么又发，这句话来自 playbook 的 `rules_after_compaction.md`，没有这个文件就用一句默认话。日志写「检测到压缩」；`sheepdog sessions` 在送达前显示「上下文 … 被压缩过」。
  - Antigravity 会话认出压缩时手上有批次在跑（或者有没交差的批次：判过回执超时、之后还没交过回执的——总线退回重投的，以及名册 managed、总线新开的会话回执超时记成 missed 的；发版这类会话常干超过 20 分钟的长活，回执超时以后多半还在做），不等下一批：同一轮就单独发一条系统消息，内容是完整现行规则（和下一批重推的一样），开头写明是认出了压缩才补发、这条不是新批次、手上的批次照常做完交回执。这条消息不改批次记账（不记 dispatches、不改在等回执的批次、不影响回执超时和羊群看板），发出去以后规则指纹记成最新，下一批不再重推。同一个压缩点只补发一次（记在账本 meta 的 `compaction_resent:<topic>`，重启、记录重读以后也一样）；发送失败不重试，退回「下一批带上」。你正在会话里（human_attached）时不插话。
  - Codex / Claude 会话一轮跑着的时候送不进去，照旧下一批重推。
  认的压缩点：
  - Antigravity：一行 `"type": "CHECKPOINT"`，content 以 `# Resuming from a compaction` 开头；
  - Codex：`type = response_item` 且 `payload.type = compaction`（老文件里的顶层 `type = compacted` 也认）；
  - Claude：`"subtype": "compact_boundary"`（没核实过，有就认）。
- **每批红线**：playbook 下的 `reminders/<key>.md`（key 是名册的 key，总线是 `bus`，总线新开的会话用它自己的 key）存在就附在这个会话每一批的最末尾（footer 之后）。只附普通批次，开场、登记通知、退休通知、规则更新不附。可用通用占位符。`sheepdog doctor` 列出哪些会话有红线，以及没有对应会话的红线文件。
- **危险命令报警**：每轮增量扫 `[audit] topics` 对应会话新跑的命令（Antigravity 是 PLANNER_RESPONSE 里 `run_command` 的 CommandLine；Codex 是 CommandExecution，以及代码模式里 `exec_command({cmd: "..."})` 的命令；Claude 是 Bash 工具调用，没核实过）。命令先整理（续行合并、空白压成一个空格），再和 `danger_patterns` 逐条正则 search。命中后：
  1. 经机器人私聊你：哪个会话、几点、什么命令（截到 200 字，`--data`、`token=`、`password=`、`secret` 之后的值隐去）；
  2. 给犯规的会话排一条系统提示（事实由 sheepdog 写，后面附 playbook 的 `audit_offender.md`）；
  3. 给 `[audit] also_notify` 里的会话各排一条系统提示（附 `audit_notify.md`）。
  命中 danger_patterns、但同时命中任一条 `ignore_patterns` 的不报。默认忽略：查帮助（`--help`、`-h`）、知识库记录脚本（`context_memory.py`、`pkm.py`）、sheepdog 自己的命令（包括 forward 的备注）、IM 收发（`lark-cli im`，消息正文里提到这些命令名）、`--dry-run`。忽略判断对整条命令做，不逐段拆：一条复合命令里如果既有危险命令、又有 `lark-cli im`（或其他忽略项），会被一起忽略。这是为了让总线写复盘记录、发消息时提到命令名不误报而接受的取舍。
  同一条命令按（会话、step）去重。合并只合并已经报过的同一条命令：同一个会话 10 分钟内已经报过的命令（按整理、脱敏后的命令文本比）再跑，只记一笔、并进那次报警，不再报；之前没报过的命令马上再报一次（同一轮里新出现的几条合成一条，同一条命令文本只列一行、标次数）。每次报警都给你发一条、给犯规的会话再排一条停手提示、照旧知会 `also_notify`，三处都写明「这是这个会话 10 分钟内第 N 次报警」。报不报只看命令有没有报过，不看上一条报警发出去没有：上一条还在通知账本里待发，也压不住新的。

```toml
[audit]
topics = ["tp_bus"]            # 审哪些会话
danger_patterns = [ ... ]      # 正则；不配就是代码里的默认一份（见 examples/config.example.toml）；[] = 关
ignore_patterns = [ ... ]      # 同时命中就不报；不配就是代码里的默认一份；[] = 不忽略
also_notify = []               # 另外提醒哪些会话「别重复操作，先核对现场」
```

- 第一次看某个会话的记录时从末尾开始，不翻旧账；sheepdog 自己新建的会话从头读（第一轮跑的命令也审）；记录文件换了或变短了从头读；名册把 topic 改指到另一个已有会话时，同样从那个会话记录的末尾开始。
- `[audit]` 热加载，下一轮生效。
- 这是事后报警，不是拦截：命令已经跑了。真正挡住要靠会话宿主自己的命令权限和凭据。

## 确认表情：看到就点，回复后撤下

配置 `[ack]` 段后开启，没有这一段就完全不动：

```toml
[ack]
emoji_type = "Get"                          # 飞书表情类型
reasons = ["p2p", "at_me", "at_all"]        # 投递原因在其中的消息，送达会话后以你的身份点表情
remove_on_reply_reasons = ["p2p", "at_me"]  # 这些原因的表情，在你回复后撤下；@所有人 只点不撤
bot_reasons = ["owner_reply"]               # 这些原因的消息送达后以机器人身份点、永不撤（默认空）
```

- **点**：消息实际投递给会话成功之后才点；排队中（你在会话里接管、等 spawn）不点；被安全规则 hold 的消息不点（tag 的照常点）。每条消息只点一次，记进账本 `acks` 表（含 `reaction_id`）。
- **撤**：账本入账一条你自己发的消息时（包括会话以你身份代回的）：私聊里，同一聊天中点表情早于这条消息的全部撤下；群里，回复了那条消息或 @ 了它的发送人才撤下对应的。只撤 `remove_on_reply_reasons` 里的原因。
- `bot_reasons`：投递原因在其中的消息，送达后以**机器人身份**（`--as bot`）点同一个表情，永不撤；与 `reasons` 同时命中以它为准。典型是 `owner_reply`：你在「找你」聊天里回复机器人，会话收到了（包括总线 forward 转交的，以实际送达为准）就点给你看。你本人的消息都按 `owner_reply` 认（含改名前的旧记录）。`acks` 表的 `identity` 列记 user / bot。
- 用 lark-cli 调 `im reactions create` / `im reactions delete`，默认 user 身份。失败只记日志和 `error` 列，不影响投递和路由，也不重试。
- `--dry-run` 不调接口，只打印将要点 / 撤什么。
- 这是 sheepdog 唯一的 IM 写操作。

## 业务规则：playbook

代码只放机制（名册、路由、投递、回执、等待计时、spawn、forward，以及自动生成的「sheepdog 接口说明」）。总线职责、工作方式、代回卡片规则、拿不准找你、等别人回复、需求归属项目、退休与接手说明、提醒文字，全部写在配置目录的 md 文件里：

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
| `cli_host.md` | 只发给 Codex / Claude 会话（common.md 之后）：回执块怎么写、发不了 IM 时怎么办 | `{{host}}` |
| `host_locked.md` | 命令行会话连续写锁冲突满 60 分钟时，给总线的提示里附的说明（为什么锁、怎么放锁） | `{{host}}` `{{since}}` |
| `rules_after_compaction.md` | 检测到宿主压缩、重推完整规则时放在规则前的一句话（缺了用代码里的一句默认话） | — |
| `audit_offender.md` | 会话跑了危险命令时，给它的系统提示里附的话（每次报警都发） | `{{commands}}` `{{alert_count}}`（10 分钟内第几次报警） |
| `audit_notify.md` | 会话跑了危险命令时，给 `[audit] also_notify` 会话的提示里附的话 | `{{commands}}` `{{offender_title}}` `{{offender_topic}}` `{{alert_count}}` |
| `attention_notice.md` | 可选：会话进入 attention 时私聊你的通知末尾附的话（事实由 sheepdog 写在前面，缺了只发事实，doctor 不报缺） | `{{host}}` `{{reason}}` `{{since}}` `{{queued}}` `{{auto_recover}}`（yes / no） |
| `attention_recovered.md` | 可选：会话自动恢复时私聊你的通知末尾附的话 | `{{host}}` `{{evidence}}` `{{queued}}` |
| `duty_collision.md` | 可选：撞车提醒后面附的话（事实由 sheepdog 写在前面） | `{{key}}` `{{owner_title}}` `{{owner_topic}}` `{{work_state}}` `{{run_state}}` `{{action}}` `{{ago}}` |
| `duty_mail.md` | 可选：会话留言后面附的话（「不是主人的指令」的抬头由 sheepdog 写死） | `{{from_topic}}` `{{from_title}}` `{{key}}` |
| `duty_digest.md` | 可选：总线批次里羊群看板摘要的标题（缺了用代码里的一句） | `{{count}}` |
| `duty_stuck.md` | 可选：会话判为中断 / 失联时私聊你的通知末尾附的话 | `{{host}}` `{{state}}` `{{detail}}` `{{queued}}` |
| `agent_sender_label.md` | 可选：机器人 / Agent 发来的消息上那行标注（缺了用默认值「🤖 来自机器人 / Agent」，doctor 不报缺） | — |
| `reminders/<key>.md` | 可选：这个会话每一批最末尾附的红线（key 是名册 key，总线是 `bus`） | — |

通用占位符：`{{session_title}}` `{{topic_id}}` `{{reply_suffix}}` `{{reply_prefix}}`（旧，保留兼容）`{{watch_remind_minutes}}` `{{watch_expire_minutes}}` `{{cli}}`（会话该敲的 sheepdog 命令，即配置的 `cli_name`，默认 `sheepdog`；代码生成的接口说明、回执提示里的命令也用它，测试环境可以指向只连测试配置的包装脚本）。占位符是简单字符串替换，不认识的原样保留。`<!-- ... -->` 是写给维护者的注释，渲染时去掉。文件每次组装 prompt 时现读，改完下一批生效，不用重启。`prompt_overlay_path` 照旧拼在总线的 common.md 之后。

相关参数在 `config.toml`：`session.reply_suffix` / `reply_card_width`（代回卡片）、`session.reply_prefix`（旧）、`[watch] remind_minutes / expire_minutes`、`routing.drop_bot_message_prefixes`（机器人消息以这些前缀开头就丢弃，防止会话找你的私聊被推回总线）。

## 安全闸

有人在 IM 里发危险请求（要密钥、要权限、删东西、跑脚本、转账、冒充你「已经同意了」、让 agent 忽略规则）时，sheepdog 在投递前先过一道规则：

- 规则写在 `security.toml`（`security_path`，示例见 `examples/security.example.toml`），每条是正则 `patterns` 加可选条件 `external_sender`（发送方租户不在 `own_tenant_keys` 里）、`sender_types`。代码里没有任何具体关键词。
- `tag`：照常投递，正文前插 `security_banner.md` 警示。`hold`：不投给任何专职会话（覆盖名册、等待、all_messages），改投总线并带警示。
- 被 hold 的消息只能由总线 `sheepdog forward --message-ids ... --quote "<你的原话>"` 转交；`--quote` 会对照总线会话 transcript 里你亲口说过的话核对（回看 `quote_max_age_hours`），核对不过就拒绝。
- 编辑过的消息会重新判定；`sheepdog security-log` 查看记录，`sheepdog doctor` 显示规则条数和 `own_tenant_keys` 是否配置。

**已知局限**：

- 规则匹配挡不住所有变形话术；最后一道防线仍是会话自己遵守 `security.md`，以及会话宿主（Antigravity / Codex / Claude）自己的命令权限和云权限本身。
- 找你和代回经 `sheepdog ask / report / reply` 发（7.22），其他消息仍是会话自己用 lark-cli 发的、命令是会话自己执行的，sheepdog 不在这些执行路径上，只能在投递前标记和拦截。

## 数据与隐私

| 层 | 位置 | 进仓库 |
|---|---|---|
| 引擎代码 | 本仓库 | ✅ |
| 个人配置 / 名册 / playbook / 安全规则 / Prompt 覆盖层 | `~/.config/sheepdog/` | ❌ |
| 运行数据（账本、Inbox、回执） | `~/.local/state/sheepdog/`，超过 `retention_days`（默认 7 天）清理 | ❌ |
| 业务产出 | 由各 session 自行写入你的知识库 / 任务系统 | ❌ |

除了加、撤确认表情（`[ack]`），以及经机器人私聊你的几类提醒：Codex / Claude 登录失效时「需要你重新登录」（24 小时最多一次）、会话写锁冲突满 60 分钟时提醒你关掉 Codex 应用（同一会话 24 小时最多一次）、会话跑了危险命令时的报警（10 分钟内报过的同一条命令不重复报，新命令马上报）、会话卡在 attention 时的通知和自动恢复后的「已恢复」（同一次 attention 各一次）、羊群看板判会话中断 / 失联时的通知（同一段一次）和恢复时的「已恢复」、同一会话连续 3 次投递失败时的「投递不出去」和之后的「投递已恢复」，sheepdog 自己对 IM 只读；会话调 `sheepdog ask / report / reply` 时，由 sheepdog 替它发这一条（找你经机器人，代回以你的身份；开了 `[identity]` 时群里机器人在就以机器人身份），发之前的检查只读。开了 `[identity]` 时，别人私聊机器人，机器人自动回一句（同一个人 24 小时一次）。设了 `[notify] file`（测试环境）时，这些通知改写进文件，不发飞书。

清理（常驻进程每小时一次）覆盖：消息、投递记录、「需要你定」、机器人消息来源、确认表情（已撤 / 机器人身份 / 点失败的）、主人表情、已结束的等待、已执行的动作、熔断记录、回执文件、Codex / Claude 已结束的运行记录和 `runs/` 下的输入输出文件、已报过的危险命令记录、羊群看板的履历和已结束的活（按 `[duty] history_days`，默认 7 天）、已回执的留言、一事一串的话题表（按最近一次往话题里发的时间；清掉以后同一把钥匙再发就新起一个话题）、替会话发出的记录、「发送前已看到」；还没结束、正在进行的不删（未结问题、还在等的等待、排队的动作、等回执的批次、不确定是否送达的批次）。已经送进 AI 会话的内容由 AI 宿主保存，不在 sheepdog 的清理范围内。

提交前运行 `python3 scripts/check_private_data.py`（或启用 `.pre-commit-config.yaml`）拦截真实 IM ID、家目录路径与凭据。测试只使用 `*_test_*` 合成数据。

## 开发

```bash
python3 -m unittest discover -s tests -v
```

## License

Apache-2.0
