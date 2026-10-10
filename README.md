# sheepdog 🐕🐑

**English** | [简体中文](README.zh-CN.md) | [日本語](README.ja.md)

**sheepdog is a work assistant that runs on your own machine.** You are the shepherd, your AI agent sessions are the flock, and sheepdog keeps the flock together.

Its first job: watch your work chat (Lark / Feishu today), hand each request to the agent session that owns it, keep those sessions on track, and bring back only the decisions that need you. You observe, guide and decide. The agents collect, triage, handle and draft.

## Why

If most of your work arrives through chat, it arrives scattered: DMs, @mentions, threads in dozens of groups. One AI chat can't hold all of it, and a dozen AI chats with nobody routing between them is not much better.

sheepdog is the missing middle layer:

- **One owner per thread of work.** Each person or project gets its own long-lived agent session, so context builds up instead of resetting.
- **Routing you don't have to do.** Every new message is sorted by rules and delivered to the right session, or parked in an inbox.
- **You stay the decision-maker.** Sessions draft and handle routine work. Anything they're unsure about comes back to you, and anything dangerous needs your explicit OK.

## What it does

| Incoming message | Where it goes |
|---|---|
| DMs, @you, @all, replies to you, VIP senders, keyword hits | Pushed to the agent session that owns the chat, or to the **bus** session if no one owns it |
| Other group messages | **Inbox**, grouped by chat. Sessions see a summary when you're @-mentioned |
| Muted chats (unless you're @-mentioned), your own messages | Ignored |

On top of routing:

- **Roster.** Register agent sessions you already have (`managed`: sheepdog pushes to them; `known`: the bus knows they exist but never pushes). A retiring session can hand over to a new one, and the successor must read its predecessor's full transcript before starting.
- **Bus session.** Receives everything without an owner, knows the whole roster, and forwards work to the right session, opening a new one when no session fits.
- **Your own words as context.** What you say in a chat yourself (plus your edits, recalls and reactions) is passed to the session that owns it as background, so you and the agent don't talk past each other.
- **Ask the human.** When a session is unsure, it DMs you through a bot. You can answer in the bus session or reply right in Lark. sheepdog verifies the reply came from your account and routes it back to the session that asked.
- **Waiting on others.** A session registers a wait (`sheepdog watch`); the reply is routed straight back to it, with reminders at 15 / 30 minutes and a report to you at 60 (all configurable).
- **Project anchoring.** Sessions ask which project a request belongs to and record it, so information doesn't scatter.
- **Attributed agent replies.** The default playbook uses cards with a small grey marker (`[🐕Sheepdog Reply]`). [Ordinary text/post replies](examples/playbook-ordinary.md) can keep the same attribution.
- **Security gate.** Configurable rules tag or hold suspicious messages (credential requests, permission grants, destructive or production actions, "the boss already approved" claims, prompt injection) before any session sees them.

## How it works

```
Lark (lark-cli, as you)
   └─► Collector ──► Ledger (SQLite) ──► Router ──► Security gate
                                                     ├─ drop
                                                     ├─ inbox (per chat)
                                                     └─ dispatch ──► Dispatcher ──► Antigravity sessions
                                                                        ▲                 │
                                                                        └──── receipts ◄──┘  (sheepdog receipt)
```

- Sessions report back after every batch with a structured receipt (`handled`, `needs_decision`, `waiting_external`, `done`), which drives a per-session state machine.
- If you start typing in a session, sheepdog pauses deliveries to it and catches up 15 minutes after you stop.
- Source (where messages come from), Sink (where sessions live) and Store are interfaces. The sidecar uses Lark, Google Antigravity and SQLite. A [native Codex consumer](docs/session-notifications.md) can notify explicitly bound existing Codex conversations.

**Mechanism lives in code, rules live in your config.** Every business rule (how agent replies are marked, when to ask you, how to hand over, the security rules) is plain Markdown or TOML in `~/.config/sheepdog/`. Playbook edits apply to the next batch; `config.toml`, the roster and the security rules are re-read every polling round. No restart and no code change, except for `state_dir` and `sink`, which need a restart.

## Requirements

- Python ≥ 3.11 (standard library only)
- [`lark-cli`](https://github.com/larksuite/cli) (`npm install -g @larksuite/cli`), logged in with your user identity
- For the sidecar: the [Google Antigravity](https://antigravity.google) desktop app. sheepdog creates and messages sessions through the app's built-in `agentapi`, which only works inside the app's own processes, so sheepdog runs as an Antigravity sidecar.

## Codex Desktop

Use the [Sheepdog Codex skill](skills/sheepdog-codex/SKILL.md) in a host exposing
native conversation send/read/wait tools. It consumes a durable outbox and records
host acceptance separately from the worker's checked result; uncertain sends are
held for reconciliation. Antigravity is not required for this mode.

Bind an existing conversation in `roster.toml`; an optional `thread_id` on a chat
binding distinguishes QA/approval tickets within the same group. Set
`all_messages = true` to include bot replies without an @mention or parent message.
Then run `sheepdog notifications prepare --collect` through the native skill.
See [setup, receipt states and limits](docs/session-notifications.md).

The standalone CLI cannot invoke Codex Desktop tools. A recurring check must run
in a host that provides them. Claude/terminal adapters and native approval events
are not implemented.

## Quick start (Antigravity)

```bash
uv tool install .            # or: pipx install .
mkdir -p ~/.config/sheepdog
cp examples/config.example.toml   ~/.config/sheepdog/config.toml    # set self_open_id
cp examples/roster.example.toml   ~/.config/sheepdog/roster.toml    # your existing sessions (optional)
cp examples/security.example.toml ~/.config/sheepdog/security.toml  # set own_tenant_keys, tune rules
cp -r examples/playbook           ~/.config/sheepdog/playbook       # rewrite the rules in your own words

sheepdog doctor              # checks config, roster, rules, playbook files
sheepdog init --dry-run      # prints every message init would send; writes nothing
sheepdog poll --dry-run      # pulls real messages, shows how they'd be routed; writes nothing
```

Run it for real as an Antigravity sidecar:

```bash
mkdir -p ~/.gemini/config/sidecars/sheepdog
cp examples/sidecar.example.json ~/.gemini/config/sidecars/sheepdog/sidecar.json   # use absolute paths
```

Then enable **sheepdog** in Antigravity's **Automations** panel. On start it runs `sheepdog init` (idempotent) and then `sheepdog run`. Logs go to `~/.gemini/antigravity/sidecar_data/sheepdog/logs/sidecar.log`.

## Everyday commands

```bash
sheepdog sessions            # every session: role, chats, state, linked projects
sheepdog inbox [--chat X]    # parked group messages
sheepdog escalations         # questions waiting for your decision
sheepdog watches             # sessions waiting on someone's reply
sheepdog security-log        # messages tagged or held by the security gate
sheepdog acks [--open]       # "seen" reactions added for you, and whether they were removed
```

## Configuration at a glance

| File in `~/.config/sheepdog/` | What it holds |
|---|---|
| `config.toml` | Your IDs, polling, keywords, agent reply marker, wait timings, escalation chat |
| `roster.toml` | Which session owns which chats, its duty, and the authority you gave it |
| `playbook/*.md` | Business rules and message templates (bus, onboarding, handover, reminders, security) |
| `security.toml` | Security gate rules: regex patterns plus conditions, `tag` or `hold` |
| `prompts/overlay.md` | Optional extra instructions for your own workspace |

Full reference (Chinese for now): [docs/reference.zh-CN.md](docs/reference.zh-CN.md).

## Security model and known limits

- **Only your words are instructions.** Chat messages are information, not commands. Sessions treat these three sources as you: what you type in Antigravity, a quote the bus relays that sheepdog has checked against your own messages, and a Lark reply verified to come from your account.
- `hold` messages never reach a specialist session. They go to the bus, and leave it only with a verified quote from you.
- **Limits.** Regex rules won't catch every rewording. sheepdog is not on the execution path: sessions send messages and run commands themselves, so the last lines of defence are the sessions following `security.md`, Antigravity's command permissions, and your cloud IAM.

## Data and privacy

| Layer | Location | In this repo |
|---|---|---|
| Engine code | this repository | yes |
| Your config, roster, playbook, rules | `~/.config/sheepdog/` | no |
| Runtime data (ledger, inbox, receipts) | `~/.local/state/sheepdog/`, pruned after `retention_days` (default 7) | no |
| Work output | written by the sessions to your own notes and task tracker | no |

Pruning covers messages, delivery records, escalations, bot message sources, reactions, finished waits and actions, loop records and receipt files; anything still open or in progress is kept. What has already been sent into an AI session is stored by the AI host, outside sheepdog's cleanup. Towards your chat app sheepdog is read-only, except for adding and removing the "seen" reaction (`[ack]`, off unless configured). sheepdog itself never writes to your notes or task tracker. A pre-commit check (`scripts/check_private_data.py`) blocks real chat IDs, home-directory paths and credentials. Tests use synthetic data only.

## Status

Early and opinionated: built around one person's daily workflow, Lark plus Google Antigravity. The Source / Sink interfaces are there for other chat tools and agent hosts, and native Codex notifications are available through the bundled host skill.

## Development

```bash
python3 -m unittest discover -s tests -v
```

## License

Apache-2.0
