import tempfile
import unittest
from pathlib import Path
from hh_agent.models import Letter
from hh_agent.store import Store
from tests.test_rules import vacancy


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/"queue.db"
        self.db=Store(self.path)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def ready(self):
        self.db.discover(vacancy())
        self.db.decide("123","ready","подходит")
        self.db.save_letter("123",Letter("Письмо",["fastapi"],[],[]))

    def test_terminal_test_skip_survives_search(self):
        self.db.discover(vacancy())
        self.db.decide("123","rejected","Пропущена: предложено пройти тест")
        self.assertFalse(self.db.discover(vacancy()))
        self.assertEqual(self.db.get("123")["status"],"rejected")

    def test_two_connections_cannot_claim_same_application(self):
        self.ready()
        self.db.pause(False)
        other=Store(self.path)
        try:
            self.assertTrue(self.db.claim("123","resume123"))
            self.assertFalse(other.claim("123","resume123"))
        finally:
            other.close()

    def test_pause_blocks_submit(self):
        self.ready()
        self.assertFalse(self.db.claim("123","resume123"))
        self.assertEqual(self.db.get("123")["status"],"ready")

    def test_crash_recovery_never_requeues_send(self):
        self.ready()
        self.db.pause(False)
        self.db.claim("123","resume123")
        self.db.recover()
        self.assertEqual(self.db.get("123")["status"],"unknown")
        self.assertFalse(self.db.claim("123","resume123"))

    def test_success_and_notification_are_atomic(self):
        self.ready()
        self.db.finish("123","sent","Вы откликнулись",notification="Успешно")
        self.assertEqual(self.db.get("123")["status"],"sent")
        self.assertEqual(self.db.pending_messages()[0]["text"],"Успешно")
        self.assertFalse(self.db.discover(vacancy()))

    def test_sent_requires_platform_evidence(self):
        self.ready()
        with self.assertRaises(ValueError):
            self.db.finish("123","sent","")

    def test_update_dedupe_survives_restart(self):
        self.assertTrue(self.db.command(51,"pause"))
        self.db.close()
        self.db=Store(self.path)
        self.assertFalse(self.db.command(51,"resume"))
        self.assertTrue(self.db.is_paused())

    def test_confirmed_pilot_is_never_queued(self):
        self.assertEqual(self.db.get("137924141")["status"],"sent")
