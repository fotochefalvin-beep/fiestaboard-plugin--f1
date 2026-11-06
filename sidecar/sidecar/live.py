"""Everything that touches FastF1 internals lives here (FastF1 >= 3.9).

- Sign-in: `fastf1.internals.f1account.get_token()` with the password method,
  headless: the credentials are put in this process's environment
  (`F1_USERNAME`/`F1_PASSWORD`, read by FastF1's `default_login`) and
  `FASTF1_AUTH_METHOD=password`. FastF1 stores the token under
  `platformdirs.user_data_dir("fastf1")`; the compose file points
  `XDG_DATA_HOME` at a named volume, so a restart reuses a valid token and a
  sign-in only happens when it has expired (~4 days).
- Live feed: `LiveClient` subclasses `fastf1.livetiming.client.SignalRClient`,
  which only writes to a file, and hands every message to a callback
  instead. It refuses to connect without a token (FastF1 would silently fall
  back to an unauthenticated, empty feed).
- Archive: `archive_token()` for the replay's authenticated downloads.

Nothing here ever logs the credentials or the token.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

# The topics the session state needs, plus Heartbeat/RaceControlMessages so
# a quiet session still shows the feed is alive.
TOPICS = [
    "Heartbeat",
    "DriverList",
    "TimingData",
    "LapCount",
    "TrackStatus",
    "SessionStatus",
    "ExtrapolatedClock",
    "RaceControlMessages",
    # Which session the feed is on: before a session starts, F1 still
    # serves the previous session's final state.
    "SessionInfo",
]
CONNECT_TIMEOUT_S = 30.0

OnTopic = Callable[[str, Any], None]


class AuthError(Exception):
    """Sign-in to the F1 account failed (wrong password, MFA, F1 down...)."""


def configure_auth(username: str, password: str) -> None:
    """Headless password sign-in for FastF1 (never prompts: no TTY needed)."""
    os.environ["FASTF1_AUTH_METHOD"] = "password"
    os.environ["F1_USERNAME"] = username
    os.environ["F1_PASSWORD"] = password


def ensure_token(force: bool = False) -> float:
    """Make sure a valid token is stored, signing in if needed.

    Returns the token's expiry (epoch seconds). Raises `AuthError`.
    """
    from fastf1.exceptions import F1AuthError
    from fastf1.internals.f1account import get_token

    try:
        token = get_token(force_renew=force)
    except F1AuthError as exc:
        raise AuthError(str(exc)) from None
    except Exception as exc:  # network errors inside the sign-in flow
        raise AuthError(type(exc).__name__) from None
    return token.expires_at


def stored_token_expiry() -> float | None:
    """Expiry of the stored token without signing in (None: no token)."""
    from fastf1.internals.f1account import token_manager

    token = token_manager.get_stored()
    return None if token is None else token.expires_at


def archive_token() -> str:
    """The bearer token for archived `/static/...` downloads (replay)."""
    from fastf1.internals.f1account import get_token

    ensure_token()
    return get_token().value


def dispatch(msg: Any, on_topic: OnTopic) -> int:
    """Feed one SignalR message into *on_topic*; returns the topics handed on.

    - `feed` messages are `[topic, data, timestamp]` (a delta).
    - The Subscribe completion carries the current state of every topic as
      `{topic: data}` (`CompletionMessage.result`); SessionInfo is handed on
      first, so the receiver knows which session the rest belongs to.
    """
    result = getattr(msg, "result", None)
    if isinstance(result, dict):
        items = sorted(result.items(), key=lambda item: item[0] != "SessionInfo")
        for topic, data in items:
            on_topic(str(topic), data)
        return len(result)
    if isinstance(msg, list) and len(msg) >= 2 and isinstance(msg[0], str):
        on_topic(msg[0], msg[1])
        return 1
    return 0


def _client_base():
    from fastf1.livetiming.client import SignalRClient

    return SignalRClient


def make_client(on_topic: OnTopic, logger: logging.Logger | None = None):
    """A connected-on-demand live client feeding *on_topic*."""
    base = _client_base()

    class LiveClient(base):  # type: ignore[misc, valid-type]
        def __init__(self) -> None:
            super().__init__(filename=os.devnull, timeout=0, logger=logger or log)
            self.topics = list(TOPICS)
            self.last_message_at = 0.0
            self._closed = threading.Event()

        def _on_message(self, msg: Any) -> None:  # noqa: D401 -- FastF1 hook
            self.last_message_at = time.monotonic()
            try:
                dispatch(msg, on_topic)
            except Exception:
                log.exception("live: failed to handle a message")

        def _on_close(self) -> None:
            self._is_connected = False
            self._closed.set()
            log.info("live: connection closed")

        def _access_token_factory(self):
            # Unlike FastF1, never connect without a token.
            ensure_token()
            from fastf1.internals.f1account import get_token

            return lambda: get_token().value

        def connect(self) -> None:
            """Negotiate, connect and subscribe; raises on failure."""
            import requests
            from signalrcore.hub_connection_builder import HubConnectionBuilder

            token_factory = self._access_token_factory()
            r = requests.options(self._negotiate_url, headers=self.headers, timeout=10)
            cookie = r.cookies.get("AWSALBCORS")
            if cookie:
                self.headers.update({"Cookie": f"AWSALBCORS={cookie}"})
            options = {
                "verify_ssl": True,
                "access_token_factory": token_factory,
                "headers": self.headers,
            }
            self._connection = (
                HubConnectionBuilder()
                .with_url(self._connection_url, options=options)
                .configure_logging(logging.WARNING)
                .build()
            )
            self._connection.on_open(self._on_connect)
            self._connection.on_close(self._on_close)
            self._connection.on("feed", self._on_message)
            self._connection.start()
            deadline = time.monotonic() + CONNECT_TIMEOUT_S
            while not self._is_connected:
                if time.monotonic() > deadline:
                    self.close()
                    raise ConnectionError("live timing connection timed out")
                time.sleep(0.1)
            self.last_message_at = time.monotonic()
            self._connection.send("Subscribe", [self.topics], on_invocation=self._on_message)

        @property
        def connected(self) -> bool:
            return bool(self._is_connected) and not self._closed.is_set()

        def close(self) -> None:
            try:
                if self._connection is not None:
                    self._connection.stop()
            except Exception:  # already closed
                pass
            self._is_connected = False

    return LiveClient()
