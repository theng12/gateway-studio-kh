#!/usr/bin/env python3
"""Gateway G1's durable render journal, isolated to SQLite and the store.

The database files are deliberately retained under this worktree's ``_trash`` folder.  The
suite imports no API, worker, fleet, adapter, Hub, Story, provider, compiler, or media code.

    .venv/bin/python tests/test_render_journal_g1.py
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gateway import store as store_module  # noqa: E402

JobStore = store_module.JobStore

ARTIFACT_ROOT = ROOT / "_trash" / "tests" / f"render-journal-g1-{uuid.uuid4().hex}"
ARTIFACT_ROOT.mkdir(parents=True)

IMMUTABLE_FIELDS = (
    "journal_id",
    "story_uuid",
    "intent_id",
    "attempt_no",
    "gateway_attempt_key",
    "owner_scope",
    "inbound_story_body_sha256",
    "outbound_hub_body_sha256",
    "packet_sha256",
    "destination_site",
    "destination_controller",
    "destination_node",
    "dispatch_key",
    "dispatch_fence",
)
REPLAY_FIELDS = tuple(field for field in IMMUTABLE_FIELDS if field != "journal_id")


def journal_values(**changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "journal_id": "11111111-1111-4111-8111-111111111111",
        "story_uuid": "story-2026-10-15-001",
        "intent_id": "render-final-cut",
        "attempt_no": 1,
        "gateway_attempt_key": "gateway-attempt-001",
        # Opaque server-internal data only. This fixture makes no authority claim.
        "owner_scope": "internal-scope-alpha",
        "inbound_story_body_sha256": "a" * 64,
        "outbound_hub_body_sha256": "b" * 64,
        "packet_sha256": "c" * 64,
        "destination_site": "site-phnom-penh",
        "destination_controller": "controller-a",
        "destination_node": "node-m4-01",
        "dispatch_key": "dispatch-001",
        "dispatch_fence": "fence-001",
    }
    values.update(changes)
    return values


def replay_values(values: dict[str, object]) -> dict[str, object]:
    return {field: values[field] for field in REPLAY_FIELDS}


class RenderJournalCase(unittest.TestCase):
    def setUp(self) -> None:
        self.case_root = ARTIFACT_ROOT / self.id().rsplit(".", 1)[-1]
        self.case_root.mkdir()
        self.path = self.case_root / "gateway.sqlite3"
        self.store = JobStore(self.path)
        self.addCleanup(self.store.close)

    def create(self, store: JobStore | None = None, **changes: object) -> dict:
        return (store or self.store).create_render_journal(**journal_values(**changes))

    def race(self, *operations):
        gate = threading.Barrier(len(operations) + 1)
        results: list[tuple[str, object] | None] = [None] * len(operations)

        def run(index: int, operation) -> None:
            gate.wait()
            try:
                results[index] = ("ok", operation())
            except Exception as exc:  # The asserted exception type is checked by each caller.
                results[index] = ("error", exc)

        threads = [threading.Thread(target=run, args=(i, fn)) for i, fn in enumerate(operations)]
        for thread in threads:
            thread.start()
        gate.wait()
        for thread in threads:
            thread.join(10)
            self.assertFalse(thread.is_alive(), "SQLite race did not settle")
        return results


class CreationAndReplayTests(RenderJournalCase):
    def test_staging_assets_and_delivery_commit_as_one_additive_transaction(self) -> None:
        inbound = b'{"fixture":"private-render"}'
        capability = b'{"profile":"fixture"}'
        render_id = "77777777-7777-4777-8777-777777777777"
        row = self.store.create_render_staging(
            render_id=render_id,
            gateway_instance_id="99999999-9999-4999-8999-999999999999",
            story_instance_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            owner_scope="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            story_uuid="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            intent_id="fixture-final",
            attempt_no=1,
            gateway_attempt_key="fixture-attempt",
            inbound_story_body=inbound,
            inbound_story_body_sha256=hashlib.sha256(inbound).hexdigest(),
            packet_sha256="2" * 64,
            destination_site="fixture-site",
            destination_controller="fixture-roster",
            destination_hub_controller="fixture-hub",
            destination_node="fixture-node",
            selected_machine=b'{"id":"fixture-node"}',
            capability_snapshot=capability,
            capability_snapshot_sha256=hashlib.sha256(capability).hexdigest(),
            assets=[{
                "logical_id": "base", "gateway_asset_handle": "3" * 32,
                "sha256": "4" * 64, "size_bytes": 7,
            }],
        )

        self.assertEqual(row["id"], render_id)
        self.assertEqual(row["assets"][0]["logical_id"], "base")
        self.assertEqual(self.store.get_render_delivery(render_id)["revision"], 0)
        counts = tuple(self.store._conn.execute(
            f"SELECT COUNT(*) FROM {table}"
        ).fetchone()[0] for table in (
            "render_staging", "render_staging_assets", "render_delivery",
        ))
        self.assertEqual(counts, (1, 1, 1))

    def test_fresh_database_adds_journal_without_changing_an_ordinary_generation_row(self) -> None:
        generation, created = self.store.submit(
            modality="image",
            model_id="model/example",
            params={"prompt": "ordinary generation"},
            idempotency_key="ordinary-generation",
            provider_id="provider-a",
        )
        self.assertTrue(created)
        before = self.store.get(generation["id"])

        self.create()
        table = self.store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'render_journal'"
        ).fetchone()
        self.assertEqual(table["name"], "render_journal")
        self.assertEqual(self.store.get(generation["id"]), before)

        self.store.close()
        reopened = JobStore(self.path)
        self.store = reopened
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.get(generation["id"]), before)
        self.assertEqual(reopened.get_render_journal(journal_values()["journal_id"])["state"], "prepared")

    def test_creation_persists_every_immutable_field_and_exact_create_replays(self) -> None:
        values = journal_values()
        row = self.store.create_render_journal(**values)
        for field in IMMUTABLE_FIELDS:
            self.assertEqual(row[field], values[field], field)
        self.assertEqual(row["state"], "prepared")
        self.assertIsNone(row["send_may_start_at"])
        self.assertIsNone(row["hub_job_id"])
        self.assertIsNone(row["cancel_requested_at"])
        self.assertEqual(self.store.create_render_journal(**values), row)

        changed_id = dict(values, journal_id="22222222-2222-4222-8222-222222222222")
        with self.assertRaises(store_module.RenderJournalConflict):
            self.store.create_render_journal(**changed_id)
        self.assertEqual(self.store._conn.execute("SELECT COUNT(*) FROM render_journal").fetchone()[0], 1)

    def test_each_scoped_composite_identity_conflicts_but_two_scopes_remain_separate(self) -> None:
        self.create()
        conflicts = (
            journal_values(
                journal_id="22222222-2222-4222-8222-222222222222",
                gateway_attempt_key="gateway-attempt-other",
                dispatch_key="dispatch-other",
            ),
            journal_values(
                journal_id="33333333-3333-4333-8333-333333333333",
                story_uuid="story-other",
                intent_id="intent-other",
                attempt_no=2,
                dispatch_key="dispatch-other-2",
            ),
            journal_values(
                journal_id="44444444-4444-4444-8444-444444444444",
                story_uuid="story-other-2",
                intent_id="intent-other-2",
                attempt_no=3,
                gateway_attempt_key="gateway-attempt-other-2",
            ),
        )
        for values in conflicts:
            with self.subTest(values=values):
                with self.assertRaises(store_module.RenderJournalConflict):
                    self.store.create_render_journal(**values)

        other_scope = self.create(
            journal_id="55555555-5555-4555-8555-555555555555",
            owner_scope="internal-scope-beta",
        )
        self.assertEqual(other_scope["owner_scope"], "internal-scope-beta")
        self.assertEqual(self.store._conn.execute("SELECT COUNT(*) FROM render_journal").fetchone()[0], 2)

    def test_scoped_replay_compares_every_immutable_caller_field(self) -> None:
        values = journal_values()
        created = self.store.create_render_journal(**values)
        replay = replay_values(values)
        self.assertEqual(self.store.replay_render_journal(**replay), created)
        self.assertEqual(
            self.store.get_render_journal_by_attempt_key(
                owner_scope=values["owner_scope"], gateway_attempt_key=values["gateway_attempt_key"]
            ),
            created,
        )
        with self.assertRaises(TypeError):
            self.store.get_render_journal_by_attempt_key(
                gateway_attempt_key=values["gateway_attempt_key"]
            )

        changes: dict[str, object] = {
            "story_uuid": "story-changed",
            "intent_id": "intent-changed",
            "attempt_no": 2,
            "gateway_attempt_key": "gateway-key-missing",
            "inbound_story_body_sha256": "d" * 64,
            "outbound_hub_body_sha256": "e" * 64,
            "packet_sha256": "f" * 64,
            "destination_site": "site-changed",
            "destination_controller": "controller-changed",
            "destination_node": "node-changed",
            "dispatch_key": "dispatch-changed",
            "dispatch_fence": "fence-changed",
        }
        for field, changed in changes.items():
            candidate = dict(replay, **{field: changed})
            with self.subTest(field=field):
                if field == "gateway_attempt_key":
                    self.assertIsNone(self.store.replay_render_journal(**candidate))
                else:
                    with self.assertRaises(store_module.RenderJournalConflict):
                        self.store.replay_render_journal(**candidate)

        # Scope is part of the lookup boundary: a missing scoped key reveals no row.
        self.assertIsNone(self.store.replay_render_journal(
            **dict(replay, owner_scope="internal-scope-missing")
        ))
        self.assertEqual(self.store._conn.execute("SELECT COUNT(*) FROM render_journal").fetchone()[0], 1)

    def test_validation_rejects_malformed_values_and_accepts_real_nonempty_boundaries(self) -> None:
        text_fields = (
            "story_uuid", "intent_id", "gateway_attempt_key", "owner_scope",
            "destination_site", "destination_controller", "destination_node",
            "dispatch_key", "dispatch_fence",
        )
        for field in text_fields:
            for invalid in ("", "non-ascii-é", "line\nbreak", "x" * 257):
                with self.subTest(field=field, invalid=invalid[:20]):
                    with self.assertRaises(ValueError):
                        self.create(**{field: invalid})
        for invalid in ("not-a-uuid", "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"):
            with self.subTest(journal_id=invalid):
                with self.assertRaises(ValueError):
                    self.create(journal_id=invalid)
        for invalid in (True, False, 0, -1, 2**63, 1.0, "1"):
            with self.subTest(attempt_no=invalid):
                with self.assertRaises(ValueError):
                    self.create(attempt_no=invalid)
        for field in ("inbound_story_body_sha256", "outbound_hub_body_sha256", "packet_sha256"):
            for invalid in ("a" * 63, "A" * 64, "g" * 64):
                with self.subTest(field=field, invalid=invalid[:8]):
                    with self.assertRaises(ValueError):
                        self.create(**{field: invalid})
        with patch.object(store_module.time, "time", return_value=float("nan")):
            with self.assertRaises(ValueError):
                self.create()

        row = self.create(
            journal_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            attempt_no=2**63 - 1,
            destination_node="n" * 256,
        )
        self.assertEqual((row["attempt_no"], len(row["destination_node"])), (2**63 - 1, 256))

    def test_empty_journal_id_is_rejected_by_create_read_and_every_transition(self) -> None:
        operations = (
            ("create", lambda: self.create(journal_id="")),
            ("read", lambda: self.store.get_render_journal("")),
            ("send", lambda: self.store.mark_render_send_may_start("")),
            ("unknown", lambda: self.store.mark_render_send_unknown("")),
            ("accept", lambda: self.store.record_render_acceptance("", "hub-job-empty-id")),
            ("cancel", lambda: self.store.request_render_cancel("")),
        )
        for name, operation in operations:
            with self.subTest(operation=name):
                with self.assertRaisesRegex(ValueError, "canonical lowercase UUID"):
                    operation()
        self.assertEqual(self.store._conn.execute("SELECT COUNT(*) FROM render_journal").fetchone()[0], 0)


class TransitionTests(RenderJournalCase):
    def test_send_marker_distinguishes_new_cas_winner_from_idempotent_read_then_unknown_persists(self) -> None:
        created = self.create()
        immutable = {field: created[field] for field in IMMUTABLE_FIELDS}

        first = self.store.mark_render_send_may_start(created["journal_id"])
        self.assertEqual(first["state"], "send_may_start")
        self.assertIs(first["newly_recorded"], True)
        self.assertIsNotNone(first["send_may_start_at"])
        second = self.store.mark_render_send_may_start(created["journal_id"])
        self.assertIs(second["newly_recorded"], False)
        self.assertEqual(second["send_may_start_at"], first["send_may_start_at"])

        unknown = self.store.mark_render_send_unknown(created["journal_id"])
        self.assertEqual(unknown["state"], "send_unknown")
        self.assertEqual(self.store.mark_render_send_unknown(created["journal_id"]), unknown)
        unknown = self.store.request_render_cancel(created["journal_id"])
        self.assertEqual(unknown["state"], "send_unknown")
        self.assertIsNotNone(unknown["cancel_requested_at"])
        with self.assertRaises(store_module.RenderJournalConflict):
            self.store.mark_render_send_may_start(created["journal_id"])
        self.assertEqual({field: unknown[field] for field in IMMUTABLE_FIELDS}, immutable)
        self.assertIsNone(unknown["hub_job_id"])

        self.store.close()
        reopened = JobStore(self.path)
        self.store = reopened
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.get_render_journal(created["journal_id"]), unknown)

    def test_acceptance_from_send_or_unknown_records_one_immutable_downstream_hub_id(self) -> None:
        journal_ids = (
            "22222222-2222-4222-8222-222222222222",
            "33333333-3333-4333-8333-333333333333",
        )
        for index, go_unknown in enumerate((False, True), 1):
            with self.subTest(go_unknown=go_unknown):
                row = self.create(
                    journal_id=journal_ids[index - 1],
                    story_uuid=f"story-{index}",
                    intent_id=f"intent-{index}",
                    attempt_no=index,
                    gateway_attempt_key=f"gateway-{index}",
                    dispatch_key=f"dispatch-{index}",
                )
                self.assertIsNone(row["hub_job_id"])
                self.store.mark_render_send_may_start(row["journal_id"])
                if go_unknown:
                    self.store.mark_render_send_unknown(row["journal_id"])
                accepted = self.store.record_render_acceptance(row["journal_id"], f"hub-job-{index}")
                self.assertEqual((accepted["state"], accepted["hub_job_id"]),
                                 ("accepted", f"hub-job-{index}"))
                self.assertEqual(
                    self.store.record_render_acceptance(row["journal_id"], f"hub-job-{index}"),
                    accepted,
                )
                accepted = self.store.request_render_cancel(row["journal_id"])
                self.assertEqual((accepted["state"], accepted["hub_job_id"]),
                                 ("accepted", f"hub-job-{index}"))
                self.assertIsNotNone(accepted["cancel_requested_at"])
                with self.assertRaises(store_module.RenderJournalConflict):
                    self.store.record_render_acceptance(row["journal_id"], "hub-job-changed")
                with self.assertRaises(store_module.RenderJournalConflict):
                    self.store.mark_render_send_unknown(row["journal_id"])

        for invalid in ("", "non-ascii-é", "line\nbreak", "x" * 257):
            with self.subTest(hub_job_id=invalid[:20]):
                with self.assertRaises(ValueError):
                    self.store.record_render_acceptance(row["journal_id"], invalid)

    def test_cancel_before_send_fences_it_and_send_before_cancel_only_records_a_request(self) -> None:
        cancel_first = self.create()
        cancelled = self.store.request_render_cancel(cancel_first["journal_id"])
        self.assertEqual(cancelled["state"], "prepared")
        self.assertIsNotNone(cancelled["cancel_requested_at"])
        self.assertEqual(self.store.request_render_cancel(cancel_first["journal_id"]), cancelled)
        with self.assertRaises(store_module.RenderJournalCancelled):
            self.store.mark_render_send_may_start(cancel_first["journal_id"])

        send_first = self.create(
            journal_id="22222222-2222-4222-8222-222222222222",
            story_uuid="story-2",
            intent_id="intent-2",
            attempt_no=2,
            gateway_attempt_key="gateway-2",
            dispatch_key="dispatch-2",
        )
        first = self.store.mark_render_send_may_start(send_first["journal_id"])
        self.assertIs(first["newly_recorded"], True)
        requested = self.store.request_render_cancel(send_first["journal_id"])
        self.assertEqual(requested["state"], "send_may_start")
        self.assertIsNotNone(requested["cancel_requested_at"])
        replay = self.store.mark_render_send_may_start(send_first["journal_id"])
        self.assertIs(replay["newly_recorded"], False)
        self.assertEqual(replay["send_may_start_at"], first["send_may_start_at"])

    def test_missing_writer_targets_are_typed_conflicts_and_never_allocate_lineage(self) -> None:
        missing = "99999999-9999-4999-8999-999999999999"
        operations = (
            lambda: self.store.mark_render_send_may_start(missing),
            lambda: self.store.mark_render_send_unknown(missing),
            lambda: self.store.record_render_acceptance(missing, "hub-job"),
            lambda: self.store.request_render_cancel(missing),
        )
        for operation in operations:
            with self.subTest(operation=operation):
                with self.assertRaises(store_module.RenderJournalNotFound):
                    operation()
        self.assertEqual(self.store._conn.execute("SELECT COUNT(*) FROM render_journal").fetchone()[0], 0)


class TransactionAndRaceTests(RenderJournalCase):
    def second_store(self) -> JobStore:
        store = JobStore(self.path)
        self.addCleanup(store.close)
        return store

    def test_every_write_has_an_explicit_transaction_and_conflict_rolls_back_cleanly(self) -> None:
        trace: list[str] = []
        self.store._conn.set_trace_callback(trace.append)
        try:
            row = self.create()
            self.store.mark_render_send_may_start(row["journal_id"])
            self.store.mark_render_send_unknown(row["journal_id"])
            self.store.record_render_acceptance(row["journal_id"], "hub-job-traced")
            self.store.request_render_cancel(row["journal_id"])
            with self.assertRaises(store_module.RenderJournalConflict):
                self.create(journal_id="22222222-2222-4222-8222-222222222222")
        finally:
            self.store._conn.set_trace_callback(None)
        statements = [sql.strip().upper() for sql in trace]
        self.assertGreaterEqual(sum(sql.startswith("BEGIN IMMEDIATE") for sql in statements), 6)
        self.assertGreaterEqual(statements.count("COMMIT"), 5)
        self.assertIn("ROLLBACK", statements)
        self.assertFalse(self.store._conn.in_transaction)
        durable = self.store.get_render_journal(journal_values()["journal_id"])
        self.assertEqual((durable["state"], durable["hub_job_id"]), ("accepted", "hub-job-traced"))

    def test_sqlite_write_contention_rolls_back_and_the_losing_connection_remains_usable(self) -> None:
        other = self.second_store()
        other._conn.execute("PRAGMA busy_timeout = 1")
        self.store._conn.execute("BEGIN IMMEDIATE")
        try:
            with self.assertRaises(sqlite3.OperationalError):
                other.create_render_journal(**journal_values())
        finally:
            self.store._conn.rollback()
        self.assertFalse(other._conn.in_transaction)
        row = other.create_render_journal(**journal_values())
        self.assertEqual(row["state"], "prepared")

    def test_two_connections_racing_changed_creates_have_one_durable_winner_and_both_remain_usable(self) -> None:
        other = self.second_store()
        first = journal_values()
        second = journal_values(journal_id="22222222-2222-4222-8222-222222222222")
        results = self.race(
            lambda: self.store.create_render_journal(**first),
            lambda: other.create_render_journal(**second),
        )
        self.assertEqual([kind for kind, _ in results].count("ok"), 1)
        errors = [value for kind, value in results if kind == "error"]
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], store_module.RenderJournalConflict)
        self.assertFalse(self.store._conn.in_transaction)
        self.assertFalse(other._conn.in_transaction)
        self.assertEqual(other._conn.execute("SELECT COUNT(*) FROM render_journal").fetchone()[0], 1)

        # The connection that lost the uniqueness race can still commit independent work.
        other.create_render_journal(**journal_values(
            journal_id="33333333-3333-4333-8333-333333333333",
            story_uuid="story-3",
            intent_id="intent-3",
            attempt_no=3,
            gateway_attempt_key="gateway-3",
            dispatch_key="dispatch-3",
        ))
        self.assertEqual(self.store._conn.execute("SELECT COUNT(*) FROM render_journal").fetchone()[0], 2)

        self.store.close()
        other.close()
        reopened = JobStore(self.path)
        self.store = reopened
        self.addCleanup(reopened.close)
        self.assertEqual(reopened._conn.execute("SELECT COUNT(*) FROM render_journal").fetchone()[0], 2)

    def test_two_connections_racing_the_send_marker_return_one_newly_recorded_result(self) -> None:
        row = self.create()
        other = self.second_store()
        results = self.race(
            lambda: self.store.mark_render_send_may_start(row["journal_id"]),
            lambda: other.mark_render_send_may_start(row["journal_id"]),
        )
        self.assertTrue(all(kind == "ok" for kind, _ in results))
        self.assertEqual(sorted(value["newly_recorded"] for _, value in results), [False, True])
        self.assertEqual(other.get_render_journal(row["journal_id"])["state"], "send_may_start")

    def test_two_connections_racing_cancel_and_send_preserve_whichever_order_commits(self) -> None:
        row = self.create()
        other = self.second_store()
        results = self.race(
            lambda: self.store.mark_render_send_may_start(row["journal_id"]),
            lambda: other.request_render_cancel(row["journal_id"]),
        )
        final = self.store.get_render_journal(row["journal_id"])
        self.assertIsNotNone(final["cancel_requested_at"])
        if final["state"] == "prepared":
            self.assertTrue(any(
                kind == "error" and isinstance(value, store_module.RenderJournalCancelled)
                for kind, value in results
            ))
        else:
            self.assertEqual(final["state"], "send_may_start")
            sends = [value for kind, value in results if kind == "ok" and "newly_recorded" in value]
            self.assertEqual(len(sends), 1)
            self.assertIs(sends[0]["newly_recorded"], True)

    def test_two_connections_racing_changed_acceptance_keep_one_hub_id(self) -> None:
        row = self.create()
        self.store.mark_render_send_may_start(row["journal_id"])
        other = self.second_store()
        results = self.race(
            lambda: self.store.record_render_acceptance(row["journal_id"], "hub-job-a"),
            lambda: other.record_render_acceptance(row["journal_id"], "hub-job-b"),
        )
        self.assertEqual([kind for kind, _ in results].count("ok"), 1)
        errors = [value for kind, value in results if kind == "error"]
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], store_module.RenderJournalConflict)
        self.assertIn(self.store.get_render_journal(row["journal_id"])["hub_job_id"],
                      {"hub-job-a", "hub-job-b"})

    def test_unknown_and_acceptance_race_never_moves_backwards_from_accepted(self) -> None:
        row = self.create()
        self.store.mark_render_send_may_start(row["journal_id"])
        other = self.second_store()
        results = self.race(
            lambda: self.store.mark_render_send_unknown(row["journal_id"]),
            lambda: other.record_render_acceptance(row["journal_id"], "hub-job-final"),
        )
        self.assertTrue(any(kind == "ok" for kind, _ in results))
        final = self.store.get_render_journal(row["journal_id"])
        self.assertEqual((final["state"], final["hub_job_id"]), ("accepted", "hub-job-final"))
        self.assertFalse(self.store._conn.in_transaction)
        self.assertFalse(other._conn.in_transaction)


class DataOnlyBoundaryTests(RenderJournalCase):
    def test_replay_is_independent_of_mutable_process_configuration(self) -> None:
        values = journal_values()
        row = self.store.create_render_journal(**values)
        mutable_hints = {
            "GATEWAY_QUEUE_PAUSED": "1",
            "GATEWAY_RENDER_PROFILE": "changed",
            "GATEWAY_MEMORY_GB": "0",
            "GATEWAY_DISK_GB": "0",
        }
        with patch.dict(os.environ, mutable_hints, clear=False):
            self.assertEqual(self.store.replay_render_journal(**replay_values(values)), row)
        self.assertEqual(set(row), {
            *IMMUTABLE_FIELDS,
            "state", "send_may_start_at", "hub_job_id", "cancel_requested_at",
            "created_at", "updated_at",
        })


if __name__ == "__main__":
    unittest.main()
