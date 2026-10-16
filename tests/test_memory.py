"""Tests for explicit, user-started persistent memory.

Everything here is deterministic: stores are backed by a temporary
directory or an in-memory fake, the clock and id factory are injected, and
no test touches a real user data directory.
"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from core.executor import ExecutionStatus, ToolExecutor
from core.memory import (
    MEMORY_OPERATIONS,
    TOOL_MEMORY_CLEAR,
    TOOL_MEMORY_DELETE,
    TOOL_MEMORY_GET,
    TOOL_MEMORY_LIST,
    TOOL_MEMORY_SAVE,
    MemoryRecord,
    MemoryService,
    MemoryStoreError,
    MemoryValidationError,
    build_memory_specs,
)
from core.memory_store import JsonMemoryStore, default_memory_dir
from core.permissions import (
    ApprovalOutcome,
    ApprovalResult,
    PermissionLevel,
    PermissionPolicy,
)
from core.tools import ToolRegistry

FIXED_TIME = datetime(2026, 10, 16, 9, 0, 0, tzinfo=timezone.utc)


def _clock():
    return FIXED_TIME


def _ids(*values):
    """Deterministic id factory yielding ``values`` in order."""
    iterator = iter(values)
    return lambda: next(iterator)


class FakeStore:
    """In-memory ``MemoryStore`` that records writes."""

    def __init__(self, records=()):
        self.records = list(records)
        self.saves = 0

    def load(self):
        return tuple(self.records)

    def save(self, records):
        self.records = list(records)
        self.saves += 1


class FailingStore:
    def load(self):
        raise MemoryStoreError("boom")

    def save(self, records):
        raise MemoryStoreError("boom")


class RecordingApprovals:
    def __init__(self, outcome=ApprovalOutcome.APPROVED):
        self.outcome = outcome
        self.requests = []

    def request(self, request):
        self.requests.append(request)
        return ApprovalResult(self.outcome, "", request.token)


def make_executor(registry, outcome=ApprovalOutcome.APPROVED):
    return ToolExecutor(
        registry, PermissionPolicy(), RecordingApprovals(outcome)
    )


def make_registry(service):
    registry = ToolRegistry()
    for spec in build_memory_specs(service):
        registry.register(spec)
    return registry


class TestMemoryRecord(unittest.TestCase):
    def test_valid_record(self):
        record = MemoryRecord(
            identifier="a1b2c3d4",
            value="likes tea",
            category="preferences",
            created_at="2026-10-16T09:00:00+00:00",
            updated_at="2026-10-16T09:00:00+00:00",
        )
        self.assertEqual(record.identifier, "a1b2c3d4")

    def assert_invalid(self, **overrides):
        fields = {
            "identifier": "a1b2c3d4",
            "value": "likes tea",
            "category": "general",
            "created_at": "",
            "updated_at": "",
        }
        fields.update(overrides)
        with self.assertRaises(MemoryValidationError):
            MemoryRecord(**fields)

    def test_identifier_must_be_hex8(self):
        self.assert_invalid(identifier="short")
        self.assert_invalid(identifier="A1B2C3D4")
        self.assert_invalid(identifier="zzzzzzzz")

    def test_value_must_not_be_empty(self):
        self.assert_invalid(value="")
        self.assert_invalid(value="   ")

    def test_value_length_is_bounded(self):
        self.assert_invalid(value="x" * 501)

    def test_roundtrip(self):
        record = MemoryRecord("a1b2c3d4", "likes tea", "general", "t", "t")
        self.assertEqual(MemoryRecord.from_dict(record.to_dict()), record)

    def test_from_dict_rejects_non_mapping(self):
        with self.assertRaises(MemoryStoreError):
            MemoryRecord.from_dict(["not", "a", "mapping"])

    def test_from_dict_requires_keys(self):
        with self.assertRaises(MemoryStoreError):
            MemoryRecord.from_dict({"id": "a1b2c3d4"})

    def test_from_dict_rejects_wrong_types(self):
        with self.assertRaises(MemoryStoreError):
            MemoryRecord.from_dict({"id": "a1b2c3d4", "value": 5})

    def test_from_dict_rejects_invalid_content(self):
        with self.assertRaises(MemoryStoreError):
            MemoryRecord.from_dict({"id": "nope", "value": "x"})


class TestJsonMemoryStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name

    def _store(self):
        return JsonMemoryStore(self.dir)

    def test_missing_file_is_empty_and_creates_nothing(self):
        store = self._store()
        self.assertEqual(store.load(), ())
        self.assertFalse(store.exists())

    def test_save_then_reload_roundtrips(self):
        store = self._store()
        record = MemoryRecord("a1b2c3d4", "likes tea", "general", "t", "t")
        store.save([record])
        self.assertTrue(store.exists())
        self.assertEqual(self._store().load(), (record,))

    def test_file_is_fixed_inside_base_dir(self):
        store = self._store()
        self.assertEqual(store.path.parent, Path(self.dir))
        self.assertEqual(store.path.name, "memories.json")

    def test_save_leaves_no_temporary_file(self):
        store = self._store()
        store.save([MemoryRecord("a1b2c3d4", "x", "general", "t", "t")])
        leftovers = [
            name
            for name in os.listdir(self.dir)
            if name.endswith(".tmp")
        ]
        self.assertEqual(leftovers, [])

    def test_corrupt_json_is_reported_and_not_overwritten(self):
        store = self._store()
        store.path.write_text("{ not valid json", encoding="utf-8")
        before = store.path.read_text(encoding="utf-8")
        with self.assertRaises(MemoryStoreError):
            store.load()
        self.assertEqual(store.path.read_text(encoding="utf-8"), before)

    def test_unknown_version_is_reported(self):
        store = self._store()
        store.path.write_text(
            json.dumps({"version": 99, "memories": []}), encoding="utf-8"
        )
        with self.assertRaises(MemoryStoreError):
            store.load()

    def test_memories_must_be_a_list(self):
        store = self._store()
        store.path.write_text(
            json.dumps({"version": 1, "memories": {}}), encoding="utf-8"
        )
        with self.assertRaises(MemoryStoreError):
            store.load()

    def test_malformed_record_is_reported(self):
        store = self._store()
        store.path.write_text(
            json.dumps(
                {"version": 1, "memories": [{"id": "nope", "value": "x"}]}
            ),
            encoding="utf-8",
        )
        with self.assertRaises(MemoryStoreError):
            store.load()

    def test_default_memory_dir_is_a_string(self):
        self.assertIsInstance(default_memory_dir(), str)


class TestMemoryService(unittest.TestCase):
    def _service(self, **overrides):
        self.store = FakeStore()
        options = {"clock": _clock, "id_factory": _ids("00000001")}
        options.update(overrides)
        return MemoryService(self.store, **options)

    def test_save_then_list(self):
        service = self._service()
        record, created = service.save("my editor is Vim")
        self.assertTrue(created)
        self.assertEqual(record.identifier, "00000001")
        self.assertEqual(service.list(), (record,))
        self.assertEqual(self.store.saves, 1)

    def test_duplicate_save_is_a_noop(self):
        service = self._service()
        first, _ = service.save("likes tea")
        second, created = service.save("Likes Tea")
        self.assertFalse(created)
        self.assertEqual(first, second)
        self.assertEqual(len(service.list()), 1)
        self.assertEqual(self.store.saves, 1)

    def test_record_limit_is_enforced(self):
        service = self._service(max_records=1)
        service.save("first")
        with self.assertRaises(MemoryValidationError):
            service.save("second")
        self.assertEqual(len(service.list()), 1)

    def test_value_is_bounded_per_service(self):
        service = self._service(max_value_chars=5)
        with self.assertRaises(MemoryValidationError):
            service.save("far too long")

    def test_blank_value_is_rejected(self):
        service = self._service()
        with self.assertRaises(MemoryValidationError):
            service.save("   ")
        self.assertEqual(self.store.saves, 0)

    def test_find_by_id(self):
        service = self._service()
        record, _ = service.save("likes tea")
        self.assertEqual(service.find(record.identifier), (record,))

    def test_find_by_value_is_case_insensitive(self):
        service = self._service()
        record, _ = service.save("Likes Tea")
        self.assertEqual(service.find("likes tea"), (record,))

    def test_find_has_no_fuzzy_matching(self):
        service = self._service()
        service.save("likes tea")
        self.assertEqual(service.find("tea"), ())
        self.assertEqual(service.find("   "), ())

    def test_delete_targets_one_record(self):
        service = self._service()
        record, _ = service.save("likes tea")
        self.assertEqual(service.delete(record.identifier), (record,))
        self.assertEqual(service.list(), ())

    def test_delete_unknown_is_a_noop(self):
        service = self._service()
        service.save("likes tea")
        self.assertEqual(service.delete("0000dead"), ())
        self.assertEqual(len(service.list()), 1)
        self.assertEqual(self.store.saves, 1)

    def test_delete_is_ambiguous_safe(self):
        duplicate = MemoryRecord("00000001", "likes tea", "general", "t", "t")
        twin = MemoryRecord("00000002", "likes tea", "general", "t", "t")
        self.store = FakeStore([duplicate, twin])
        service = MemoryService(self.store, clock=_clock)
        matches = service.delete("likes tea")
        self.assertEqual(len(matches), 2)
        self.assertEqual(len(service.list()), 2)
        self.assertEqual(self.store.saves, 0)

    def test_clear_removes_everything(self):
        service = self._service(id_factory=_ids("00000001", "00000002"))
        service.save("first")
        service.save("second")
        self.assertEqual(service.clear(), 2)
        self.assertEqual(service.list(), ())

    def test_clear_when_empty_changes_nothing(self):
        service = self._service()
        self.assertEqual(service.clear(), 0)
        self.assertEqual(self.store.saves, 0)

    def test_reads_do_not_write(self):
        service = self._service()
        service.save("likes tea")
        service.list()
        service.find("likes tea")
        self.assertEqual(self.store.saves, 1)

    def test_list_orders_oldest_first(self):
        times = iter(
            [
                datetime(2026, 10, 16, 10, 0, tzinfo=timezone.utc),
                datetime(2026, 10, 16, 9, 0, tzinfo=timezone.utc),
            ]
        )
        self.store = FakeStore()
        service = MemoryService(
            self.store,
            clock=lambda: next(times),
            id_factory=_ids("00000001", "00000002"),
        )
        newer, _ = service.save("newer")
        older, _ = service.save("older")
        self.assertEqual(service.list(), (older, newer))

    def test_store_errors_surface(self):
        service = MemoryService(FailingStore())
        with self.assertRaises(MemoryStoreError):
            service.list()
        with self.assertRaises(MemoryStoreError):
            service.save("x")

    def test_construction_creates_no_file(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonMemoryStore(directory)
            MemoryService(store)
            self.assertFalse(store.exists())


class TestMemoryTools(unittest.TestCase):
    def _setup(self):
        self.store = FakeStore()
        self.service = MemoryService(
            self.store, clock=_clock, id_factory=_ids("00000001", "00000002")
        )
        self.registry = make_registry(self.service)
        return self.service

    def test_permission_levels(self):
        specs = {spec.name: spec for spec in build_memory_specs(self._setup())}
        self.assertIs(
            specs[TOOL_MEMORY_SAVE].permission, PermissionLevel.SAFE
        )
        self.assertIs(
            specs[TOOL_MEMORY_LIST].permission, PermissionLevel.SAFE
        )
        self.assertIs(
            specs[TOOL_MEMORY_GET].permission, PermissionLevel.SAFE
        )
        self.assertIs(
            specs[TOOL_MEMORY_DELETE].permission, PermissionLevel.CONFIRM
        )
        self.assertIs(
            specs[TOOL_MEMORY_CLEAR].permission, PermissionLevel.CONFIRM
        )

    def test_describe_never_leaks_arguments(self):
        specs = {spec.name: spec for spec in build_memory_specs(self._setup())}
        for name in (TOOL_MEMORY_SAVE, TOOL_MEMORY_GET, TOOL_MEMORY_DELETE):
            summary = specs[name].describe(
                {"value": "LEAKTOKEN", "selector": "LEAKTOKEN"}
            )
            self.assertNotIn("LEAKTOKEN", summary)

    def test_save_via_executor(self):
        self._setup()
        executor = make_executor(self.registry)
        result = executor.execute(TOOL_MEMORY_SAVE, {"value": "likes tea"})
        self.assertIs(result.status, ExecutionStatus.OK)
        self.assertEqual(len(self.service.list()), 1)

    def test_save_rejects_empty_value(self):
        self._setup()
        executor = make_executor(self.registry)
        result = executor.execute(TOOL_MEMORY_SAVE, {"value": "   "})
        self.assertIs(result.status, ExecutionStatus.INVALID)
        self.assertEqual(self.service.list(), ())

    def test_list_and_get_do_not_require_confirmation(self):
        self._setup()
        self.service.save("likes tea")
        executor = make_executor(self.registry)
        self.assertIs(
            executor.execute(TOOL_MEMORY_LIST).status, ExecutionStatus.OK
        )
        self.assertIs(
            executor.execute(
                TOOL_MEMORY_GET, {"selector": "likes tea"}
            ).status,
            ExecutionStatus.OK,
        )

    def test_list_and_get_do_not_mutate(self):
        self._setup()
        self.service.save("likes tea")
        executor = make_executor(self.registry)
        executor.execute(TOOL_MEMORY_LIST)
        executor.execute(TOOL_MEMORY_GET, {"selector": "likes tea"})
        self.assertEqual(len(self.service.list()), 1)

    def test_delete_is_denied_without_approval(self):
        self._setup()
        record, _ = self.service.save("likes tea")
        executor = make_executor(self.registry, ApprovalOutcome.DENIED)
        result = executor.execute(
            TOOL_MEMORY_DELETE, {"selector": record.identifier}
        )
        self.assertIs(result.status, ExecutionStatus.DENIED)
        self.assertEqual(len(self.service.list()), 1)

    def test_delete_succeeds_when_approved(self):
        self._setup()
        record, _ = self.service.save("likes tea")
        executor = make_executor(self.registry, ApprovalOutcome.APPROVED)
        result = executor.execute(
            TOOL_MEMORY_DELETE, {"selector": record.identifier}
        )
        self.assertIs(result.status, ExecutionStatus.OK)
        self.assertEqual(self.service.list(), ())

    def test_clear_is_denied_without_approval(self):
        self._setup()
        self.service.save("likes tea")
        executor = make_executor(self.registry, ApprovalOutcome.DENIED)
        result = executor.execute(TOOL_MEMORY_CLEAR)
        self.assertIs(result.status, ExecutionStatus.DENIED)
        self.assertEqual(len(self.service.list()), 1)

    def test_clear_succeeds_when_approved(self):
        self._setup()
        self.service.save("likes tea")
        executor = make_executor(self.registry, ApprovalOutcome.APPROVED)
        result = executor.execute(TOOL_MEMORY_CLEAR)
        self.assertIs(result.status, ExecutionStatus.OK)
        self.assertEqual(self.service.list(), ())

    def test_delete_requires_a_selector(self):
        self._setup()
        executor = make_executor(self.registry)
        result = executor.execute(TOOL_MEMORY_DELETE, {})
        self.assertIs(result.status, ExecutionStatus.INVALID)

    def test_corrupt_store_is_reported_as_text(self):
        self._setup().save("likes tea")
        self.store.load = lambda: (_ for _ in ()).throw(
            MemoryStoreError("boom")
        )
        executor = make_executor(self.registry)
        result = executor.execute(TOOL_MEMORY_LIST)
        self.assertIs(result.status, ExecutionStatus.OK)
        self.assertIn("damaged", result.value["text"])

    def test_operation_map_matches_tool_names(self):
        self.assertEqual(
            MEMORY_OPERATIONS,
            {
                "save": TOOL_MEMORY_SAVE,
                "list": TOOL_MEMORY_LIST,
                "get": TOOL_MEMORY_GET,
                "delete": TOOL_MEMORY_DELETE,
                "clear": TOOL_MEMORY_CLEAR,
            },
        )


if __name__ == "__main__":
    unittest.main()
