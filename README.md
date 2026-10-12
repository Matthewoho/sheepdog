# sheepdog 🐕🐑

**English** | [简体中文](README.zh-CN.md) | [日本語](README.ja.md)

sheepdog is a work assistant that runs on the owner's own machine. Every minute it pulls new messages from Lark (Feishu), hands each one to the AI session responsible for it, and tracks the state of those sessions. When something needs the owner's decision, the session asks the owner in Lark through a bot.

The name: the owner is the shepherd, the AI sessions are the flock, and sheepdog keeps the flock together.

## Flock board

`sheepdog board` shows what each session is doing right now: its run state, the work on its hands, and its last action. When something changes, the bus session gets a digest.

- Run states: working, waiting, stalled, lost, owner-busy, idle. sheepdog recomputes them every round from the delivery ledger, the session transcript and the owner's own typing. "Waiting" also reads the session's latest receipt.
- Work on hand comes mostly from facts sheepdog records itself: registered waits, unanswered questions to the owner, hand-overs, and which session a message with an approval number, PR or Linear issue was last delivered to. Sessions can add entries with `sheepdog work set`; these are shown but never change delivery or state, and are marked possibly stale after 4 hours without an update while the session is idle. A differential test runs the same random scenarios twice, once with wrong registrations and once with none, and checks that deliveries, notices and states are identical.
- Stalled and lost: a session with work in flight and no new tool call for 15 minutes is stalled; no transcript change for 30 minutes is lost. New messages for it are held and the owner gets a DM. When the session moves again, held messages are delivered once and the owner is told it has recovered.
- Owner-busy: for 15 minutes after the owner types in a session, sheepdog doesn't deliver to it and doesn't call it stalled.
- Collisions: when a message carries a number that recently went to another session, sheepdog attaches a note saying which session has it and what that session is doing. It only informs; delivery doesn't change.
- Notes between sessions: `sheepdog tell`. The header is set by code and says the note is not the owner's instruction. A pair of sessions that sends more than 6 notes in 10 minutes is paused.

## Workflow

1. Collect: once a minute, pull new messages with lark-cli as the owner and record them in a local ledger (SQLite).
2. Route: use the roster and rules to pick a session, or park the message in the inbox.
3. Security check: tag or hold messages that match a security rule.
4. Deliver: send the batch to the session and wait for a receipt.
5. Ask: when a session is unsure, it DMs the owner in Lark through a bot.
6. Answer: the owner quote-replies in Lark, and the answer goes back to the session that asked.

```
Lark (lark-cli, as the owner)
   └─► Collector ──► Ledger (SQLite) ──► Router ──► Security check
                                                     ├─ drop
                                                     ├─ inbox (per chat)
                                                     └─ deliver ──► sessions (Antigravity / Codex / Claude)
                                                           ▲                  │
                                                           └──── receipts ◄───┘
```

## Routing

| Incoming message | Where it goes |
|---|---|
| DMs, @owner, @all, replies to the owner, VIP senders, keyword hits | The session that owns the chat, or the bus if no one owns it |
| Other group messages | Inbox, grouped by chat. A batch with @owner or @all carries an inbox summary |
| Muted chats (unless the owner is @-mentioned), the owner's own messages | Not pushed |

- The roster (`roster.toml`) says which session owns which chats. `managed` sessions get pushes. `known` sessions are only listed for the bus and never get pushes.
- The bus is a special session. It receives everything without an owner and knows the whole roster. It does light work only: it routes and forwards (`sheepdog forward`) to a dedicated session, and opens a new one when none fits (`sheepdog new-session`, at most 5 a day by default).
- Muted chats are not pushed, but replies to the owner still get through.
- When the owner quote-replies to a "needs your decision" message, the answer goes to the session that sent it. If the source can't be identified, it goes to the bus with the original text.
- Messages the owner has already read are still delivered, marked "✓ read". If a delivered message is edited or recalled, the session is told.

## Sessions

- Every batch has an ID. The session answers with a receipt (`handled`, `needs_decision`, `waiting_external`, `done`), and the receipt sets the session state: idle, waiting on the owner, waiting on someone else, or closed.
- When a session is unsure, it asks the owner with `sheepdog ask` (reports go through `sheepdog report`), one question per message. sheepdog adds the first line with the session's topic ID (`[tp_xxx]`), which is how the owner's reply finds its way back. Messages about the same thing carry the same `--key`: the first one starts a message in the main chat, later ones go into its thread, and the owner's replies in that thread go back to the session by thread.
- Waiting on someone is registered with `sheepdog watch`. By default the session is reminded at 15 and 30 minutes, and reports to the owner at 60. A reply only counts once the session confirms it is the answer it was waiting for.
- Handover: a retiring session writes a handover note. Its successor reads the full predecessor transcript first and lists what it read in its receipt.
- When the rules change, running sessions get the full new rules with their next batch.

### Session hosts

Each session runs on one host. The roster and `new-session` can set host, model and reasoning effort per session. A session never changes host; to switch, retire it and open a new one.

- **Antigravity** (default): sessions are created and messaged through the Antigravity app's built-in `agentapi`.
- **Codex**: sheepdog starts its own `codex app-server`, one background process per turn. Default model and effort are set in `[hosts.codex]` and can be overridden per session.
  - Permissions come from a Codex profile. When `profile` is set, sheepdog reads that profile and expands it into `-c` flags for the app-server it starts. The owner's own sessions in the Codex app are not touched. sheepdog adds no other sandbox or approval flags, and declines every approval request Codex sends.
  - A profile only applies to sessions opened after the change. To move an existing session onto it, retire the session and open a new one.
  - While the owner has a thread open in the Codex app, the app holds its write lock and batches for that session wait in the queue. After 60 minutes the bot asks the owner to close the app.
- **Claude**: runs through `claude -p`. The code is in place but has not been tested in a real environment.
- Codex and Claude are off by default. Opening a session on either needs the owner's own words: `new-session --host codex|claude` must carry a `--quote` that sheepdog verifies came from the owner (`ask_owner_before_new`, on by default).
- Codex and Claude sessions usually can't write to sheepdog's state directory, so they end their answer with a `sheepdog-receipt` block instead of running the receipt command.

## Security

- Only three things count as the owner's instructions:
  - what the owner types in the session itself (Antigravity, Codex or Claude);
  - a quote relayed by the bus that sheepdog has checked against the owner's own messages in the ledger;
  - a Lark reply verified to come from the owner's account.
- Anything else said in Lark is information, not a command.
- Security rules live in `security.toml`. Each rule is a regex plus optional conditions (external sender, sender type). The code has no built-in keywords.
  - `tag`: delivered as usual, with a warning above the text. The example rules cover secrets, permissions, deletion, running code and impersonation.
  - `hold`: delivered to the bus only. The bus can pass it on only with a verified quote from the owner. The example rules cover external requests for secrets, payments and prompt injection.
- Internal and external senders are told apart by tenant (`own_tenant_keys`).

## Guardrails

When a host compacts a session's context, the rules from the session's opening prompt can be lost. sheepdog reads each session's transcript incrementally every round and adds three guardrails:

- **Rules after compaction**: on a new compaction point, the next batch carries the full current rules, with one line in front saying why (`rules_after_compaction.md`). If an Antigravity session is still working on an unfinished batch, the full rules are sent right away as a separate message instead of waiting for the next batch (it is not a batch; receipts are unchanged). Compaction markers are recognised for Antigravity and Codex; the Claude marker is unverified.
- **Per-batch reminders**: if the playbook has `reminders/<key>.md` for a session, that file is appended to the end of every batch for it.
- **Command audit** (`[audit]`): new commands run by audited sessions are matched against `danger_patterns`. On a hit:
  1. the bot DMs the owner with the session, time and command (sensitive values masked);
  2. the session that ran it gets a notice (`audit_offender.md`);
  3. sessions listed in `also_notify` are told to check the current state before acting again (`audit_notify.md`).

  Commands that also match `ignore_patterns` are skipped, for example help lookups, `--dry-run`, or a command name mentioned in a chat message. A command already reported within the last 10 minutes is not reported again; a new command is reported at once, and the session gets another stop prompt each time. If the direct message to the owner fails (for example a Lark rate limit), it is retried with backoff; after 2 hours unsent, `sheepdog doctor` and `sheepdog board` show a warning at the top, and `sheepdog notices` lists it. This alerts after the fact; the command has already run.

## Speaking for the owner

- Every message a session sends as the owner goes out as a card with a small grey `[🐕Sheepdog Reply]` in the bottom-right corner, so people can tell it came from an agent. Replies go out through `sheepdog reply`, which first checks (read-only) whether the owner has already spoken in that chat; if so it does not send and hands the owner's words back to the session.
- With `[ack]` configured: once a DM or an @owner message reaches a session, sheepdog adds a "Get" reaction as the owner and removes it after the reply. @all messages get the reaction but it is never removed.
- Whether and how to reply is up to the playbook. Messages from bots or agents get a label line, whose text comes from `agent_sender_label.md`. The example says to reply politely once and not to keep a back-and-forth going. Without that file the label is just "🤖 来自机器人 / Agent" ("from a bot / agent").
- Loop breaker: if a session replies in one chat more than 4 times in 10 minutes and the other side is a bot, the chat is paused for 30 minutes. During the pause its messages go to the inbox and the bus is told. `sheepdog loops --clear` lifts it by hand.

## Following the owner's own actions

- With `[owner_context]` on, what the owner says in Lark, the reactions they add, and their edits and recalls are passed to the owning session as background. The session uses this so it doesn't repeat a reply the owner already gave, and defers to the owner on any conflict.
- Agent replies carry the card marker, so a session's own messages are never mistaken for the owner's.
- When the owner types in a session, sheepdog stops delivering to it and catches up 15 minutes after they stop. The same applies while the owner has a Codex thread open in the app.

## Reliability

- If a round doesn't finish pulling, the cursor doesn't move and the next round pulls again; messages are de-duplicated by ID. Network errors are retried with backoff.
- The ledger is written before delivery. If the process dies mid-delivery, sheepdog checks the session transcript after restart: batches already delivered are not resent; batches it can't confirm are marked uncertain and resent with `sheepdog redeliver` after someone checks.
- Messages longer than 200 lines are cut, with a note saying how many lines are left and how to read them with `sheepdog show`.
- `config.toml`, the roster and the security rules are re-read every round, and playbook files every time a prompt is built. No restart is needed, except for `state_dir` and `sink`.
- The ledger keeps 7 days by default; anything still open is kept.

## Setup and use

### Requirements

- Python ≥ 3.11 (standard library only)
- [`lark-cli`](https://github.com/larksuite/cli) (`npm install -g @larksuite/cli`), logged in as the owner
- The [Google Antigravity](https://antigravity.google) desktop app. sheepdog creates and messages sessions through the app's built-in `agentapi`, which only works inside the app's own processes, so sheepdog runs as an Antigravity sidecar.
- Optional: the `codex` and/or `claude` CLI, installed and logged in, if you enable Codex or Claude sessions (`[hosts.codex]`, `[hosts.claude]`).

### Install and dry run

```bash
uv tool install .            # or: pipx install .
mkdir -p ~/.config/sheepdog
cp examples/config.example.toml   ~/.config/sheepdog/config.toml    # set self_open_id
cp examples/roster.example.toml   ~/.config/sheepdog/roster.toml    # existing sessions (optional)
cp examples/security.example.toml ~/.config/sheepdog/security.toml  # set own_tenant_keys, tune rules
cp -r examples/playbook           ~/.config/sheepdog/playbook       # rewrite the rules in your own words

sheepdog doctor              # checks config, roster, rules, playbook files and hosts
sheepdog init --dry-run      # prints every message init would send; writes nothing
sheepdog poll --dry-run      # pulls real messages, shows how they'd be routed; writes nothing
```

### Run

As an Antigravity sidecar:

```bash
mkdir -p ~/.gemini/config/sidecars/sheepdog
cp examples/sidecar.example.json ~/.gemini/config/sidecars/sheepdog/sidecar.json   # use absolute paths
```

Then enable **sheepdog** in Antigravity's **Automations** panel. On start it runs `sheepdog init` (idempotent) and then `sheepdog run`. Logs go to `~/.gemini/antigravity/sidecar_data/sheepdog/logs/sidecar.log`.

### Everyday commands

```bash
sheepdog sessions            # every session: host, duty, chats, state, linked projects
sheepdog board [--topic tp_x] # flock board: what each session is doing, work on hand, last action
sheepdog inbox [--chat X]    # parked group messages
sheepdog escalations         # questions waiting for the owner
sheepdog watches             # sessions waiting on someone's reply
sheepdog security-log        # messages tagged or held by the security check
sheepdog acks [--open]       # "seen" reactions added for the owner, and whether they were removed
sheepdog show --topic tp_x   # the last few turns of a session
sheepdog push-rules          # send the full current rules to sessions now
```

### Configuration files

| File in `~/.config/sheepdog/` | What it holds |
|---|---|
| `config.toml` | Owner IDs, polling, keywords, reply marker, wait timings, the owner's escalation chat, session hosts, command audit |
| `roster.toml` | Which session owns which chats, its duty, the authority it has, its host and model |
| `playbook/*.md` | Business rules and message templates: bus, onboarding, handover, reminders, security, bot-message label, per-batch reminders |
| `security.toml` | Security rules: regex plus conditions, `tag` or `hold` |
| `prompts/overlay.md` | Optional extra instructions for the bus, such as the owner's notes conventions |

Code holds the mechanism; business rules live in these files. Full reference (Chinese for now): [docs/reference.zh-CN.md](docs/reference.zh-CN.md).

### Where data lives

| Layer | Location | In this repo |
|---|---|---|
| Engine code | this repository | yes |
| Config, roster, playbook, rules | `~/.config/sheepdog/` | no |
| Runtime data (ledger, inbox, receipts) | `~/.local/state/sheepdog/`, pruned after `retention_days` (default 7) | no |
| Work output | written by the sessions to the owner's own notes and task tracker | no |

- What has already been sent into an AI session is stored by the AI host, outside sheepdog's cleanup.
- sheepdog only reads from Lark, with two kinds of exception: adding and removing the "seen" reaction (`[ack]`, off unless configured), and bot DMs to the owner: Codex or Claude CLI logged out, the Codex app holding a write lock for 60 minutes, command-audit alerts, and a session stuck in attention (plus a short note when it recovers on its own).
- sheepdog itself never writes to the owner's notes or task tracker.
- A pre-commit check (`scripts/check_private_data.py`) blocks real chat IDs, home-directory paths and credentials. Tests use synthetic data only.

### Development

```bash
python3 -m unittest discover -s tests -v
```

## Limits

- Apart from asking the owner and replying on the owner's behalf, which go through sheepdog, sessions send messages and run commands themselves; sheepdog is not on those execution paths. The reply format, asking the owner and the security rules are all enforced by prompts, not by code. The last lines of defence are the session host's own command permissions and the owner's cloud permissions.
- Security checks are regex rules and can miss a rewording. The command audit alerts after the command has run.
- When the Antigravity app is closed, sheepdog stops too. It picks up where it left off when the app is back.
- Edits and recalls are only noticed within a few minutes of the original message.
- Lark is the only chat source implemented. The Claude host has not been tested in a real environment.
- It was built around one person's daily work. The example rules reflect that and should be rewritten for your own.

## License

Apache-2.0
