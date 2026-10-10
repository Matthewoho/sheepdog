"""A QA bot reply may have only thread_id: route it without borrowing another task."""

import unittest

from sheepdog.engine import BUS_TOPIC_ID, apply_ownership
from sheepdog.models import Mention
from sheepdog.roster import RosterError, parse_roster
from sheepdog.router import DISPATCH, INBOX, RouteContext, route
from tests.test_roster import Base, ME, msg


def roster_data():
    return {"session": [
        {"key": "qa", "mode": "managed", "conversation_id": "conv_test_qa",
         "chats": [{"chat_id": "oc_test_group", "thread_id": "omt_test_qa", "all_messages": True}]},
        {"key": "review", "mode": "managed", "conversation_id": "conv_test_review",
         "chats": [{"chat_id": "oc_test_group", "thread_id": "omt_test_review", "all_messages": True}]},
    ]}


class ThreadOwnershipTest(Base):
    def setUp(self):
        super().setUp()
        self.roster = parse_roster(roster_data())

    def test_bot_reply_without_mention_or_parent_reaches_exact_owner(self):
        event = msg(chat_id="oc_test_group", thread_id="omt_test_qa", sender_type="app", content="PASS")
        self.poll([event], muted={"oc_test_group"})
        self.assertEqual((self.row(event.message_id)["route"], self.row(event.message_id)["topic_id"]),
                         (DISPATCH, "tp_qa"))
        dispatcher = self.disp()
        dispatcher.dispatch_once()
        self.ack("tp_qa")
        self.ack("tp_review")
        dispatcher.dispatch_once()
        self.assertTrue(any("PASS" in text for text in self.sink.to("conv_test_qa")))
        self.assertFalse(any("PASS" in text for text in self.sink.to("conv_test_review")))
        self.poll([event])
        dispatcher.dispatch_once()
        self.assertEqual(sum("PASS" in text for text in self.sink.to("conv_test_qa")), 1)

    def test_unbound_thread_does_not_inherit_another_threads_owner(self):
        self.poll([msg(chat_id="oc_test_group", thread_id="omt_test_other", mentions=[Mention(ME)])])
        self.assertEqual(self.row("om_test_1")["topic_id"], BUS_TOPIC_ID)

    def test_chat_wide_wait_cannot_steal_a_bound_thread(self):
        watch = self.store.add_watch("tp_review", "ou_test_alice", "oc_test_group", "waiting for another task")
        self.poll([msg(chat_id="oc_test_group", thread_id="omt_test_qa")])
        self.assertEqual(self.row("om_test_1")["topic_id"], "tp_qa")
        self.assertIsNone(self.store.get_watch(watch)["last_reply_message_id"])

    def test_same_thread_id_in_another_chat_does_not_match(self):
        self.poll([msg(chat_id="oc_test_other", thread_id="omt_test_qa", sender_type="app")])
        self.assertEqual((self.row("om_test_1")["route"], self.row("om_test_1")["topic_id"]), (INBOX, None))

    def test_self_ignored_and_loop_prevention_win(self):
        for event in [msg(chat_id="oc_test_group", thread_id="omt_test_qa", sender_id=ME),
                      msg(chat_id="oc_test_group", thread_id="omt_test_qa", sender_type="app", content="FX-DROP·echo")]:
            decision, topic = apply_ownership(event, route(event, RouteContext(ME), self.cfg.routing), self.roster, [])
            self.assertIsNone(topic)
        event = msg(chat_id="oc_test_group", thread_id="omt_test_qa")
        _, topic = apply_ownership(event, route(event, RouteContext(ME), self.cfg.routing), self.roster, [event.chat_id])
        self.assertIsNone(topic)

    def test_exact_thread_overrides_chat_default_independent_of_order(self):
        data = roster_data()
        data["session"].insert(0, {"key": "default", "mode": "managed", "conversation_id": "conv_test_default",
                                    "chats": [{"chat_id": "oc_test_group", "all_messages": True}]})
        for sessions in [data["session"], list(reversed(data["session"]))]:
            roster = parse_roster({"session": sessions})
            self.assertEqual(roster.owner_of("oc_test_group", "omt_test_qa")[0].key, "qa")
            self.assertEqual(roster.owner_of("oc_test_group", "omt_test_unknown")[0].key, "default")

    def test_duplicate_thread_binding_rejected(self):
        data = roster_data()
        data["session"][1]["chats"][0]["thread_id"] = "omt_test_qa"
        with self.assertRaises(RosterError):
            parse_roster(data)

    def test_owner_context_follows_thread(self):
        self.cfg.owner_context.enabled = True
        self.poll([msg(chat_id="oc_test_group", thread_id="omt_test_review", sender_id=ME)])
        self.assertEqual(self.row("om_test_1")["topic_id"], "tp_review")

    def test_empty_thread_binding_rejected(self):
        data = roster_data()
        data["session"][0]["chats"][0]["thread_id"] = " "
        with self.assertRaises(RosterError):
            parse_roster(data)


if __name__ == "__main__":
    unittest.main()
