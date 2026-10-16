"""Unit tests for command routing (pure logic, no execution)."""

import dataclasses
import unittest

from core.app_launcher import APP_LAUNCH_INTENTS, TOOL_LAUNCH_APPLICATION
from core.memory import (
    TOOL_MEMORY_CLEAR,
    TOOL_MEMORY_DELETE,
    TOOL_MEMORY_GET,
    TOOL_MEMORY_LIST,
    TOOL_MEMORY_SAVE,
)
from core.research import TOOL_WEB_RESEARCH
from core.router import (
    DEFAULT_TOOL_INTENTS,
    CommandKind,
    CommandRequest,
    IntentRule,
    PlaceholderRouter,
    ToolCommandRouter,
    _research_query,
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
        # matching is exact, so unrelated speech cannot reach a tool. The
        # only deliberate exception is a *leading* filler or vocative,
        # which is stripped before matching (see
        # TestSpeechQueryNormalization); extra words elsewhere still do
        # not match.
        for text in (
            "what time is it in paris",
            "please tell me the time now",
            "i want to know what is the date",
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

    def test_fixed_rule_arguments_are_carried_into_the_request(self):
        router = ToolCommandRouter(
            (
                IntentRule(
                    "demo",
                    frozenset({"do the thing"}),
                    {"mode": "a"},
                ),
            ),
            available_tools=("demo",),
        )
        command = router.route("do the thing")
        self.assertIs(command.kind, CommandKind.TOOL)
        self.assertEqual(dict(command.arguments), {"mode": "a"})

    def test_spoken_text_is_never_copied_into_fixed_arguments(self):
        router = ToolCommandRouter(
            (
                IntentRule(
                    "demo",
                    frozenset({"do the thing"}),
                    {"key": "value"},
                ),
            ),
            available_tools=("demo",),
        )
        # Extra spoken words do not route at all (exact match only)...
        spoken = router.route("do the thing with secret hunter2")
        self.assertIs(spoken.kind, CommandKind.UNSUPPORTED)
        # ...and a matching phrase carries only the fixed arguments.
        command = router.route("do the thing")
        self.assertEqual(dict(command.arguments), {"key": "value"})
        self.assertEqual(command.text, "")

    def test_rule_arguments_are_copied_not_shared(self):
        router = ToolCommandRouter(
            (
                IntentRule(
                    "demo",
                    frozenset({"go"}),
                    {"key": "value"},
                ),
            ),
            available_tools=("demo",),
        )
        first = router.route("go")
        dict(first.arguments)["key"] = "changed"
        second = router.route("go")
        self.assertEqual(dict(second.arguments), {"key": "value"})


class TestAppLaunchIntents(unittest.TestCase):
    def _router(self):
        return ToolCommandRouter(
            APP_LAUNCH_INTENTS,
            available_tools=(TOOL_LAUNCH_APPLICATION,),
        )

    def test_phrases_route_to_fixed_identifiers(self):
        router = self._router()
        for phrase in ("open notepad", "launch notepad", "start notepad"):
            with self.subTest(phrase=phrase):
                command = router.route(phrase)
                self.assertIs(command.kind, CommandKind.TOOL)
                self.assertEqual(command.tool_name, TOOL_LAUNCH_APPLICATION)
                self.assertEqual(dict(command.arguments), {"app": "notepad"})
        for phrase in ("open calculator", "launch calculator"):
            with self.subTest(phrase=phrase):
                command = router.route(phrase)
                self.assertEqual(
                    dict(command.arguments), {"app": "calculator"}
                )

    def test_launch_phrases_are_inert_without_the_tool(self):
        router = ToolCommandRouter(APP_LAUNCH_INTENTS, available_tools=())
        self.assertIs(
            router.route("open notepad").kind, CommandKind.UNSUPPORTED
        )

    def test_arbitrary_application_names_do_not_route(self):
        router = self._router()
        for spoken in (
            "open minesweeper",
            "launch notepad with admin rights",
            "start cmd.exe",
        ):
            with self.subTest(spoken=spoken):
                self.assertIs(
                    router.route(spoken).kind, CommandKind.UNSUPPORTED
                )



class TestResearchFallback(unittest.TestCase):
    def _router(self, **kwargs):
        options = {
            "available_tools": (TOOL_WEB_RESEARCH, "get_current_time"),
            "research_tool": TOOL_WEB_RESEARCH,
        }
        options.update(kwargs)
        return ToolCommandRouter(DEFAULT_TOOL_INTENTS, **options)

    def test_eligible_questions_route_with_the_spoken_query(self):
        router = self._router()
        for text in (
            "What is the distance between Ranchi and Dhanbad?",
            "why is the sky blue",
            "who directed the movie Dhurandhar",
            "tell me about the eiffel tower",
        ):
            with self.subTest(text=text):
                command = router.route(text)
                self.assertIs(command.kind, CommandKind.TOOL)
                self.assertEqual(command.tool_name, TOOL_WEB_RESEARCH)
                self.assertEqual(dict(command.arguments), {"query": text})
                self.assertEqual(command.text, "")

    def test_fixed_intents_win_over_research(self):
        command = self._router().route("what time is it")
        self.assertEqual(command.tool_name, "get_current_time")
        self.assertEqual(dict(command.arguments), {})

    def test_action_requests_stay_unsupported(self):
        router = self._router()
        for text in (
            "open google chrome",
            "delete this file",
            "run this command",
            "can you open notepad",
            "could you please launch calculator",
            "please delete it",
        ):
            with self.subTest(text=text):
                self.assertIs(
                    router.route(text).kind, CommandKind.UNSUPPORTED
                )

    def test_non_questions_stay_unsupported(self):
        router = self._router()
        for text in ("the weather is nice", "hello there", "why"):
            with self.subTest(text=text):
                self.assertIs(
                    router.route(text).kind, CommandKind.UNSUPPORTED
                )

    def test_query_is_bounded(self):
        command = self._router(research_max_query_chars=10).route(
            "what is the distance between two very distant places"
        )
        self.assertIs(command.kind, CommandKind.TOOL)
        self.assertEqual(len(dict(command.arguments)["query"]), 10)

    def test_fallback_is_inert_without_a_research_tool(self):
        router = self._router(research_tool=None)
        self.assertIs(
            router.route("what is the distance").kind,
            CommandKind.UNSUPPORTED,
        )

    def test_fallback_is_inert_when_the_tool_is_not_registered(self):
        router = self._router(available_tools=("get_current_time",))
        self.assertIs(
            router.route("what is the distance").kind,
            CommandKind.UNSUPPORTED,
        )

    def test_research_query_predicate(self):
        for text in ("what is a", "is it going to rain", "who won"):
            with self.subTest(text=text):
                self.assertIsNotNone(_research_query(text))
        for text in ("", "why", "open chrome", "delete this file"):
            with self.subTest(text=text):
                self.assertIsNone(_research_query(text))


class TestResearchRequestBoundary(unittest.TestCase):
    """Batch 18: keep informational questions and block action requests."""

    def _router(self, **kwargs):
        options = {
            "available_tools": (TOOL_WEB_RESEARCH, "get_current_time"),
            "research_tool": TOOL_WEB_RESEARCH,
        }
        options.update(kwargs)
        return ToolCommandRouter(DEFAULT_TOOL_INTENTS, **options)

    def test_modal_request_with_ability_filler_is_unsupported(self):
        # An action request phrased with "able to" must not reach research.
        self.assertIs(
            self._router().route("are you able to open the file").kind,
            CommandKind.UNSUPPORTED,
        )

    def test_politeness_and_filler_do_not_open_a_back_door(self):
        for text in (
            "could you kindly open notepad",
            "would you please delete this file",
            "can i just launch the calculator",
        ):
            with self.subTest(text=text):
                self.assertIs(
                    self._router().route(text).kind, CommandKind.UNSUPPORTED
                )

    def test_wh_how_to_question_still_routes(self):
        # A "how to" question is informational, not an imperative.
        command = self._router().route("how to open a bank account")
        self.assertIs(command.kind, CommandKind.TOOL)
        self.assertEqual(command.tool_name, TOOL_WEB_RESEARCH)

    def test_informational_question_about_an_action_word_routes(self):
        command = self._router().route("why should I open notepad")
        self.assertEqual(command.tool_name, TOOL_WEB_RESEARCH)
        self.assertEqual(
            dict(command.arguments)["query"], "why should I open notepad"
        )

    def test_modal_question_about_a_subject_still_routes(self):
        command = self._router().route("does the library open at nine")
        self.assertIs(command.kind, CommandKind.TOOL)
        self.assertEqual(command.tool_name, TOOL_WEB_RESEARCH)

    def test_fixed_intent_still_wins(self):
        command = self._router().route("can you tell me the time")
        self.assertEqual(command.tool_name, "get_current_time")


class TestSpeechQueryNormalization(unittest.TestCase):
    """Batch 18.1: leading conversational noise does not hide a command.

    A person (and Whisper) often precede a question with "and Jarvis," or
    just "Jarvis,". Only a fixed, leading set of fillers is dropped, and
    only for classification: the original utterance is still what a
    provider receives, and an action request can never become a question.
    """

    def _router(self, **kwargs):
        options = {
            "available_tools": (TOOL_WEB_RESEARCH, "get_current_time"),
            "research_tool": TOOL_WEB_RESEARCH,
        }
        options.update(kwargs)
        return ToolCommandRouter(DEFAULT_TOOL_INTENTS, **options)

    def test_leading_and_vocative_question_routes_to_research(self):
        command = self._router().route(
            "And Jarvis, who is the president of U.S.A.?"
        )
        self.assertIs(command.kind, CommandKind.TOOL)
        self.assertEqual(command.tool_name, TOOL_WEB_RESEARCH)

    def test_leading_vocative_alone_routes(self):
        command = self._router().route(
            "Jarvis, who is the president of the USA?"
        )
        self.assertIs(command.kind, CommandKind.TOOL)
        self.assertEqual(command.tool_name, TOOL_WEB_RESEARCH)

    def test_acronym_punctuation_does_not_change_routing(self):
        router = self._router()
        for text in (
            "who is the president of the U.S.A.",
            "who is the president of the USA",
            "And Jarvis, who is the president of U.S.A.?",
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    router.route(text).tool_name, TOOL_WEB_RESEARCH
                )

    def test_original_utterance_is_still_the_query(self):
        text = "And Jarvis, who is the president of the U.S.A.?"
        command = self._router().route(text)
        # Classification is de-noised; the query that is sent keeps the
        # user's own words (nothing about the provider call changes).
        self.assertEqual(dict(command.arguments)["query"], text)

    def test_lead_in_before_a_builtin_intent_still_matches(self):
        command = self._router().route("Jarvis, what time is it")
        self.assertIs(command.kind, CommandKind.TOOL)
        self.assertEqual(command.tool_name, "get_current_time")
        self.assertEqual(dict(command.arguments), {})

    def test_stacked_lead_ins_are_all_dropped(self):
        command = self._router().route("so well um jarvis who won the game")
        self.assertIs(command.kind, CommandKind.TOOL)
        self.assertEqual(command.tool_name, TOOL_WEB_RESEARCH)

    def test_lead_ins_never_turn_a_request_into_a_question(self):
        router = self._router()
        for text in (
            "Jarvis, delete my files",
            "and open the file",
            "so run the command",
            "um remove the file",
            "okay launch the calculator",
            "Jarvis, are you able to open the file?",
            "And Jarvis, could you kindly delete this file",
        ):
            with self.subTest(text=text):
                self.assertIs(
                    router.route(text).kind, CommandKind.UNSUPPORTED
                )

    def test_pure_lead_in_is_not_a_command(self):
        router = self._router()
        for text in ("jarvis", "and", "hey jarvis", "ok so"):
            with self.subTest(text=text):
                command = router.route(text)
                self.assertIs(command.kind, CommandKind.UNSUPPORTED)

    def test_lead_in_does_not_route_without_a_research_tool(self):
        router = self._router(research_tool=None)
        self.assertIs(
            router.route("Jarvis, who is the president of the USA?").kind,
            CommandKind.UNSUPPORTED,
        )


class TestMemoryRouting(unittest.TestCase):
    """Batch 19: explicit memory phrases route only to memory tools.

    The router names a registered memory tool and never executes it. Saving
    needs a whole-word trigger, so unrelated text is never stored, and with
    ``memory_tools`` unset every memory phrase stays unsupported.
    """

    def _router(self, **kwargs):
        tools = {
            "save": TOOL_MEMORY_SAVE,
            "list": TOOL_MEMORY_LIST,
            "get": TOOL_MEMORY_GET,
            "delete": TOOL_MEMORY_DELETE,
            "clear": TOOL_MEMORY_CLEAR,
        }
        options = {
            "available_tools": (
                *tools.values(),
                "get_current_time",
                TOOL_WEB_RESEARCH,
            ),
            "memory_tools": tools,
        }
        options.update(kwargs)
        return ToolCommandRouter(DEFAULT_TOOL_INTENTS, **options)

    def test_save_trigger_preserves_value_and_case(self):
        command = self._router().route("Remember that I love Python")
        self.assertIs(command.kind, CommandKind.TOOL)
        self.assertEqual(command.tool_name, TOOL_MEMORY_SAVE)
        self.assertEqual(dict(command.arguments), {"value": "I love Python"})

    def test_polite_save_trigger(self):
        command = self._router().route(
            "Please remember that the meeting is on Friday"
        )
        self.assertEqual(command.tool_name, TOOL_MEMORY_SAVE)
        self.assertEqual(
            dict(command.arguments)["value"], "the meeting is on Friday"
        )

    def test_leading_filler_is_skipped_for_save(self):
        command = self._router().route("and jarvis remember that I like tea")
        self.assertEqual(command.tool_name, TOOL_MEMORY_SAVE)
        self.assertEqual(dict(command.arguments)["value"], "I like tea")

    def test_note_that_is_a_save_trigger(self):
        command = self._router().route("Note that the code is blue")
        self.assertEqual(command.tool_name, TOOL_MEMORY_SAVE)

    def test_bare_remember_is_not_a_save(self):
        router = self._router()
        for text in (
            "remember to call mom",
            "remember that",
            "I prefer concise answers",
            "how can I remember more effectively?",
            "what should I remember for the exam",
        ):
            with self.subTest(text=text):
                self.assertIs(
                    router.route(text).kind, CommandKind.UNSUPPORTED
                )

    def test_list_phrases(self):
        router = self._router()
        for text in (
            "list my memories",
            "list my saved memories",
            "show my memories",
            "what do you remember",
            "what do you remember about me",
            "what have you remembered",
        ):
            with self.subTest(text=text):
                command = router.route(text)
                self.assertIs(command.kind, CommandKind.TOOL)
                self.assertEqual(command.tool_name, TOOL_MEMORY_LIST)
                self.assertEqual(dict(command.arguments), {})

    def test_list_prefix_with_a_subject(self):
        command = self._router().route("what do you remember about my editor")
        self.assertEqual(command.tool_name, TOOL_MEMORY_LIST)

    def test_get_phrase_carries_a_selector(self):
        command = self._router().route("recall memory a1b2c3d4")
        self.assertIs(command.kind, CommandKind.TOOL)
        self.assertEqual(command.tool_name, TOOL_MEMORY_GET)
        self.assertEqual(dict(command.arguments), {"selector": "a1b2c3d4"})

    def test_get_without_a_selector_is_unsupported(self):
        self.assertIs(
            self._router().route("what is memory").kind,
            CommandKind.UNSUPPORTED,
        )

    def test_delete_phrase_carries_a_selector(self):
        command = self._router().route("forget memory a1b2c3d4")
        self.assertEqual(command.tool_name, TOOL_MEMORY_DELETE)
        self.assertEqual(dict(command.arguments), {"selector": "a1b2c3d4"})

    def test_delete_by_value(self):
        command = self._router().route("delete the memory about my editor")
        self.assertEqual(command.tool_name, TOOL_MEMORY_DELETE)
        self.assertEqual(dict(command.arguments), {"selector": "my editor"})

    def test_clear_phrases(self):
        router = self._router()
        for text in (
            "clear all memories",
            "clear all my saved memories",
            "forget everything",
            "delete all memories",
        ):
            with self.subTest(text=text):
                command = router.route(text)
                self.assertEqual(command.tool_name, TOOL_MEMORY_CLEAR)
                self.assertEqual(dict(command.arguments), {})

    def test_action_requests_are_not_treated_as_clear_or_delete(self):
        router = self._router()
        for text in ("delete all files", "remove the file", "open notepad"):
            with self.subTest(text=text):
                self.assertIs(
                    router.route(text).kind, CommandKind.UNSUPPORTED
                )

    def test_memory_disabled_leaves_phrases_unsupported(self):
        router = self._router(memory_tools=None)
        for text in (
            "remember that I like tea",
            "list my memories",
            "forget memory a1b2c3d4",
            "clear all memories",
        ):
            with self.subTest(text=text):
                self.assertIs(
                    router.route(text).kind, CommandKind.UNSUPPORTED
                )

    def test_unregistered_memory_tool_is_ignored(self):
        router = self._router(
            available_tools=("get_current_time",),
            memory_tools={"save": TOOL_MEMORY_SAVE},
        )
        self.assertIs(
            router.route("remember that I like tea").kind,
            CommandKind.UNSUPPORTED,
        )

    def test_memory_wins_over_research(self):
        router = self._router(research_tool=TOOL_WEB_RESEARCH)
        command = router.route("remember that I like tea")
        self.assertEqual(command.tool_name, TOOL_MEMORY_SAVE)
        # A list prefix is a memory request, not a research question.
        command = router.route("what do you remember about the war")
        self.assertEqual(command.tool_name, TOOL_MEMORY_LIST)

    def test_research_still_works_alongside_memory(self):
        router = self._router(research_tool=TOOL_WEB_RESEARCH)
        command = router.route("who is the president of the USA")
        self.assertEqual(command.tool_name, TOOL_WEB_RESEARCH)


class TestCommandRequest(unittest.TestCase):
    def test_defaults(self):
        command = CommandRequest(CommandKind.EXIT)
        self.assertEqual(command.text, "")
        self.assertEqual(command.tool_name, "")
        self.assertEqual(dict(command.arguments), {})
        self.assertEqual(command.response, "")

    def test_is_frozen(self):
        command = CommandRequest(CommandKind.EXIT)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            command.kind = CommandKind.TOOL

    def test_tool_request_carries_name_and_arguments(self):
        command = CommandRequest(
            CommandKind.TOOL, tool_name="demo", arguments={"a": 1}
        )
        self.assertEqual(command.tool_name, "demo")
        self.assertEqual(command.arguments, {"a": 1})


if __name__ == "__main__":
    unittest.main()
