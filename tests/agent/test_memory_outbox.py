"""Regression tests for durable export boundaries, not provider availability."""

import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from nanobot.memory_sync.adapter import CONVERSATION_ID_KEY, SessionMemoryExporter
from nanobot.memory_sync.outbox import MemoryOutbox
from nanobot.session.manager import Session, SessionManager


class MemoryExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "private" / "outbox.sqlite3"
        self.outbox = MemoryOutbox(self.path)
        self.exporter = SessionMemoryExporter(self.outbox, known_secrets=("MY_REAL_SECRET_12345",))

    def session(self, key="discord:test"):
        session = Session(key=key)
        session.add_message("user", "Bitte merke dir: Ich bevorzuge Deutsch.")
        session.add_message("assistant", "Verstanden.")
        session._archive_observer = self.exporter.capture
        return session

    def rows(self):
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            return db.execute("SELECT * FROM jobs ORDER BY first_seq").fetchall()

    def test_checkpoint_exports_originals_not_summary(self):
        session = self.session()
        session.commit_summary_checkpoint("A lossy summary.")
        payload = json.loads(self.outbox.next_job().payload)
        content = payload["items"][0]["content"]
        self.assertIn("Ich bevorzuge Deutsch", content)
        self.assertNotIn("A lossy summary", content)
        self.assertEqual(len(session.messages), 3)
        self.assertEqual(payload["items"][0]["update_mode"], "replace")

    def test_repeated_checkpoint_and_restart_do_not_duplicate(self):
        session = self.session()
        session.commit_summary_checkpoint("First")
        first = self.outbox.next_job()
        self.exporter = SessionMemoryExporter(MemoryOutbox(self.path))
        session._archive_observer = self.exporter.capture
        session.commit_summary_checkpoint("Again")
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.outbox.next_job().operation_id, first.operation_id)

    def test_checkpoint_markers_do_not_shift_sequence(self):
        session = self.session()
        session.commit_summary_checkpoint("First")
        session.add_message("user", "Und Englisch ist auch okay.")
        session.add_message("assistant", "Okay.")
        session.commit_summary_checkpoint("Second")
        self.assertEqual([(r["first_seq"], r["last_seq"]) for r in self.rows()], [(1, 2), (3, 4)])

    def test_new_queues_before_clear_and_rotates_identity(self):
        session = self.session()
        session.clear()
        self.assertEqual(len(self.rows()), 1)
        old_id = self.rows()[0]["conversation_id"]
        self.assertEqual(session.messages, [])
        self.assertNotEqual(session.metadata[CONVERSATION_ID_KEY], old_id)
        session.add_message("user", "A new conversation")
        session.commit_summary_checkpoint("New")
        self.assertEqual(len(self.rows()), 2)
        self.assertNotEqual(self.rows()[0]["conversation_id"], self.rows()[1]["conversation_id"])

    def test_queue_failure_prevents_clear_and_checkpoint(self):
        session = self.session()
        original = list(session.messages)
        with patch.object(self.outbox, "enqueue", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                session.clear()
            self.assertEqual(session.messages, original)
            with self.assertRaises(OSError):
                session.commit_summary_checkpoint("must not commit")
            self.assertEqual(session.messages, original)
            self.assertEqual(session.last_archived, 0)

    def test_crash_after_queue_commit_before_session_save_is_idempotent(self):
        session = self.session()
        restored = Session(key=session.key, created_at=session.created_at,
                           messages=list(session.messages))
        self.exporter.capture(session, len(session.messages), "compact")
        # Restored source has no freshly assigned metadata, as after power loss.
        self.exporter.capture(restored, len(restored.messages), "compact")
        self.assertEqual(len(self.rows()), 1)

    def test_pending_jobs_survive_reopen_and_serialize_within_conversation(self):
        session = self.session()
        session.commit_summary_checkpoint("First")
        first = MemoryOutbox(self.path).next_job()
        session.add_message("user", "Another fact")
        session.commit_summary_checkpoint("Second")
        self.outbox.record(first.document_id, "submitted", delay=300)
        self.assertIsNone(self.outbox.next_job())
        self.outbox.record(first.document_id, "completed")
        self.assertEqual(self.outbox.next_job().first_seq, 3)

    def test_needs_attention_blocks_only_affected_conversation(self):
        one = self.session("discord:one")
        one.commit_summary_checkpoint("One")
        job = self.outbox.next_job()
        self.outbox.record(job.document_id, "needs_attention")
        one.add_message("user", "Later")
        one.commit_summary_checkpoint("Later")
        two = self.session("discord:two")
        two.commit_summary_checkpoint("Two")
        self.assertEqual(self.outbox.next_job().session_key, "discord:two")

    def test_local_suppression_erases_payload_and_cannot_be_requeued(self):
        session = self.session()
        session.commit_summary_checkpoint("First")
        self.outbox.suppress_session(session.key)
        self.assertIsNone(MemoryOutbox(self.path).next_job())
        self.assertEqual(self.rows()[0]["payload"], "")
        self.exporter.capture(session, len(session.messages), "compact")
        self.assertEqual(len(self.rows()), 1)

    def test_rewritten_or_shortened_exported_prefix_fails_closed(self):
        session = self.session()
        session.commit_summary_checkpoint("First")
        session.messages[0]["content"] = "Changed source"
        with self.assertRaisesRegex(ValueError, "changed"):
            session.commit_summary_checkpoint("Must fail")
        session.messages = []
        with self.assertRaisesRegex(ValueError, "shortened"):
            session.commit_summary_checkpoint("Must fail")

    def test_corrupt_payload_is_not_sent(self):
        self.session().commit_summary_checkpoint("One")
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE jobs SET payload='tampered'")
        with self.assertRaisesRegex(ValueError, "integrity"):
            self.outbox.next_job()

    def test_concurrent_enqueue_has_one_cursor_and_job(self):
        def enqueue(_):
            MemoryOutbox(self.path).enqueue("shared", "discord:test", ["a", "b"], ["Hello", "Hi"])
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(enqueue, range(8)))
        self.assertEqual(len(self.rows()), 1)

    def test_bank_change_requires_explicit_migration(self):
        with self.assertRaisesRegex(ValueError, "bank changed"):
            MemoryOutbox(self.path, bank_id="another-bank")

    def test_sanitization_excludes_injected_memory_reasoning_and_secrets(self):
        session = self.session()
        session.messages[0]["content"] += "\n\nINJECTED_MEMORY"
        session.messages[0]["_runtime_context"] = {"version": 1, "suffix": "INJECTED_MEMORY"}
        session.messages[1]["reasoning_content"] = "HIDDEN_REASONING"
        session.add_message("user", "MY_REAL_SECRET_12345 and api_key=secret-value")
        session.commit_summary_checkpoint("Summary")
        payload = self.outbox.next_job().payload
        for secret in ["INJECTED_MEMORY", "HIDDEN_REASONING", "MY_REAL_SECRET_12345", "secret-value"]:
            self.assertNotIn(secret, payload)
        self.assertIn("REDACTED", payload)

    def test_memory_recall_is_not_new_evidence(self):
        session = self.session()
        session.add_message("assistant", "", tool_calls=[{
            "id": "recall-1", "type": "function",
            "function": {"name": "mcp_hindsight_recall", "arguments": "{}"},
        }])
        session.add_message("tool", "RECALLED_MEMORY", tool_call_id="recall-1")
        session.commit_summary_checkpoint("Summary")
        self.assertNotIn("RECALLED_MEMORY", self.outbox.next_job().payload)

    def test_incomplete_tool_exchange_is_deferred_until_result(self):
        session = self.session()
        session.add_message("assistant", "", tool_calls=[{
            "id": "call-1", "type": "function",
            "function": {"name": "read_file", "arguments": "{}"},
        }])
        session.commit_summary_checkpoint("First")
        self.assertEqual(self.rows()[0]["last_seq"], 2)
        session.add_message("tool", "TOOL_RESULT", tool_call_id="call-1", name="read_file")
        session.commit_summary_checkpoint("Second")
        self.assertIn("TOOL_RESULT", self.rows()[1]["payload"])
        self.assertIn("read_file", self.rows()[1]["payload"])

    def test_new_preserves_unresolved_tool_exchange(self):
        session = self.session()
        session.add_message("assistant", "Unfinished", tool_calls=[{
            "id": "unfinished", "type": "function",
            "function": {"name": "read_file", "arguments": "{}"},
        }])
        session.clear()
        self.assertIn("Unfinished", self.outbox.next_job().payload)

    def test_queue_and_cursor_roll_back_together(self):
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TRIGGER fail_insert BEFORE INSERT ON jobs "
                       "BEGIN SELECT RAISE(ABORT, 'simulated failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.session().commit_summary_checkpoint("Fail")
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], 0)

    def test_ephemeral_and_internal_sessions_are_not_exported(self):
        from nanobot.session.manager import SessionPolicy
        session = self.session()
        session.policy = SessionPolicy(persist=False)
        session.commit_summary_checkpoint("Private")
        self.session("dream:internal").commit_summary_checkpoint("Internal")
        self.assertEqual(self.outbox.status(), {})

    def test_manager_binds_observer_to_loaded_and_cached_sessions(self):
        with patch("nanobot.config.paths.get_config_path", return_value=self.root / "config.json"):
            manager = SessionManager(self.root / "workspace", sessions_root=self.root / "sessions")
            session = manager.get_or_create("discord:test")
            manager.set_archive_observer(self.exporter.capture)
            session.add_message("user", "Hello")
            session.commit_summary_checkpoint("First")
            manager.save(session, fsync=True)
            manager.invalidate(session.key)
            restored = manager.get_or_create(session.key)
            restored.add_message("user", "Second")
            restored.commit_summary_checkpoint("Second")
        self.assertEqual(len(self.rows()), 2)

    def test_opt_in_session_and_runtime_writes_are_fsynced(self):
        with patch("nanobot.config.paths.get_config_path", return_value=self.root / "config.json"):
            manager = SessionManager(self.root / "workspace", sessions_root=self.root / "sessions")
            manager.set_archive_observer(self.exporter.capture)
            session = manager.get_or_create("discord:test")
            session.add_message("user", "Hello")
            with patch("nanobot.session.manager.os.fsync") as sync:
                manager.save(session)
                self.assertGreaterEqual(sync.call_count, 2)
            session.metadata["runtime_checkpoint"] = {"phase": "awaiting_tools"}
            with patch("nanobot.session.manager.os.fsync") as sync:
                manager.save_runtime_checkpoint(session)
                self.assertGreaterEqual(sync.call_count, 2)

    def test_export_is_disabled_by_default(self):
        from nanobot.config.schema import HindsightConfig
        self.assertFalse(HindsightConfig().enabled)
        session = Session(key="discord:unconfigured")
        session.add_message("user", "Hello")
        session.commit_summary_checkpoint("Summary")
        self.assertNotIn(CONVERSATION_ID_KEY, session.metadata)
        self.assertEqual(self.outbox.status(), {})

    def test_fork_has_independent_export_identity(self):
        with patch("nanobot.config.paths.get_config_path", return_value=self.root / "config.json"):
            manager = SessionManager(self.root / "workspace", sessions_root=self.root / "sessions")
            manager.set_archive_observer(self.exporter.capture)
            session = manager.get_or_create("discord:source")
            session.add_message("user", "One")
            session.add_message("assistant", "Two")
            session.commit_summary_checkpoint("Summary")
            manager.save(session, fsync=True)
            fork = manager.fork_session_before_user_index(session.key, "discord:fork", 1)
            self.assertNotIn(CONVERSATION_ID_KEY, fork.metadata)
            fork.add_message("user", "New question")
            fork.commit_summary_checkpoint("Fork")
        self.assertEqual(len(self.rows()), 2)
        self.assertNotEqual(self.rows()[0]["conversation_id"], self.rows()[1]["conversation_id"])


if __name__ == "__main__":
    unittest.main()
