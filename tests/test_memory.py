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
from unittest import mock

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


class UnboundApprovals:
    """Approves but never echoes the minted ticket token."""

    def request(self, request):
        return ApprovalResult(ApprovalOutcome.APPROVED, "", "wrong-token")


class BlockingRule:
    """Risk rule that escalates one named tool to BLOCKED."""

    def __init__(self, tool_name):
        self._tool_name = tool_name

    def evaluate(self, spec, args):
        if spec.name == self._tool_name:
            return PermissionLevel.BLOCKED
        return None


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

    def test_value_must_be_printable(self):
        self.assert_invalid(value="line one\nline two")

    def test_category_must_match_the_stored_charset(self):
        self.assert_invalid(category="General")
        self.assert_invalid(category="cat!")

    def test_from_dict_rejects_a_non_printable_value(self):
        with self.assertRaises(MemoryStoreError):
            MemoryRecord.from_dict(
                {"id": "a1b2c3d4", "value": "a\nb", "category": "general"}
            )

    def test_from_dict_rejects_an_invalid_category(self):
        with self.assertRaises(MemoryStoreError):
            MemoryRecord.from_dict(
                {"id": "a1b2c3d4", "value": "x", "category": "Bad!"}
            )

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

    def test_control_characters_in_the_path_are_rejected(self):
        with self.assertRaises(MemoryStoreError):
            JsonMemoryStore("bad\x00dir")

    def test_a_base_dir_that_is_a_file_is_rejected(self):
        target = Path(self.dir) / "not-a-dir"
        target.write_text("x", encoding="utf-8")
        with self.assertRaises(MemoryStoreError):
            JsonMemoryStore(str(target))

    def test_a_memory_file_that_is_a_directory_is_rejected(self):
        (Path(self.dir) / "memories.json").mkdir()
        with self.assertRaises(MemoryStoreError):
            JsonMemoryStore(self.dir)

    def test_a_missing_directory_is_created_on_demand(self):
        target = Path(self.dir) / "nested" / "memory"
        store = JsonMemoryStore(str(target))
        self.assertFalse(target.exists())
        store.save([MemoryRecord("a1b2c3d4", "x", "general", "t", "t")])
        self.assertEqual(JsonMemoryStore(str(target)).load()[0].value, "x")

    def test_failed_replace_leaves_the_original_and_no_temporary(self):
        store = self._store()
        first = MemoryRecord("a1b2c3d4", "first", "general", "t", "t")
        store.save([first])
        before = store.path.read_text(encoding="utf-8")
        with mock.patch(
            "core.memory_store.os.replace", side_effect=OSError("boom")
        ):
            with self.assertRaises(MemoryStoreError):
                store.save(
                    [MemoryRecord("b1b2c3d4", "second", "general", "t", "t")]
                )
        self.assertEqual(store.path.read_text(encoding="utf-8"), before)
        self.assertEqual(self._store().load(), (first,))
        self.assertEqual(
            [name for name in os.listdir(self.dir) if name.endswith(".tmp")],
            [],
        )

    def test_a_failing_temporary_file_is_reported_safely(self):
        store = self._store()
        with mock.patch(
            "core.memory_store.tempfile.mkstemp",
            side_effect=OSError("nope"),
        ):
            with self.assertRaises(MemoryStoreError):
                store.save(
                    [MemoryRecord("a1b2c3d4", "x", "general", "t", "t")]
                )
        self.assertFalse(store.exists())

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

    def test_construction_rejects_unusable_limits(self):
        for kwargs in (
            {"max_records": 0},
            {"max_records": 201},
            {"max_value_chars": 0},
            {"max_value_chars": 501},
            {"max_category_chars": 0},
            {"max_category_chars": 33},
            {"max_records": "100"},
            {"max_records": None},
            {"max_records": 10.5},
            {"max_records": True},
        ):
            with self.subTest(**kwargs):
                with self.assertRaises(ValueError):
                    MemoryService(FakeStore(), **kwargs)

    def test_memories_persist_across_reconstruction(self):
        with tempfile.TemporaryDirectory() as directory:
            first = MemoryService(
                JsonMemoryStore(directory),
                clock=_clock,
                id_factory=_ids("00000001"),
            )
            record, _ = first.save("my editor is Vim")
            second = MemoryService(JsonMemoryStore(directory))
            self.assertEqual(second.find(record.identifier), (record,))

    def test_a_corrupt_store_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonMemoryStore(directory)
            store.path.write_text("{ broken", encoding="utf-8")
            service = MemoryService(store)
            with self.assertRaises(MemoryStoreError):
                service.save("anything")
            with self.assertRaises(MemoryStoreError):
                service.clear()
            self.assertEqual(
                store.path.read_text(encoding="utf-8"), "{ broken"
            )

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

    def test_duplicate_save_reports_the_existing_memory(self):
        self._setup()
        executor = make_executor(self.registry)
        executor.execute(TOOL_MEMORY_SAVE, {"value": "likes tea"})
        result = executor.execute(TOOL_MEMORY_SAVE, {"value": "Likes Tea"})
        self.assertIs(result.status, ExecutionStatus.OK)
        self.assertFalse(result.value["saved"])
        self.assertEqual(len(self.service.list()), 1)

    def test_oversized_save_is_invalid(self):
        self._setup()
        executor = make_executor(self.registry)
        result = executor.execute(TOOL_MEMORY_SAVE, {"value": "x" * 501})
        self.assertIs(result.status, ExecutionStatus.INVALID)
        self.assertEqual(self.service.list(), ())

    def test_malformed_selector_is_invalid(self):
        self._setup()
        executor = make_executor(self.registry)
        self.assertIs(
            executor.execute(TOOL_MEMORY_GET, {"selector": 5}).status,
            ExecutionStatus.INVALID,
        )
        self.assertIs(
            executor.execute(
                TOOL_MEMORY_DELETE, {"selector": "x" * 501}
            ).status,
            ExecutionStatus.INVALID,
        )

    def test_list_reports_only_ids_and_values(self):
        record = MemoryRecord(
            "00000001",
            "likes tea",
            "secretcat",
            "2020-01-01T00:00:00+00:00",
            "2020-01-01T00:00:00+00:00",
        )
        self.store = FakeStore([record])
        service = MemoryService(self.store, clock=_clock)
        executor = make_executor(make_registry(service))
        result = executor.execute(TOOL_MEMORY_LIST)
        text = result.value["text"]
        self.assertIn("00000001: likes tea", text)
        self.assertNotIn("secretcat", text)
        self.assertNotIn("2020-01-01", text)
        self.assertNotIn("memories.json", text)

    def test_ambiguous_get_and_delete_ask_for_an_id(self):
        self.store = FakeStore(
            [
                MemoryRecord("00000001", "likes tea", "general", "t", "t"),
                MemoryRecord("00000002", "likes tea", "general", "t", "t"),
            ]
        )
        service = MemoryService(self.store, clock=_clock)
        executor = make_executor(make_registry(service))
        get_result = executor.execute(
            TOOL_MEMORY_GET, {"selector": "likes tea"}
        )
        self.assertIn("memory id", get_result.value["text"])
        delete_result = executor.execute(
            TOOL_MEMORY_DELETE, {"selector": "likes tea"}
        )
        self.assertIn("nothing was deleted", delete_result.value["text"])
        self.assertEqual(len(service.list()), 2)

    def test_save_is_blocked_by_policy_without_a_write(self):
        self._setup()
        executor = ToolExecutor(
            self.registry,
            PermissionPolicy([BlockingRule(TOOL_MEMORY_SAVE)]),
            RecordingApprovals(),
        )
        result = executor.execute(TOOL_MEMORY_SAVE, {"value": "x"})
        self.assertIs(result.status, ExecutionStatus.BLOCKED)
        self.assertEqual(self.service.list(), ())
        self.assertEqual(self.store.saves, 0)

    def test_delete_denied_when_the_approval_is_not_bound(self):
        self._setup()
        record, _ = self.service.save("likes tea")
        executor = ToolExecutor(
            self.registry, PermissionPolicy(), UnboundApprovals()
        )
        result = executor.execute(
            TOOL_MEMORY_DELETE, {"selector": record.identifier}
        )
        self.assertIs(result.status, ExecutionStatus.DENIED)
        self.assertEqual(len(self.service.list()), 1)

    def test_shutdown_denies_memory_operations(self):
        self._setup()
        record, _ = self.service.save("likes tea")
        executor = make_executor(self.registry)
        executor.close()
        self.assertIs(
            executor.execute(TOOL_MEMORY_SAVE, {"value": "x"}).status,
            ExecutionStatus.DENIED,
        )
        self.assertIs(
            executor.execute(
                TOOL_MEMORY_DELETE, {"selector": record.identifier}
            ).status,
            ExecutionStatus.DENIED,
        )
        self.assertIs(
            executor.execute(TOOL_MEMORY_CLEAR).status,
            ExecutionStatus.DENIED,
        )
        self.assertEqual(len(self.service.list()), 1)
        self.assertEqual(self.store.saves, 1)

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
