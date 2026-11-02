"""Sidecar configuration: env vars and Docker secret files -> frozen Config.

Every value is read once at startup and validated; anything missing or
malformed raises `ConfigError` so the process fails fast.

Secrets can be passed either directly (`F1_PASSWORD=...`) or as a file path
in `<NAME>_FILE` (`F1_PASSWORD_FILE=/run/secrets/f1_password`), the usual
Docker secrets pattern. A direct value wins over a file. In the compose
setup only the `_FILE` form is used.

`FIESTABOARD_RECEIVE_URL` must be an http(s) URL ending in
`/v1/plugins/f1/receive`; on the compose network that is
`http://fiestaboard:3000/api/v1/plugins/f1/receive`, via FiestaBoard's nginx.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_HEARTBEAT_FILE = "/tmp/heartbeat"
DEFAULT_JOLPICA_URL = "https://api.jolpi.ca/ergast/f1"
RECEIVE_PATH = "/v1/plugins/f1/receive"


class ConfigError(Exception):
    """A required setting is missing or malformed. Fatal at startup."""


@dataclass(frozen=True)
class Config:
    # Credentials and the FiestaBoard token are kept out of repr().
    f1_username: str = field(repr=False)
    f1_password: str = field(repr=False)
    fiestaboard_receive_url: str
    fiestaboard_api_token: str = field(repr=False)
    heartbeat_file: Path = Path(DEFAULT_HEARTBEAT_FILE)
    jolpica_url: str = DEFAULT_JOLPICA_URL


@dataclass(frozen=True)
class PushConfig:
    """Only what pushing needs (the replay command, without F1 credentials)."""

    fiestaboard_receive_url: str
    fiestaboard_api_token: str = field(repr=False)


def _read(env: Mapping[str, str], name: str, *, required: bool = True) -> str | None:
    value = env.get(name)
    if value is not None and value.strip():
        return value.strip()
    file_path = env.get(f"{name}_FILE")
    if file_path:
        try:
            value = Path(file_path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError(f"{name}_FILE={file_path!r} cannot be read: {exc.strerror}") from exc
        if value:
            return value
        raise ConfigError(f"{name}_FILE={file_path!r} is empty")
    if required:
        raise ConfigError(f"{name} is not set (set {name} or {name}_FILE)")
    return None


def _placeholder(name: str, value: str) -> str:
    # The example files hold YOUR_... placeholders; catch a forgotten copy.
    if value.startswith("YOUR_"):
        raise ConfigError(f"{name} still holds the example placeholder")
    return value


def _parse_receive_url(raw: str) -> str:
    try:
        parts = urlsplit(raw)
        parts.port  # noqa: B018 -- raises ValueError on a malformed port
    except ValueError:
        raise ConfigError(f"FIESTABOARD_RECEIVE_URL={raw!r} is not a valid URL") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError(f"FIESTABOARD_RECEIVE_URL={raw!r} must be an http(s) URL")
    if not parts.path.endswith(RECEIVE_PATH) or parts.query or parts.fragment:
        raise ConfigError(f"FIESTABOARD_RECEIVE_URL={raw!r} must end in {RECEIVE_PATH}")
    return raw


def _parse_http_url(name: str, raw: str) -> str:
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError(f"{name}={raw!r} must be an http(s) URL")
    return raw.rstrip("/")


def load_push_config(env: Mapping[str, str] | None = None) -> PushConfig:
    env = os.environ if env is None else env
    return PushConfig(
        fiestaboard_receive_url=_parse_receive_url(_read(env, "FIESTABOARD_RECEIVE_URL")),
        fiestaboard_api_token=_placeholder(
            "FIESTABOARD_API_TOKEN", _read(env, "FIESTABOARD_API_TOKEN")
        ),
    )


def load_credentials(env: Mapping[str, str] | None = None) -> tuple[str, str]:
    """F1 account email and password (F1_USERNAME[_FILE], F1_PASSWORD[_FILE])."""
    env = os.environ if env is None else env
    return (
        _placeholder("F1_USERNAME", _read(env, "F1_USERNAME")),
        _placeholder("F1_PASSWORD", _read(env, "F1_PASSWORD")),
    )


def load_config(env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    username, password = load_credentials(env)
    push = load_push_config(env)
    return Config(
        f1_username=username,
        f1_password=password,
        fiestaboard_receive_url=push.fiestaboard_receive_url,
        fiestaboard_api_token=push.fiestaboard_api_token,
        heartbeat_file=Path(
            _read(env, "HEARTBEAT_FILE", required=False) or DEFAULT_HEARTBEAT_FILE
        ),
        jolpica_url=_parse_http_url(
            "JOLPICA_URL", _read(env, "JOLPICA_URL", required=False) or DEFAULT_JOLPICA_URL
        ),
    )
