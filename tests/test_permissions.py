"""Unit tests for the permission model, risk rules and approval.

Everything here is pure logic: no microphone, no real terminal, and
filesystem interactions limited to temporary directories.
"""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.permissions import (
    ApprovalOutcome,
    ApprovalRequest,
    ConsoleApprovalProvider,
    PathBoundaryRule,
    PermissionLevel,
    PermissionPolicy,
)


def make_spec(name="demo", permission=PermissionLevel.SAFE):
    """A minimal ToolSpec stand-in with only the attributes rules use."""
    return SimpleNamespace(
        name=name, permission=permission, description="demo tool"
    )


class FixedRule:
    def __init__(self, level):
        self.level = level
        self.calls = 0

    def evaluate(self, spec, args):
        self.calls += 1
        return self.level


class RaisingRule:
    def evaluate(self, spec, args):
        raise RuntimeError("rule exploded")


class TestPermissionLevel(unittest.TestCase):
    def test_rank_is_strictly_increasing(self):
        self.assertLess(
            PermissionLevel.SAFE.rank, PermissionLevel.CONFIRM.rank
        )
        self.assertLess(
            PermissionLevel.CONFIRM.rank, PermissionLevel.HIGH_RISK.rank
        )
        self.assertLess(
            PermissionLevel.HIGH_RISK.rank, PermissionLevel.BLOCKED.rank
        )

    def test_requires_approval_flags(self):
        self.assertFalse(PermissionLevel.SAFE.requires_approval)
        self.assertTrue(PermissionLevel.CONFIRM.requires_approval)
        self.assertTrue(PermissionLevel.HIGH_RISK.requires_approval)
        self.assertFalse(PermissionLevel.BLOCKED.requires_approval)


class TestPermissionPolicyBaseLevels(unittest.TestCase):
    def setUp(self):
        self.policy = PermissionPolicy()

    def test_safe_needs_no_approval(self):
        decision = self.policy.classify(make_spec(), {})
        self.assertIs(decision.level, PermissionLevel.SAFE)
        self.assertFalse(decision.requires_approval)

    def test_confirm_requires_approval(self):
        decision = self.policy.classify(
            make_spec(permission=PermissionLevel.CONFIRM), {}
        )
        self.assertIs(decision.level, PermissionLevel.CONFIRM)
        self.assertTrue(decision.requires_approval)

    def test_high_risk_requires_approval(self):
        decision = self.policy.classify(
            make_spec(permission=PermissionLevel.HIGH_RISK), {}
        )
        self.assertIs(decision.level, PermissionLevel.HIGH_RISK)
        self.assertTrue(decision.requires_approval)

    def test_blocked_is_terminal_and_needs_no_approval(self):
        decision = self.policy.classify(
            make_spec(permission=PermissionLevel.BLOCKED), {}
        )
        self.assertIs(decision.level, PermissionLevel.BLOCKED)
        self.assertFalse(decision.requires_approval)
        self.assertIn("blocked", decision.reason.lower())

    def test_base_level_still_runs_rules(self):
        # Blocked base short-circuits, so rules are not consulted.
        rule = FixedRule(PermissionLevel.SAFE)
        policy = PermissionPolicy(escalations=[rule])
        policy.classify(make_spec(permission=PermissionLevel.BLOCKED), {})
        self.assertEqual(rule.calls, 0)


class TestPermissionPolicyEscalation(unittest.TestCase):
    def test_rule_can_escalate_safe_to_high_risk(self):
        policy = PermissionPolicy(
            escalations=[FixedRule(PermissionLevel.HIGH_RISK)]
        )
        decision = policy.classify(make_spec(), {})
        self.assertIs(decision.level, PermissionLevel.HIGH_RISK)
        self.assertTrue(decision.requires_approval)
        self.assertIn("escalated", decision.reason)

    def test_rule_can_escalate_to_blocked(self):
        policy = PermissionPolicy(
            escalations=[FixedRule(PermissionLevel.BLOCKED)]
        )
        decision = policy.classify(make_spec(), {})
        self.assertIs(decision.level, PermissionLevel.BLOCKED)
        self.assertFalse(decision.requires_approval)

    def test_rule_cannot_downgrade(self):
        policy = PermissionPolicy(
            escalations=[FixedRule(PermissionLevel.CONFIRM)]
        )
        decision = policy.classify(
            make_spec(permission=PermissionLevel.HIGH_RISK), {}
        )
        self.assertIs(decision.level, PermissionLevel.HIGH_RISK)

    def test_none_result_leaves_level_unchanged(self):
        policy = PermissionPolicy(escalations=[FixedRule(None)])
        decision = policy.classify(make_spec(), {})
        self.assertIs(decision.level, PermissionLevel.SAFE)

    def test_malformed_rule_result_fails_closed(self):
        policy = PermissionPolicy(escalations=[FixedRule("high_risk")])
        decision = policy.classify(make_spec(), {})
        self.assertIs(decision.level, PermissionLevel.BLOCKED)

    def test_raising_rule_fails_closed(self):
        policy = PermissionPolicy(escalations=[RaisingRule()])
        decision = policy.classify(make_spec(), {})
        self.assertIs(decision.level, PermissionLevel.BLOCKED)


class TestPathBoundaryRule(unittest.TestCase):
    def setUp(self):
        self._base = tempfile.TemporaryDirectory()
        self._other = tempfile.TemporaryDirectory()
        self.base = Path(self._base.name).resolve()
        self.other = Path(self._other.name).resolve()
        self.forbidden = self.base / "secret"
        self.forbidden.mkdir()

    def tearDown(self):
        self._base.cleanup()
        self._other.cleanup()

    def make_rule(self, **overrides):
        params = {
            "tool_names": ["open_file"],
            "arg_keys": ["path"],
            "allowed_roots": [str(self.base)],
            "forbidden_roots": [str(self.forbidden)],
        }
        params.update(overrides)
        return PathBoundaryRule(**params)

    def evaluate(self, rule, value, name="open_file"):
        return rule.evaluate(make_spec(name=name), {"path": value})

    def test_path_inside_allowed_root_is_unchanged(self):
        rule = self.make_rule()
        self.assertIsNone(self.evaluate(rule, str(self.base / "notes.txt")))

    def test_path_outside_allowed_root_escalates_to_high_risk(self):
        rule = self.make_rule()
        self.assertIs(
            self.evaluate(rule, str(self.other / "x.txt")),
            PermissionLevel.HIGH_RISK,
        )

    def test_path_inside_forbidden_root_is_blocked(self):
        rule = self.make_rule()
        self.assertIs(
            self.evaluate(rule, str(self.forbidden / "id_rsa")),
            PermissionLevel.BLOCKED,
        )

    def test_traversal_into_forbidden_root_is_blocked(self):
        rule = self.make_rule()
        traversal = str(self.base / "sub" / ".." / "secret" / "f")
        self.assertIs(self.evaluate(rule, traversal), PermissionLevel.BLOCKED)

    def test_outside_level_can_be_blocked(self):
        rule = self.make_rule(outside_level=PermissionLevel.BLOCKED)
        self.assertIs(
            self.evaluate(rule, str(self.other / "x.txt")),
            PermissionLevel.BLOCKED,
        )

    def test_symlink_into_forbidden_root_is_blocked(self):
        link = self.base / "link"
        try:
            os.symlink(
                str(self.forbidden), str(link), target_is_directory=True
            )
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available in this environment")
        self.assertIs(
            self.evaluate(rule=self.make_rule(), value=str(link / "file")),
            PermissionLevel.BLOCKED,
        )

    def test_other_tool_names_are_ignored(self):
        rule = self.make_rule()
        self.assertIsNone(
            rule.evaluate(make_spec(name="other"), {"path": "/etc/passwd"})
        )

    def test_missing_or_non_string_values_are_ignored(self):
        rule = self.make_rule()
        self.assertIsNone(rule.evaluate(make_spec(), {}))
        self.assertIsNone(rule.evaluate(make_spec(), {"path": 123}))
        self.assertIsNone(rule.evaluate(make_spec(), {"path": ""}))

    def test_no_allowed_roots_only_forbidden_applies(self):
        rule = self.make_rule(allowed_roots=[])
        self.assertIsNone(self.evaluate(rule, str(self.other / "x.txt")))
        self.assertIs(
            self.evaluate(rule, str(self.forbidden / "f")),
            PermissionLevel.BLOCKED,
        )

    def test_invalid_outside_level_rejected(self):
        with self.assertRaises(ValueError):
            self.make_rule(outside_level=PermissionLevel.SAFE)

    def test_policy_integration_escalates(self):
        policy = PermissionPolicy(escalations=[self.make_rule()])
        clean = policy.classify(
            make_spec(name="open_file"), {"path": str(self.base / "a.txt")}
        )
        self.assertIs(clean.level, PermissionLevel.SAFE)

        outside = policy.classify(
            make_spec(name="open_file"), {"path": str(self.other / "a.txt")}
        )
        self.assertIs(outside.level, PermissionLevel.HIGH_RISK)
        self.assertTrue(outside.requires_approval)

        forbidden = policy.classify(
            make_spec(name="open_file"),
            {"path": str(self.forbidden / "a.txt")},
        )
        self.assertIs(forbidden.level, PermissionLevel.BLOCKED)
        self.assertFalse(forbidden.requires_approval)


class RecorderInput:
    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []

    def __call__(self, prompt=""):
        self.prompts.append(prompt)
        if not self.answers:
            return ""
        return self.answers.pop(0)


class RaiserInput:
    def __init__(self, exc):
        self.exc = exc

    def __call__(self, prompt=""):
        raise self.exc


class TestConsoleApprovalProvider(unittest.TestCase):
    def approve_for(self, level, answer, tool="demo"):
        recorder = RecorderInput([answer])
        provider = ConsoleApprovalProvider(input_fn=recorder)
        result = provider.request(
            ApprovalRequest(tool_name=tool, summary="do a thing", level=level)
        )
        return result, recorder

    def test_safe_is_auto_approved_without_prompting(self):
        result, recorder = self.approve_for(PermissionLevel.SAFE, "")
        self.assertIs(result.outcome, ApprovalOutcome.APPROVED)
        self.assertEqual(recorder.prompts, [])

    def test_confirm_yes_is_approved(self):
        result, _ = self.approve_for(PermissionLevel.CONFIRM, "y")
        self.assertIs(result.outcome, ApprovalOutcome.APPROVED)

    def test_confirm_yes_word_is_approved(self):
        result, _ = self.approve_for(PermissionLevel.CONFIRM, "YES")
        self.assertIs(result.outcome, ApprovalOutcome.APPROVED)

    def test_confirm_default_is_denied(self):
        result, _ = self.approve_for(PermissionLevel.CONFIRM, "")
        self.assertIs(result.outcome, ApprovalOutcome.DENIED)

    def test_confirm_other_text_is_denied(self):
        result, _ = self.approve_for(PermissionLevel.CONFIRM, "maybe")
        self.assertIs(result.outcome, ApprovalOutcome.DENIED)

    def test_high_risk_requires_exact_tool_name(self):
        result, _ = self.approve_for(PermissionLevel.HIGH_RISK, "demo")
        self.assertIs(result.outcome, ApprovalOutcome.APPROVED)

    def test_high_risk_wrong_name_is_denied(self):
        result, _ = self.approve_for(PermissionLevel.HIGH_RISK, "demoo")
        self.assertIs(result.outcome, ApprovalOutcome.DENIED)

    def test_high_risk_empty_is_denied(self):
        result, _ = self.approve_for(PermissionLevel.HIGH_RISK, "")
        self.assertIs(result.outcome, ApprovalOutcome.DENIED)

    def test_blocked_is_denied(self):
        result, _ = self.approve_for(PermissionLevel.BLOCKED, "y")
        self.assertIs(result.outcome, ApprovalOutcome.DENIED)

    def test_eof_fails_closed(self):
        provider = ConsoleApprovalProvider(input_fn=RaiserInput(EOFError()))
        result = provider.request(
            ApprovalRequest("demo", "thing", PermissionLevel.CONFIRM)
        )
        self.assertIs(result.outcome, ApprovalOutcome.UNAVAILABLE)

    def test_keyboard_interrupt_fails_closed(self):
        provider = ConsoleApprovalProvider(
            input_fn=RaiserInput(KeyboardInterrupt())
        )
        result = provider.request(
            ApprovalRequest("demo", "thing", PermissionLevel.HIGH_RISK)
        )
        self.assertIs(result.outcome, ApprovalOutcome.UNAVAILABLE)

    def test_unexpected_error_fails_closed(self):
        provider = ConsoleApprovalProvider(
            input_fn=RaiserInput(OSError("stdin gone"))
        )
        result = provider.request(
            ApprovalRequest("demo", "thing", PermissionLevel.CONFIRM)
        )
        self.assertIs(result.outcome, ApprovalOutcome.UNAVAILABLE)

    def test_prompt_does_not_include_raw_arguments(self):
        result, recorder = self.approve_for(PermissionLevel.CONFIRM, "n")
        # The provider only ever sees the developer-authored summary.
        self.assertNotIn("path", " ".join(recorder.prompts))
        self.assertIs(result.outcome, ApprovalOutcome.DENIED)

    def test_approval_echoes_the_request_token(self):
        # A granted approval must be bound to the exact invocation by
        # echoing the confirmation token back to the executor.
        recorder = RecorderInput(["y"])
        provider = ConsoleApprovalProvider(input_fn=recorder)
        request = ApprovalRequest(
            tool_name="demo",
            summary="do a thing",
            level=PermissionLevel.CONFIRM,
            token="ticket-123",
        )
        result = provider.request(request)
        self.assertIs(result.outcome, ApprovalOutcome.APPROVED)
        self.assertEqual(result.token, "ticket-123")

    def test_high_risk_approval_echoes_the_request_token(self):
        recorder = RecorderInput(["demo"])
        provider = ConsoleApprovalProvider(input_fn=recorder)
        request = ApprovalRequest(
            tool_name="demo",
            summary="do a thing",
            level=PermissionLevel.HIGH_RISK,
            token="ticket-456",
        )
        result = provider.request(request)
        self.assertIs(result.outcome, ApprovalOutcome.APPROVED)
        self.assertEqual(result.token, "ticket-456")


if __name__ == "__main__":
    unittest.main()
