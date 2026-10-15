"""Tests for the explicit listening-state machine.

Pure logic, no audio hardware, no I/O.
"""

import unittest

from voice.states import (
    InvalidStateTransition,
    ListeningState,
    ListeningStateMachine,
)


class TestInitialState(unittest.TestCase):
    def test_defaults_to_idle(self):
        self.assertIs(ListeningStateMachine().state, ListeningState.IDLE)

    def test_history_starts_with_initial_state(self):
        machine = ListeningStateMachine()
        self.assertEqual(machine.history, (ListeningState.IDLE,))

    def test_can_start_in_a_given_state(self):
        machine = ListeningStateMachine(ListeningState.LISTENING)
        self.assertIs(machine.state, ListeningState.LISTENING)


class TestLegalTransitions(unittest.TestCase):
    def test_full_happy_path_cycle(self):
        machine = ListeningStateMachine()
        machine.to(ListeningState.LISTENING)
        machine.to(ListeningState.PROCESSING)
        machine.to(ListeningState.SPEAKING)
        machine.to(ListeningState.IDLE)
        self.assertIs(machine.state, ListeningState.IDLE)
        self.assertEqual(
            machine.history,
            (
                ListeningState.IDLE,
                ListeningState.LISTENING,
                ListeningState.PROCESSING,
                ListeningState.SPEAKING,
                ListeningState.IDLE,
            ),
        )

    def test_listening_can_return_to_idle_without_speaking(self):
        machine = ListeningStateMachine()
        machine.to(ListeningState.LISTENING)
        machine.to(ListeningState.IDLE)
        self.assertIs(machine.state, ListeningState.IDLE)

    def test_processing_can_skip_speaking(self):
        machine = ListeningStateMachine()
        machine.to(ListeningState.LISTENING)
        machine.to(ListeningState.PROCESSING)
        machine.to(ListeningState.IDLE)
        self.assertIs(machine.state, ListeningState.IDLE)

    def test_can_stop_from_every_active_state(self):
        for start in (
            ListeningState.IDLE,
            ListeningState.LISTENING,
            ListeningState.PROCESSING,
            ListeningState.SPEAKING,
        ):
            with self.subTest(start=start):
                machine = ListeningStateMachine(start)
                machine.close()
                self.assertIs(machine.state, ListeningState.STOPPED)
                self.assertTrue(machine.stopped)


class TestSameStateNoOp(unittest.TestCase):
    def test_moving_to_current_state_is_a_noop(self):
        machine = ListeningStateMachine()
        self.assertIs(machine.to(ListeningState.IDLE), ListeningState.IDLE)
        self.assertEqual(machine.history, (ListeningState.IDLE,))

    def test_close_is_idempotent(self):
        machine = ListeningStateMachine()
        machine.close()
        machine.close()
        self.assertIs(machine.state, ListeningState.STOPPED)
        self.assertEqual(
            machine.history, (ListeningState.IDLE, ListeningState.STOPPED)
        )


class TestIllegalTransitions(unittest.TestCase):
    def test_cannot_listen_while_processing(self):
        machine = ListeningStateMachine(ListeningState.PROCESSING)
        with self.assertRaises(InvalidStateTransition):
            machine.to(ListeningState.LISTENING)

    def test_cannot_speak_before_processing(self):
        machine = ListeningStateMachine(ListeningState.LISTENING)
        with self.assertRaises(InvalidStateTransition):
            machine.to(ListeningState.SPEAKING)

    def test_stopped_is_terminal(self):
        machine = ListeningStateMachine(ListeningState.STOPPED)
        for target in (
            ListeningState.IDLE,
            ListeningState.LISTENING,
            ListeningState.PROCESSING,
            ListeningState.SPEAKING,
        ):
            with self.subTest(target=target):
                with self.assertRaises(InvalidStateTransition):
                    machine.to(target)

    def test_failed_transition_leaves_state_unchanged(self):
        machine = ListeningStateMachine(ListeningState.PROCESSING)
        with self.assertRaises(InvalidStateTransition):
            machine.to(ListeningState.LISTENING)
        self.assertIs(machine.state, ListeningState.PROCESSING)
        self.assertEqual(
            machine.history, (ListeningState.PROCESSING,)
        )


class TestCanTransition(unittest.TestCase):
    def test_reports_legal_and_illegal_moves(self):
        machine = ListeningStateMachine(ListeningState.IDLE)
        self.assertTrue(machine.can_transition(ListeningState.LISTENING))
        self.assertTrue(machine.can_transition(ListeningState.STOPPED))
        self.assertTrue(machine.can_transition(ListeningState.IDLE))
        self.assertFalse(machine.can_transition(ListeningState.SPEAKING))

    def test_stopped_cannot_transition_anywhere(self):
        machine = ListeningStateMachine(ListeningState.STOPPED)
        self.assertTrue(machine.can_transition(ListeningState.STOPPED))
        for target in (
            ListeningState.IDLE,
            ListeningState.LISTENING,
            ListeningState.PROCESSING,
            ListeningState.SPEAKING,
        ):
            with self.subTest(target=target):
                self.assertFalse(machine.can_transition(target))


class TestOnChangeCallback(unittest.TestCase):
    def test_callback_receives_old_and_new_state(self):
        seen = []
        machine = ListeningStateMachine(
            on_change=lambda old, new: seen.append((old, new))
        )
        machine.to(ListeningState.LISTENING)
        machine.to(ListeningState.PROCESSING)
        self.assertEqual(
            seen,
            [
                (ListeningState.IDLE, ListeningState.LISTENING),
                (ListeningState.LISTENING, ListeningState.PROCESSING),
            ],
        )

    def test_callback_not_called_for_noop(self):
        seen = []
        machine = ListeningStateMachine(
            on_change=lambda old, new: seen.append((old, new))
        )
        machine.to(ListeningState.IDLE)
        self.assertEqual(seen, [])


if __name__ == "__main__":
    unittest.main()
