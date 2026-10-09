# sheepdog 🐕🐑

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
```

Agent 以你的身份对外发 IM 消息时，正文开头必须加代回前缀（`session.reply_prefix`，默认 `🐕 [Agent 代回] `）。这条规则写在总线 bootstrap、onboarding 和每批信号末尾；消息是各会话自己发的，sheepdog 只能靠 prompt 约束，做不到发送时强制。

`agentapi` 依赖 Antigravity App 注入的环境变量（`ANTIGRAVITY_AGENTAPI_EXE` / `ANTIGRAVITY_LS_ADDRESS` / `ANTIGRAVITY_CSRF_TOKEN`），因此 `run` 需要在 App 托管的 sidecar 或 App 内终端中运行。

## 数据与隐私

| 层 | 位置 | 进仓库 |
|---|---|---|
| 引擎代码 | 本仓库 | ✅ |
| 个人配置 / 名册 / Prompt 覆盖层 | `~/.config/sheepdog/` | ❌ |
| 运行数据（账本、Inbox、回执） | `~/.local/state/sheepdog/`，默认保留 7 天 | ❌ |
| 业务产出 | 由各 session 自行写入你的知识库 / 任务系统 | ❌ |

提交前运行 `python3 scripts/check_private_data.py`（或启用 `.pre-commit-config.yaml`）拦截真实 IM ID、家目录路径与凭据。测试只使用 `*_test_*` 合成数据。

## 开发

```bash
python3 -m unittest discover -s tests -v
```

## License

Apache-2.0
