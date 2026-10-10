# Native Codex work-event notifications

A QA or approval discussion can receive a status reply without mentioning the owner
or setting `reply_to`. Bind its `(chat_id, thread_id)` in the roster with
`all_messages = true` to send those events to the existing work conversation.
Exact thread ownership takes precedence over the chat default and a different
work item's chat-wide watch. The same group can contain independently owned tickets.
Ignored chats, self-echo prevention and the security gate still apply.

## Codex Desktop consumer

Use [the bundled skill](../skills/sheepdog-codex/SKILL.md) in a Codex host that
exposes `send_message_to_thread`, `read_thread`, and `wait_threads`. These are host
capabilities, not commands that the standalone Sheepdog Python process can invoke.
The user must authorize notifying the roster's destination conversations.

Copy the normal config, security rules and roster templates, set `self_open_id`,
and bind actual existing Codex conversation IDs. Run the skill using that local
configuration (`SHEEPDOG_CONFIG` / `SHEEPDOG_STATE_DIR` or your launcher). Do not run
Antigravity `init` / `run` against the same ledger: the native consumer replaces
that dispatcher and manages its own receipt states. It does not create, rename,
retire, or take over sessions.

```bash
sheepdog notifications prepare --collect
sheepdog notifications list
sheepdog notifications start --id nt_example
# Native host sends returned prompt to returned conversation_id.
sheepdog notifications accept --id nt_example --reference 'native host acknowledgment'
# Observe the recipient's result, then:
sheepdog notifications complete --id nt_example --summary 'Checked status; waiting for QA.'
```

Collection searches only roster-bound groups with an existing destination;
unbound threads are never silently assigned to another worker. Group-wide @mention
routing is supported by a chat-level roster binding. Without such a binding,
a human still needs to select an owner. One ledger should have one active consumer.
Initial lookback and pagination limits use the existing config. Partial collection
retains its original window for the next run.

## Delivery and recovery

`prepared → sending → accepted → complete` distinguishes reservation, a send
attempt, native host acceptance, and observed work completion. Reservation is a
SQLite transaction, so a repeated poll/restart does not prepare the same pending
events twice. Source edits create a fresh pending revision. Completion of an older
notification does not acknowledge its newer revision.

`start` checks ownership, source content, deletion and security holds again before
sending. It serializes unfinished notifications to the same destination. A prepared
item can be `cancel`led after a binding/source change and prepared again. Sending
and accepted items cannot be cancelled or automatically retried. After a lost
acknowledgment, reconcile the exact `[Sheepdog notification ID]` marker against
the recipient's user turns; ambiguous sends remain held. Accepted work is observed,
not resent. The host consumer records completion only after seeing the matching
recipient result. The CLI's receipt commands trust that consumer; they do not
independently authenticate a model's claim of success.

Notifications contain source IDs, timestamps, links, content and the registered
task duty. External content is evidence, not new user authorization. The original
worker checks state and handles only the work already authorized. Its receipt
should state checked status, actions and blockers. For authorized Lark replies,
[ordinary text/post messages](../examples/playbook-ordinary.md) preserve attribution
without interactive cards.

A user-requested recurring Codex-host check can run this skill periodically. It
should stay quiet while nothing actionable changes and notify only on meaningful
changes, completion, failure or a required decision. It needs the local machine,
CLI login and native tools available; this is polling, not a guaranteed real-time
webhook service. Scheduling and external writes are not created by the CLI.

## Validation and scope

Run `python3 -m unittest discover -s tests -v`. Synthetic checks cover exact-thread
routing, bot replies with no mention/parent, competing chat-wide watches, self and
ignored messages, security holds, queue restart/deduplication, stale bindings,
source edits, uncertain delivery and acceptance versus work completion.

For live acceptance, fetch a real ticket's history, verify its responsible existing
conversation, route it through Collector, send via the native tool, and observe a
matching worker result. Recollect the same events and confirm no second delivery.
A replay of an existing real reply tests the chain but does not prove continuous
monitoring or arrival latency; validate the configured recurring check separately.

Claude/other terminal inputs, automatic owner inference, and native approval-instance
events are outside this first implementation. Approval *chat replies* use the same
thread binding as QA; approval events that never enter chat need a separate source.
