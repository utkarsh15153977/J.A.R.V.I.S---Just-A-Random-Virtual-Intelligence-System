"""Explicit listening states for the voice interaction loop.

The states describe what Jarvis is currently doing with the microphone
and the speaker. They are deliberately small and are the only sanctioned
way to move between phases, so that (for example) the application cannot
start capturing a command before it has been activated, and cannot
capture audio while it is speaking.

Transitions are validated: an attempt to make an illegal move raises
:class:`InvalidStateTransition` instead of silently continuing. Moving to
the *same* state is a harmless no-op, and ``STOPPED`` is terminal.

This module is pure logic (no audio, no I/O) and therefore fully unit
testable.
"""

from __future__ import annotations

from enum import Enum
from typing import Callable, Dict, FrozenSet, Optional, Tuple


class ListeningState(Enum):
    """What Jarvis is doing right now."""

    IDLE = "idle"  # waiting for activation (wake word / manual)
    LISTENING = "listening"  # capturing the user's command
    PROCESSING = "processing"  # routing and handling the command
    SPEAKING = "speaking"  # producing spoken output
    STOPPED = "stopped"  # resources released; terminal


# Which moves are legal. Anything not listed here is rejected.
_ALLOWED: Dict[ListeningState, FrozenSet[ListeningState]] = {
    ListeningState.IDLE: frozenset(
        {ListeningState.LISTENING, ListeningState.STOPPED}
    ),
    ListeningState.LISTENING: frozenset(
        {
            ListeningState.PROCESSING,
            ListeningState.IDLE,
            ListeningState.STOPPED,
        }
    ),
    ListeningState.PROCESSING: frozenset(
        {
            ListeningState.SPEAKING,
            ListeningState.IDLE,
            ListeningState.STOPPED,
        }
    ),
    ListeningState.SPEAKING: frozenset(
        {ListeningState.IDLE, ListeningState.STOPPED}
    ),
    ListeningState.STOPPED: frozenset(),
}


class InvalidStateTransition(RuntimeError):
    """Raised when a listening-state move is not allowed."""


class ListeningStateMachine:
    """Tracks the current :class:`ListeningState` and guards transitions."""

    def __init__(
        self,
        state: ListeningState = ListeningState.IDLE,
        on_change: Optional[
            Callable[[ListeningState, ListeningState], None]
        ] = None,
    ) -> None:
        self._state = state
        self._on_change = on_change
        self._history: Tuple[ListeningState, ...] = (state,)

    @property
    def state(self) -> ListeningState:
        return self._state

    @property
    def history(self) -> Tuple[ListeningState, ...]:
        """Every state visited, starting with the initial one."""
        return self._history

    @property
    def stopped(self) -> bool:
        return self._state is ListeningState.STOPPED

    def can_transition(self, target: ListeningState) -> bool:
        if target is self._state:
            return True
        return target in _ALLOWED[self._state]

    def to(self, target: ListeningState) -> ListeningState:
        """Move to ``target`` or raise :class:`InvalidStateTransition`.

        Moving to the current state is a no-op and returns the state.
        """
        if target is self._state:
            return self._state
        if target not in _ALLOWED[self._state]:
            raise InvalidStateTransition(
                f"cannot move from {self._state.value} to {target.value}"
            )
        previous = self._state
        self._state = target
        self._history = (*self._history, target)
        if self._on_change is not None:
            self._on_change(previous, target)
        return target

    def close(self) -> ListeningState:
        """Move to ``STOPPED`` (idempotent), releasing the machine."""
        return self.to(ListeningState.STOPPED)
