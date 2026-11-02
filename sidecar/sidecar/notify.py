"""Push race snapshots to FiestaBoard's receive route.

- `POST {FIESTABOARD_RECEIVE_URL}` with `Authorization: Bearer <token>` and
  the snapshot plus `sent_at` (UTC ISO-8601, `Z`). Full snapshots: a lost or
  repeated push does no harm, the latest one wins.
- 5 s timeout, redirects not followed (the token only ever goes to the
  configured URL), never retried here: the next change or heartbeat is the
  retry.
- 401/403 is logged at ERROR (token wrong or revoked); any other non-2xx, a
  timeout or a connection error at WARNING, once per outcome change so a
  FiestaBoard outage doesn't flood the log. The token and the response body
  are never logged.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import requests

log = logging.getLogger(__name__)

NOTIFY_TIMEOUT_S = 5.0


def iso_utc(ts: datetime) -> str:
    """UTC ISO-8601 with a `Z` suffix, millisecond precision."""
    return ts.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Notifier:
    def __init__(
        self,
        url: str,
        token: str,
        session: requests.Session,
        timeout: float = NOTIFY_TIMEOUT_S,
    ) -> None:
        self._url = url
        self._token = token
        self._session = session
        self._timeout = timeout
        self._last_outcome = "ok"

    def push(self, snapshot: dict[str, Any], now: datetime | None = None) -> bool:
        """POST one snapshot. Returns True on 2xx. Never raises."""
        body = dict(snapshot)
        body["sent_at"] = iso_utc(now or datetime.now(UTC))
        status: int | str
        try:
            response = self._session.post(
                self._url,
                json=body,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=self._timeout,
                allow_redirects=False,
            )
        except (requests.RequestException, ValueError) as exc:
            # ValueError: e.g. UnicodeEncodeError for a non-latin-1 token header.
            outcome, level, status = f"failed:{type(exc).__name__}", logging.WARNING, "-"
        else:
            status = response.status_code
            response.close()  # the body is never read or logged
            if 200 <= status < 300:
                outcome, level = "ok", logging.DEBUG
            elif status in (401, 403):
                outcome, level = "auth_failed", logging.ERROR
            else:
                outcome, level = f"failed:{status}", logging.WARNING

        if outcome != self._last_outcome:
            # Log state changes at their level; repeats only at DEBUG.
            log.log(
                logging.INFO if outcome == "ok" else level,
                "push outcome=%s status=%s session=%s lap=%s",
                outcome,
                status,
                snapshot.get("session"),
                snapshot.get("lap"),
            )
        else:
            log.debug("push outcome=%s status=%s lap=%s", outcome, status, snapshot.get("lap"))
        self._last_outcome = outcome
        return outcome == "ok"
