"""Unit tests for command routing (pure logic, no execution)."""

import unittest

from core.router import (
    CommandKind,
    CommandRequest,
    IntentRule,
    PlaceholderRouter,
    ToolCommandRouter,
)


class TestPlaceholderRouterExit(unittest.TestCase):
    def setUp(self):
        self.router = PlaceholderRouter()

    def test_exit_words_are_recognised(self):
        for word in ("exit", "quit", "stop", "goodbye", "shutdown"):
            with self.subTest(word=word):
                self.assertIs(
                    self.router.route(word).kind, CommandKind.EXIT
                )

    def test_exit_words_are_case_and_space_insensitive(self):
        self.assertIs(
            self.router.route("  QUIT  ").kind, CommandKind.EXIT
        )

    def test_multi_word_exit_phrase(self):
        self.assertIs(
            self.router.route("shut down").kind, CommandKind.EXIT
        )

    def test_exit_words_tolerate_punctuation(self):
        for text in ("stop.", "quit!", "shut down.", "Stop?", "goodbye,"):
            with self.subTest(text=text):
                self.assertIs(
                    self.router.route(text).kind, CommandKind.EXIT
                )

    def test_exit_phrase_tolerates_extra_whitespace(self):
        self.assertIs(
            self.router.route("shut   down").kind, CommandKind.EXIT
        )


class TestPlaceholderRouterClarifyAndUnsupported(unittest.TestCase):
    def setUp(self):
        self.router = PlaceholderRouter()

    def test_empty_text_asks_for_clarification(self):
        command = self.router.route("")
        self.assertIs(command.kind, CommandKind.CLARIFY)
        self.assertTrue(command.response)

    def test_whitespace_only_asks_for_clarification(self):
        self.assertIs(
            self.router.route("   ").kind, CommandKind.CLARIFY
        )

    def test_none_is_treated_as_empty(self):
        self.assertIs(self.router.route(None).kind, CommandKind.CLARIFY)

    def test_unknown_text_is_unsupported(self):
        command = self.router.route("open youtube")
        self.assertIs(command.kind, CommandKind.UNSUPPORTED)
        self.assertTrue(command.response)

    def test_unsupported_response_does_not_echo_input(self):
        command = self.router.route("my secret password is hunter2")
        self.assertNotIn("hunter2", command.response)
        self.assertNotIn("secret", command.response)

    def test_router_never_produces_a_tool_request(self):
        for text in ("", "hello", "delete everything", "exit"):
            with self.subTest(text=text):
                command = self.router.route(text)
                self.assertIsNot(command.kind, CommandKind.TOOL)

    def test_custom_exit_words(self):
        router = PlaceholderRouter(exit_words=frozenset({"farewell"}))
        self.assertIs(router.route("farewell").kind, CommandKind.EXIT)
        self.assertIs(router.route("exit").kind, CommandKind.UNSUPPORTED)

    def test_custom_responses_are_used(self):
        router = PlaceholderRouter(
            clarify_response="CLARIFY", unsupported_response="NOPE"
        )
        self.assertEqual(router.route("").response, "CLARIFY")
        self.assertEqual(router.route("x").response, "NOPE")


class TestToolCommandRouter(unittest.TestCase):
    def setUp(self):
        self.rules = (
            IntentRule(
                tool_name="get_current_time",
                phrases=frozenset(
                    {"what time is it", "tell me the time"}
                ),
            ),
            IntentRule(
                tool_name="get_current_date",
                phrases=frozenset({"what is the date"}),
            ),
        )
        self.router = ToolCommandRouter(
            self.rules,
            available_tools=("get_current_time", "get_current_date"),
        )

    def test_known_phrase_routes_to_registered_tool(self):
        command = self.router.route("what time is it")
        self.assertIs(command.kind, CommandKind.TOOL)
        self.assertEqual(command.tool_name, "get_current_time")
        self.assertEqual(dict(command.arguments), {})

    def test_second_phrase_for_same_tool(self):
        command = self.router.route("Tell me the time")
        self.assertIs(command.kind, CommandKind.TOOL)
        self.assertEqual(command.tool_name, "get_current_time")

    def test_matching_is_case_punctuation_and_apostrophe_insensitive(self):
        for text in (
            "WHAT TIME IS IT?",
            "  what time is it  ",
            "what time is it.",
        ):
            with self.subTest(text=text):
                self.assertIs(
                    self.router.route(text).kind, CommandKind.TOOL
                )

    def test_apostrophes_are_ignored(self):
        router = ToolCommandRouter(
            (
                IntentRule(
                    tool_name="t",
                    phrases=frozenset({"what's the date"}),
                ),
            ),
            available_tools=("t",),
        )
        variants = (
            "what's the date",
            "whats the date",
            "what\u2019s the date",
        )
        for text in variants:
            with self.subTest(text=text):
                self.assertIs(router.route(text).kind, CommandKind.TOOL)

    def test_unregistered_tool_is_not_matched(self):
        router = ToolCommandRouter(
            self.rules, available_tools=("get_current_date",)
        )
        self.assertIs(
            router.route("what time is it").kind, CommandKind.UNSUPPORTED
        )
        self.assertIs(
            router.route("what is the date").kind, CommandKind.TOOL
        )

    def test_empty_registry_behaves_like_the_placeholder(self):
        router = ToolCommandRouter(self.rules, available_tools=())
        self.assertIs(router.route("what time is it").kind,
                      CommandKind.UNSUPPORTED)

    def test_unknown_phrase_is_unsupported(self):
        self.assertIs(
            self.router.route("open youtube").kind, CommandKind.UNSUPPORTED
        )

    def test_superset_phrases_do_not_invoke_a_tool(self):
        # A phrase that merely *contains* a known phrase must not match:
        # matching is exact, so unrelated speech cannot reach a tool.
        for text in (
            "what time is it in paris",
            "please tell me the time now",
            "hey jarvis what is the date",
        ):
            with self.subTest(text=text):
                self.assertIs(
                    self.router.route(text).kind, CommandKind.UNSUPPORTED
                )

    def test_empty_text_asks_for_clarification(self):
        self.assertIs(self.router.route("").kind, CommandKind.CLARIFY)

    def test_punctuation_only_is_treated_as_empty(self):
        self.assertIs(self.router.route("?!!").kind, CommandKind.CLARIFY)

    def test_exit_words_still_win(self):
        self.assertIs(self.router.route("stop").kind, CommandKind.EXIT)

    def test_router_never_copies_spoken_text_into_arguments(self):
        command = self.router.route("What time is it?")
        self.assertIs(command.kind, CommandKind.TOOL)
        self.assertEqual(dict(command.arguments), {})
        self.assertEqual(command.text, "")

    def test_unsupported_response_does_not_echo_input(self):
        command = self.router.route("my password is hunter2")
        self.assertNotIn("hunter2", command.response)

    def test_custom_responses_are_used(self):
        router = ToolCommandRouter(
            self.rules,
            available_tools=("get_current_time",),
            clarify_response="CLARIFY",
            unsupported_response="NOPE",
        )
        self.assertEqual(router.route("").response, "CLARIFY")
        self.assertEqual(router.route("open youtube").response, "NOPE")

    def test_does_not_route_phrases_for_an_unrelated_tool(self):
        router = ToolCommandRouter(
            (
                IntentRule("launch_missiles", frozenset({"fire"})),
                IntentRule("get_current_time", frozenset({"time"})),
            ),
            available_tools=("get_current_time",),
        )
        self.assertIs(router.route("fire").kind, CommandKind.UNSUPPORTED)
        self.assertIs(router.route("time").kind, CommandKind.TOOL)


class TestCommandRequest(unittest.TestCase):
    def test_defaults(self):
        command = CommandRequest(CommandKind.EXIT)
        self.assertEqual(command.text, "")
        self.assertEqual(command.tool_name, "")
        self.assertEqual(dict(command.arguments), {})
        self.assertEqual(command.response, "")

    def test_is_frozen(self):
        command = CommandRequest(CommandKind.EXIT)
        with self.assertRaises(Exception):
            command.kind = CommandKind.TOOL

    def test_tool_request_carries_name_and_arguments(self):
        command = CommandRequest(
            CommandKind.TOOL, tool_name="demo", arguments={"a": 1}
        )
        self.assertEqual(command.tool_name, "demo")
        self.assertEqual(command.arguments, {"a": 1})


if __name__ == "__main__":
    unittest.main()
