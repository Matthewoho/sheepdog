"""Durable native-host delivery; acceptance is not completion."""
import tempfile
import unittest
from pathlib import Path

from sheepdog.notifications import NotificationQueue, NotificationError
from sheepdog.roster import parse_roster
from sheepdog.store import Store
from tests.test_roster import msg, roster_data


class NotificationsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'state.db'
        self.store = Store(self.path)
        self.roster = parse_roster(roster_data())
        self.queue = NotificationQueue(self.store, self.roster)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def seed(self, **kw):
        event = msg(chat_id='oc_test_alpha_p2p', **kw)
        self.store.upsert_message(event, 'dispatch', 'owned', [], 'tp_alpha')
        return event

    def test_restart_and_duplicate_collection_do_not_repeat_delivery(self):
        self.seed()
        first = self.queue.prepare()
        self.assertEqual(len(first), 1)
        other = Store(self.path)
        try:
            q = NotificationQueue(other, self.roster)
            self.assertEqual(q.prepare(), [])
            self.assertEqual(q.items()[0]['id'], first[0]['id'])
        finally:
            other.close()

    def test_host_acceptance_does_not_claim_work_complete(self):
        self.seed()
        item = self.queue.prepare()[0]
        with self.assertRaises(NotificationError):
            self.queue.accept(item['id'], 'ack')
        self.queue.start(item['id'])
        self.queue.accept(item['id'], 'native-tool-ack')
        self.assertEqual(self.queue.items()[0]['state'], 'accepted')
        self.assertEqual(self.store.get_message('om_test_1')['dispatch_state'], 'notification')
        self.queue.complete(item['id'], 'checked: waiting for QA')
        self.assertEqual(self.store.get_message('om_test_1')['dispatch_state'], 'acked')

    def test_uncertain_send_cannot_be_started_twice(self):
        self.seed()
        item = self.queue.prepare()[0]
        self.queue.start(item['id'])
        with self.assertRaises(NotificationError):
            self.queue.start(item['id'])
        with self.assertRaises(NotificationError):
            self.queue.cancel(item['id'])

    def test_binding_change_prevents_sending_to_old_owner(self):
        self.seed()
        item = self.queue.prepare()[0]
        self.roster.sessions[0].conversation_id = 'conv_test_reassigned'
        with self.assertRaises(NotificationError):
            self.queue.start(item['id'])
        self.queue.cancel(item['id'])
        self.assertEqual(self.queue.prepare()[0]['conversation_id'], 'conv_test_reassigned')

    def test_disabling_all_messages_invalidates_prepared_event(self):
        self.seed()
        item = self.queue.prepare()[0]
        self.roster.sessions[0].chats[0].all_messages = False
        with self.assertRaises(NotificationError):
            self.queue.start(item['id'])

    def test_unbound_held_deleted_events_not_queued(self):
        self.seed()
        self.store.set_security('om_test_1', ['held'], 'hold', 'tp_alpha', [])
        self.seed(message_id='om_test_deleted', deleted=True)
        self.store.upsert_message(msg(message_id='om_test_bus'), 'dispatch', 'mention', [], 'tp_bus')
        self.assertEqual(self.queue.prepare(), [])

    def test_edit_after_prepare_requires_fresh_payload(self):
        self.seed()
        item = self.queue.prepare()[0]
        self.store.update_content('om_test_1', msg(content='changed'))
        with self.assertRaises(NotificationError):
            self.queue.start(item['id'])
        self.queue.cancel(item['id'])
        self.assertIn('changed', self.queue.prepare()[0]['prompt'])

    def test_new_revision_not_acked_by_old_notification(self):
        self.seed()
        item = self.queue.prepare()[0]
        self.queue.start(item['id'])
        self.queue.accept(item['id'], 'ack')
        self.store.update_content('om_test_1', msg(content='updated'), dispatch_state='pending')
        newer = self.queue.prepare()[0]
        with self.assertRaises(NotificationError):
            self.queue.start(newer['id'])
        self.queue.complete(item['id'], 'checked original')
        self.assertEqual(self.store.get_message('om_test_1')['batch_id'], newer['id'])
        self.assertEqual(self.store.get_message('om_test_1')['dispatch_state'], 'notification')
        self.queue.start(newer['id'])


class BoundSourceTest(unittest.TestCase):
    def test_native_collection_is_scoped_to_bound_groups(self):
        from sheepdog.source.lark import LarkCliSource
        source = LarkCliSource(chat_ids=['oc_test_a', 'oc_test_a', 'oc_test_b'])
        calls = []
        source._run = lambda args: calls.append(args) or {'messages': []}
        source.fetch_since('2026-01-01T00:00:00+00:00')
        self.assertEqual(calls[0][calls[0].index('--chat-id') + 1], 'oc_test_a,oc_test_b')
        empty = LarkCliSource(chat_ids=[])
        empty._run = lambda args: self.fail('Empty roster must not fetch every group')
        self.assertEqual(empty.fetch_since('2026-01-01T00:00:00+00:00'), [])
