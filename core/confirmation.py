"""Bound, single-use confirmation tickets for Jarvis tool actions.

This module closes the gap between "ask a human yes/no" and "the yes
that came back actually belongs to *this* action". A bare
``ApprovalResult(APPROVED)`` carries no evidence of which tool, which
arguments or which moment it was granted for, so a provider that replayed
a cached approval — or approved a different tool — would be trusted.

A :class:`ConfirmationTicket` fixes that. Before prompting, the executor
*mints* a ticket bound to the exact tool id and a digest of the exact
validated arguments, with a fresh nonce and a deadline. The approval
provider must echo that nonce back. The ticket is then *consumed* once,
and only if:

* the nonce is still pending (not stale, not already used),
* it was minted for this same tool id and arguments fingerprint, and
* its deadline has not passed.

Anything else fails closed. Consumption is single-use: a second attempt
with the same nonce is rejected.

The registry is pure logic over an injected monotonic clock and an
injectable nonce source, so it is fully unit-testable with no terminal,
microphone or real time. It holds no secrets and logs nothing.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Optional

from core.permissions import PermissionLevel

# Default how long a minted ticket stays redeemable, in seconds. Short on
# purpose: a confirmation is meant to be consumed within one prompt.
DEFAULT_TICKET_TTL = 60.0


def fingerprint_args(args: Optional[Mapping[str, Any]]) -> str:
    """Return a stable digest of validated arguments.

    Key order does not matter, and the result is a fixed-length hex
    digest so it is safe to store and compare. The raw argument values
    are never returned or logged — only their digest — so a fingerprint
    cannot leak a path, a secret or a transcript.
    """
    payload = dict(args or {})
    try:
        canonical = json.dumps(
            payload, sort_keys=True, default=repr, separators=(",", ":")
        )
    except (TypeError, ValueError):
        canonical = repr(sorted(payload.items(), key=lambda kv: str(kv[0])))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ConfirmationTicket:
    """A minted, redeemable approval bound to one exact action.

    ``token`` is the opaque nonce the provider must echo. ``tool_name``
    and ``args_fingerprint`` bind the ticket to the precise action that
    was shown for approval, so it cannot authorise a different tool or
    different arguments. ``expires_at`` is a monotonic deadline.
    """

    token: str
    tool_name: str
    args_fingerprint: str
    level: PermissionLevel
    issued_at: float
    expires_at: float


class ConfirmationRegistry:
    """Issues and redeems single-use confirmation tickets.

    Dependencies (clock and nonce factory) are injected so tests never
    touch real time or real randomness. The registry is deliberately
    small: it stores only pending tickets, prunes expired ones as it
    goes, and exposes ``revoke_all`` for shutdown.
    """

    def __init__(
        self,
        *,
        clock: Optional[Callable[[], float]] = None,
        nonce_factory: Optional[Callable[[], str]] = None,
        ttl: float = DEFAULT_TICKET_TTL,
    ) -> None:
        if ttl <= 0:
            raise ValueError("ttl must be positive")
        self._clock = clock if clock is not None else time.monotonic
        self._nonce = (
            nonce_factory if nonce_factory is not None else _new_nonce
        )
        self._ttl = float(ttl)
        self._pending: dict[str, ConfirmationTicket] = {}

    def __len__(self) -> int:
        return len(self._pending)

    def issue(
        self,
        tool_name: str,
        args_fingerprint: str,
        level: PermissionLevel,
    ) -> ConfirmationTicket:
        """Mint a fresh ticket bound to one exact action.

        Each call returns a brand-new nonce, so two invocations — even of
        the same tool with the same arguments — never share a ticket.
        """
        self._prune()
        now = self._clock()
        ticket = ConfirmationTicket(
            token=self._nonce(),
            tool_name=tool_name,
            args_fingerprint=args_fingerprint,
            level=level,
            issued_at=now,
            expires_at=now + self._ttl,
        )
        self._pending[ticket.token] = ticket
        return ticket

    def consume(
        self,
        token: str,
        *,
        tool_name: str,
        args_fingerprint: str,
    ) -> bool:
        """Redeem a ticket, honouring binding, expiry and single-use.

        Returns ``True`` only when the token is still pending, was minted
        for this exact ``tool_name`` and ``args_fingerprint``, and has not
        expired. Redemption removes the ticket, so the same token can
        never be redeemed twice. Every failure path returns ``False``
        (fail closed) and leaves no partial state.
        """
        if not isinstance(token, str) or not token:
            return False
        ticket = self._pending.pop(token, None)
        if ticket is None:
            # Unknown, never minted here, or already redeemed: a
            # redeemed ticket has left the pending set (single use).
            return False
        if self._clock() > ticket.expires_at:
            return False
        if ticket.tool_name != tool_name:
            return False
        if ticket.args_fingerprint != args_fingerprint:
            return False
        return True

    def revoke(self, token: str) -> None:
        """Discard one pending ticket (e.g. the prompt was interrupted)."""
        if isinstance(token, str):
            self._pending.pop(token, None)

    def revoke_all(self) -> None:
        """Discard every pending ticket (shutdown fails closed)."""
        self._pending.clear()

    def _prune(self) -> None:
        now = self._clock()
        expired = [
            token
            for token, ticket in self._pending.items()
            if now > ticket.expires_at
        ]
        for token in expired:
            self._pending.pop(token, None)


def _new_nonce() -> str:
    """A fresh, unguessable confirmation nonce."""
    return secrets.token_hex(16)
