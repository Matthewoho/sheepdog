# Ordinary Lark replies

The default playbook uses interactive cards. To use ordinary messages, replace its
card rules in your **personal** `common.md`, `batch_footer.md`, and `watch_nudge.md`
with the following rule. Replace other references to card replies in those files
as well, so the worker receives one consistent instruction.

```markdown
When authorized to send on the owner's behalf, use an ordinary text or rich-text
post, and append `{{reply_suffix}}` on its own line. Use `--markdown` for lists,
links and emphasis (`post`); use `--text` for literal text. For a thread reply,
use `+messages-reply --message-id <source_message_id> --reply-in-thread`.
Do not use `--msg-type interactive` or `sheepdog reply-card`.
Keep existing approval, routing, waiting and security rules.
```

Preview the request before sending:

```bash
lark-cli im +messages-send --as user --chat-id oc_example \
  --markdown 'Status checked. Waiting for the updated QA conclusion.

[🐕Sheepdog Reply]' --dry-run
```

Keep the same `session.reply_suffix` in `owner_context.agent_markers`; Sheepdog
uses it to distinguish agent replies from the owner's own words and prevent loops.
Changing the presentation does not grant permission to send messages.
