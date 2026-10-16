"""Batch 15: end-to-end voice-pipeline integration checks.

These drive the whole loop through the composition root with injected
fakes (listener, speaker, router, executor, state machine). They assert
*behaviour* the user depends on operationally:

* a spoken command executes exactly once and never twice per activation;
* a router failure is recovered from and the loop keeps listening;
* an empty transcript reaches no executor and the loop keeps listening;
* repeated activations stay isolated (one execution each);
* an unexpected transcription error shuts down cleanly with every
  resource released and the state machine stopped.

No microphone, speaker, model file, network or external process is used.
"""

import unittest

from app import EXIT_OK, EXIT_RUNTIME_ERROR
from core.router import CommandKind, CommandRequest
from tests.test_app import (
    FakeListener,
    RecordingExecutor,
    ScriptedRouter,
    run_main,
)
from voice.states import ListeningState, ListeningStateMachine


class TestCommandExecution(unittest.TestCase):
    def test_command_executes_exactly_once_per_activation(self):
        router = ScriptedRouter(
            {
                "time": CommandRequest(
                    CommandKind.TOOL, tool_name="clock", arguments={}
                ),
                "date": CommandRequest(
                    CommandKind.TOOL, tool_name="calendar", arguments={}
                ),
                "exit": CommandRequest(CommandKind.EXIT),
            }
        )
        executor = RecordingExecutor()
        listener = FakeListener(texts=["time", "date", "exit"])
        code, _, _, _ = run_main(
            listener_factory=lambda config: listener,
            router=router,
            executor=executor,
        )
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(
            executor.calls,
            [("clock", {}), ("calendar", {})],
        )

    def test_no_duplicate_execution_on_repeated_activation(self):
        machine = ListeningStateMachine()
        router = ScriptedRouter(
            {
                "time": CommandRequest(
                    CommandKind.TOOL, tool_name="clock", arguments={}
                ),
                "exit": CommandRequest(CommandKind.EXIT),
            }
        )
        executor = RecordingExecutor()
        listener = FakeListener(texts=["time", "time", "exit"])
        code, _, _, _ = run_main(
            listener_factory=lambda config: listener,
            router=router,
            executor=executor,
            state_machine=machine,
        )
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(len(executor.calls), 2)
        self.assertTrue(
            all(call == ("clock", {}) for call in executor.calls)
        )
        self.assertIs(machine.state, ListeningState.STOPPED)


class ExplodingOnceRouter:
    """Raises on the first ``route`` call, then delegates."""

    def __init__(self, delegate):
        self.delegate = delegate
        self.calls = 0

    def route(self, text):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("router exploded")
        return self.delegate.route(text)


class TestPipelineRecovery(unittest.TestCase):
    def test_empty_transcript_reaches_no_executor(self):
        machine = ListeningStateMachine()
        router = ScriptedRouter(
            {
                "": CommandRequest(
                    CommandKind.CLARIFY, response="Say it again?"
                ),
                "exit": CommandRequest(CommandKind.EXIT),
            }
        )
        executor = RecordingExecutor()
        listener = FakeListener(texts=["", "exit"])
        code, output, _, _ = run_main(
            listener_factory=lambda config: listener,
            router=router,
            executor=executor,
            state_machine=machine,
        )
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(executor.calls, [])
        self.assertTrue(any("Say it again?" in line for line in output))
        self.assertIs(machine.state, ListeningState.STOPPED)

    def test_router_failure_recovers_and_loop_continues(self):
        machine = ListeningStateMachine()
        delegate = ScriptedRouter(
            {
                "time": CommandRequest(
                    CommandKind.TOOL, tool_name="clock", arguments={}
                ),
                "exit": CommandRequest(CommandKind.EXIT),
            }
        )
        router = ExplodingOnceRouter(delegate)
        executor = RecordingExecutor()
        listener = FakeListener(texts=["anything", "time", "exit"])
        code, output, _, _ = run_main(
            listener_factory=lambda config: listener,
            router=router,
            executor=executor,
            state_machine=machine,
        )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(
            any(
                "couldn't" in line.lower() or "problem" in line.lower()
                for line in output
            )
        )
        self.assertEqual(executor.calls, [("clock", {})])
        self.assertIs(machine.state, ListeningState.STOPPED)

    def test_unexpected_transcription_error_shuts_down_cleanly(self):
        machine = ListeningStateMachine()
        listener = FakeListener(
            on_transcribe=lambda: RuntimeError("provider exploded")
        )
        code, output, _, _ = run_main(
            listener_factory=lambda config: listener,
            state_machine=machine,
        )
        self.assertEqual(code, EXIT_RUNTIME_ERROR)
        self.assertTrue(listener.closed)
        self.assertIs(machine.state, ListeningState.STOPPED)
        self.assertTrue(
            any("problem hearing you" in line for line in output)
        )
        self.assertFalse(
            any("Traceback" in line for line in output)
        )

    def test_unexpected_transcription_error_never_runs_tools(self):
        router = ScriptedRouter(
            {"exit": CommandRequest(CommandKind.EXIT)}
        )
        executor = RecordingExecutor()
        listener = FakeListener(
            on_transcribe=lambda: RuntimeError("provider exploded")
        )
        code, _, _, _ = run_main(
            listener_factory=lambda config: listener,
            router=router,
            executor=executor,
        )
        self.assertEqual(code, EXIT_RUNTIME_ERROR)
        self.assertEqual(executor.calls, [])


if __name__ == "__main__":
    unittest.main()
