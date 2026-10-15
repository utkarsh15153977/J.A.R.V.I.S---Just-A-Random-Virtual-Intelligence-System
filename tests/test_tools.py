"""Unit tests for ToolSpec, the registry and argument validation."""

import unittest

from core.permissions import PermissionLevel
from core.tools import (
    ToolInputError,
    ToolRegistry,
    ToolSpec,
    validate_mapping,
)


def noop_handler(**kwargs):
    return kwargs


def passthrough_validate(raw):
    return dict(raw)


def make_spec(name="demo", permission=PermissionLevel.SAFE, **overrides):
    params = dict(
        name=name,
        description="demo tool",
        permission=permission,
        validate=passthrough_validate,
        handler=noop_handler,
    )
    params.update(overrides)
    return ToolSpec(**params)


class TestToolSpecValidation(unittest.TestCase):
    def test_valid_spec_constructs(self):
        spec = make_spec()
        self.assertEqual(spec.name, "demo")
        self.assertIs(spec.permission, PermissionLevel.SAFE)
        self.assertIsNone(spec.timeout)

    def test_empty_name_rejected(self):
        with self.assertRaises(ValueError):
            make_spec(name="")

    def test_whitespace_name_rejected(self):
        with self.assertRaises(ValueError):
            make_spec(name="   ")

    def test_non_callable_validator_rejected(self):
        with self.assertRaises(TypeError):
            make_spec(validate="not callable")

    def test_non_callable_handler_rejected(self):
        with self.assertRaises(TypeError):
            make_spec(handler=None)

    def test_non_permission_level_rejected(self):
        with self.assertRaises(TypeError):
            make_spec(permission="safe")

    def test_non_positive_timeout_rejected(self):
        with self.assertRaises(ValueError):
            make_spec(timeout=0)
        with self.assertRaises(ValueError):
            make_spec(timeout=-1)

    def test_positive_timeout_accepted(self):
        self.assertEqual(make_spec(timeout=2.5).timeout, 2.5)


class TestToolRegistry(unittest.TestCase):
    def setUp(self):
        self.registry = ToolRegistry()

    def test_register_and_get(self):
        spec = make_spec(name="alpha")
        self.registry.register(spec)
        self.assertIs(self.registry.get("alpha"), spec)

    def test_get_unknown_returns_none(self):
        self.assertIsNone(self.registry.get("nope"))

    def test_duplicate_registration_rejected(self):
        self.registry.register(make_spec(name="alpha"))
        with self.assertRaises(ValueError):
            self.registry.register(make_spec(name="alpha"))

    def test_non_spec_registration_rejected(self):
        with self.assertRaises(TypeError):
            self.registry.register("nope")

    def test_names_are_sorted(self):
        self.registry.register(make_spec(name="beta"))
        self.registry.register(make_spec(name="alpha"))
        self.assertEqual(self.registry.names(), ("alpha", "beta"))

    def test_contains_and_len(self):
        self.registry.register(make_spec(name="alpha"))
        self.assertIn("alpha", self.registry)
        self.assertNotIn("beta", self.registry)
        self.assertEqual(len(self.registry), 1)


class TestValidateMapping(unittest.TestCase):
    def test_required_present_returns_value(self):
        result = validate_mapping({"path": "a.txt"}, required={"path": str})
        self.assertEqual(result, {"path": "a.txt"})

    def test_missing_required_raises(self):
        with self.assertRaises(ToolInputError):
            validate_mapping({}, required={"path": str})

    def test_wrong_type_raises(self):
        with self.assertRaises(ToolInputError):
            validate_mapping({"count": "5"}, required={"count": int})

    def test_unknown_key_raises(self):
        with self.assertRaises(ToolInputError):
            validate_mapping({"path": "a", "extra": 1}, required={"path": str})

    def test_optional_absent_is_omitted(self):
        result = validate_mapping(
            {"path": "a"}, required={"path": str}, optional={"flag": bool}
        )
        self.assertEqual(result, {"path": "a"})

    def test_optional_present_is_included(self):
        result = validate_mapping(
            {"path": "a", "flag": True},
            required={"path": str},
            optional={"flag": bool},
        )
        self.assertEqual(result, {"path": "a", "flag": True})

    def test_result_contains_only_declared_keys(self):
        result = validate_mapping(
            {"path": "a", "flag": False},
            required={"path": str},
            optional={"flag": bool},
        )
        self.assertEqual(set(result), {"path", "flag"})

    def test_tuple_types_accepted(self):
        result = validate_mapping(
            {"timeout": 1.5}, required={"timeout": (int, float)}
        )
        self.assertEqual(result, {"timeout": 1.5})

    def test_non_mapping_raises(self):
        with self.assertRaises(ToolInputError):
            validate_mapping(["not", "a", "mapping"], required={})

    def test_field_both_required_and_optional_raises(self):
        with self.assertRaises(ValueError):
            validate_mapping(
                {"path": "a"}, required={"path": str}, optional={"path": str}
            )

    def test_error_message_does_not_echo_values(self):
        secret = "SUPER-SECRET-VALUE"
        with self.assertRaises(ToolInputError) as ctx:
            validate_mapping({"count": secret}, required={"count": int})
        self.assertNotIn(secret, str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
