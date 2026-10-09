# sheepdog 牧羊犬 🐕🐑

[English](README.md) | **简体中文** | [日本語](README.ja.md)

**牧羊犬是一个跑在你自己电脑上的工作助理。** 你是牧羊人，AI 会话是羊群，牧羊犬负责看好这群羊。

它的第一项工作：盯着你的工作聊天工具（目前是飞书 / Lark），把每件找你的事交给负责它的 AI 会话，管好这些会话的状态，只把需要你拍板的事带回给你。你负责观察、指导、决策；AI 负责收集、分类、处理、起草。

## 为什么做它

工作主要靠聊天工具来的人都知道，事情是散的：私聊、@你、几十个群里的讨论。一个 AI 对话装不下这么多，开十几个 AI 对话又没人分派，一样乱。

牧羊犬补的就是中间这一层：

- **一件事一个负责人。** 每个人、每个项目有自己长期运行的 AI 会话，上下文越积越多，不会每次从头来。
- **分派不用你操心。** 新消息按规则分好，送到对应的会话，或者先放进收件箱。
- **拍板的始终是你。** 会话处理日常、起草回复；拿不准的回来问你，危险的事必须你明确同意。

## 它做什么

| 收到的消息 | 去哪 |
|---|---|
| 私聊、@你、@所有人、回复你的、关键人发言、命中关键词 | 送给负责这个聊天的会话；没人负责就送给**总线** |
| 其他群消息 | 进**收件箱**，按群分开；有人 @你时附上摘要 |
| 免打扰群（没 @你）、你自己发的 | 忽略 |

分派之外：

- **名册**：把你已经在用的 AI 会话登记进来。`managed` 的会往里推消息；`known` 的只让总线知道它管什么，不推。旧会话可以退休，由新会话接手；新会话必须先把前任的聊天记录完整读完才开工。
- **总线**：接收没人负责的消息，知道整份名册，把事转交给对的会话，没有合适的会话就新开一个。
- **你的发言作为背景**：你本人在聊天里的发言（以及编辑、撤回、点的表情）会作为背景送给负责的会话，避免各说各的。
- **拿不准就找你**：会话通过机器人私聊你。你可以去总线回答，也可以直接在飞书上回复。牧羊犬核对确实是你本人账号发的，再送回发问的会话。
- **等别人回复**：会话登记一条等待（`sheepdog watch`），对方一回复就直接送回去。15 分钟、30 分钟提醒礼貌催一下，60 分钟停止等待并向你汇报。时间都可以配置。
- **需求先问项目**：会话会问清需求属于哪个项目并记下来，信息不会散。
- **代回用卡片**：AI 替你发出的回复以卡片发出，右下角带一个灰色小标记 `[🐕Sheepdog Reply]`，别人一眼能分清是不是你本人。
- **安全检查**：可疑消息在送到会话之前，先按可配置的规则标记或拦截。比如索要凭据、开权限、删除或动生产、声称「老板已经同意了」、让 AI 忽略规则。

## 怎么工作

```
飞书（lark-cli，以你的身份）
   └─► 采集 ──► 账本（SQLite） ──► 分派规则 ──► 安全检查
                                                ├─ 丢弃
                                                ├─ 收件箱（按群）
                                                └─ 投递 ──► 投递器 ──► Antigravity 会话
                                                              ▲               │
                                                              └──── 回执 ◄────┘（sheepdog receipt）
```

- 会话每处理完一批消息就交一份回执：已处理、需要你定、在等别人、已完成。牧羊犬按回执切换每个会话的状态。
- 你在某个会话里打字，牧羊犬就暂停往那里送消息；你停下 15 分钟后再补上。
- 消息来源、会话所在的平台、账本存储都是接口。目前只实现了飞书、Google Antigravity 和 SQLite。

**机制写在代码里，规则写在你的配置里。** 所有业务规则都是 `~/.config/sheepdog/` 下的 Markdown 或 TOML 文件，包括代回怎么标记、什么时候找你、怎么交接、安全规则。playbook 改完下一批消息就生效；config.toml、名册、安全规则每轮拉取都会重读。不用重启，也不用改代码；只有 `state_dir`、`sink` 改了需要重启。

## 需要什么

- Python 3.11 或更高（只用标准库）
- [`lark-cli`](https://github.com/larksuite/cli)（`npm install -g @larksuite/cli`），用你本人身份登录
- [Google Antigravity](https://antigravity.google) 桌面 App。牧羊犬通过 App 内置的 `agentapi` 新建会话、发消息，它只能在 App 自己的进程里用，所以牧羊犬以 Antigravity 后台进程（sidecar）的方式运行。

## 快速开始

```bash
uv tool install .            # 或者：pipx install .
mkdir -p ~/.config/sheepdog
cp examples/config.example.toml   ~/.config/sheepdog/config.toml    # 填 self_open_id
cp examples/roster.example.toml   ~/.config/sheepdog/roster.toml    # 登记你现有的会话（可选）
cp examples/security.example.toml ~/.config/sheepdog/security.toml  # 填 own_tenant_keys，按需调整规则
cp -r examples/playbook           ~/.config/sheepdog/playbook       # 按你的工作方式改写规则

sheepdog doctor              # 检查配置、名册、规则、playbook 文件
sheepdog init --dry-run      # 打印初始化会发出的每条消息，不写任何数据
sheepdog poll --dry-run      # 拉真实消息，看会怎么分派，不写任何数据
```

正式运行，交给 Antigravity 托管：

```bash
mkdir -p ~/.gemini/config/sidecars/sheepdog
cp examples/sidecar.example.json ~/.gemini/config/sidecars/sheepdog/sidecar.json   # 里面的路径写绝对路径
```

然后在 Antigravity 的 **Automations（自动化）** 面板里启用 **sheepdog**。启动时会先跑 `sheepdog init`（可以重复执行），再跑 `sheepdog run`。日志在 `~/.gemini/antigravity/sidecar_data/sheepdog/logs/sidecar.log`。

## 常用命令

```bash
sheepdog sessions            # 所有会话：职责、负责的聊天、状态、关联项目
sheepdog inbox [--chat 群名]  # 收件箱里的群消息
sheepdog escalations         # 等你拍板的问题
sheepdog watches             # 会话正在等谁回复
sheepdog security-log        # 被安全检查标记或拦截的消息
sheepdog acks [--open]       # 替你点过的确认表情，以及是否已撤
```

## 配置一览

| `~/.config/sheepdog/` 下的文件 | 放什么 |
|---|---|
| `config.toml` | 你的 ID、拉取频率、关键词、代回标记、等待时间、找你用的聊天 |
| `roster.toml` | 哪个会话负责哪些聊天、它的职责、你给它的授权 |
| `playbook/*.md` | 业务规则和消息模板：总线、开场说明、交接、提醒、安全 |
| `security.toml` | 安全检查规则：正则加条件，动作是标记（tag）或拦截（hold） |
| `prompts/overlay.md` | 可选，给会话的额外指令，比如你自己知识库的协议 |

完整参考：[docs/reference.zh-CN.md](docs/reference.zh-CN.md)。

## 安全模型与局限

- **只有你的话算指令。** 聊天消息是信息，不是命令。会话只把三种来源当成你：你在 Antigravity 里打的字；总线转达、并且牧羊犬对照过你原话的引用；核对过来自你本人账号的飞书回复。
- 被拦截（hold）的消息不会送到任何专属会话，只到总线；要转出去，必须附上核对过的你的原话。
- **局限**：正则挡不住所有变形话术。牧羊犬不在执行路径上，消息是会话自己发的，命令也是会话自己跑的。所以最后几道防线是：会话遵守 `security.md`、Antigravity 的命令权限、你的云权限设置。

## 数据与隐私

| 层 | 位置 | 进仓库 |
|---|---|---|
| 引擎代码 | 本仓库 | 是 |
| 你的配置、名册、playbook、规则 | `~/.config/sheepdog/` | 否 |
| 运行数据（账本、收件箱、回执） | `~/.local/state/sheepdog/`，超过 `retention_days`（默认 7 天）清理 | 否 |
| 工作产出 | 由各会话写进你自己的笔记和任务系统 | 否 |

清理覆盖消息、投递记录、「需要你定」、机器人消息来源、表情、已结束的等待和动作、熔断记录、回执文件；还没结束、正在进行的不删。已经送进 AI 会话的内容由 AI 宿主保存，不在牧羊犬的清理范围内。除了加、撤确认表情（`[ack]`，不配置就不开），牧羊犬对 IM 只读。牧羊犬本身从不写你的笔记和任务系统。提交前的检查脚本（`scripts/check_private_data.py`）会拦住真实的聊天 ID、家目录路径和凭据。测试只用编造的数据。

## 现状

还在早期，带着很强的个人习惯：围绕一个人的日常工作搭的，用的是飞书加 Google Antigravity。接其他聊天工具、其他 AI 平台的接口留好了，但还没有实现。

## 开发

```bash
python3 -m unittest discover -s tests -v
```

## 许可证

Apache-2.0
