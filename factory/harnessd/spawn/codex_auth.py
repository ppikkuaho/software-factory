"""Codex OAuth home preparation.

The canonical pinned Codex home resolves its auth through the machine's one
``~/.codex/auth.json`` token lineage. Worker seats get a fresh isolated
CODEX_HOME seeded from the current access/id credentials, with no usable child
refresh token. Token values never leave auth files or authenticated headers.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from harnessd import addressing, config
from harnessd.spawn import oauth_guard


MIN_ACCESS_SECONDS: int = 15 * 60
AUTH_PREFLIGHT_TIMEOUT_SECONDS: float = 3.0
CODEX_MODELS_ENDPOINT: str = "https://chatgpt.com/backend-api/codex/models"
AUTH_REPAIR_GUIDANCE: str = (
    "Run codex login against the default ~/.codex home; the pinned auth symlink "
    "will see the fresh token immediately. If the pinned auth.json is not that "
    "symlink, run python3 tools/repair_pinned_codex_auth.py from the harness repo."
)


@dataclass(frozen=True)
class CodexSeat:
    seat_id: str
    home: Path
    auth_version: str
    access_seconds_remaining: int | None


def _read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        value = json.load(fh)
    if not isinstance(value, dict):
        raise oauth_guard.AuthExpired(f"{path} did not contain a JSON object")
    return value


def _decode_jwt_claims(token: str | None) -> dict[str, Any] | None:
    if not token or token.count(".") < 2:
        return None
    try:
        payload = token.split(".", 2)[1]
        payload += "=" * ((4 - len(payload) % 4) % 4)
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        claims = json.loads(decoded)
    except Exception:
        return None
    return claims if isinstance(claims, dict) else None


def _seconds_remaining(token: str | None, *, now: int | None = None) -> int | None:
    claims = _decode_jwt_claims(token)
    if not claims:
        return None
    exp = claims.get("exp")
    if not isinstance(exp, int):
        return None
    now = int(time.time()) if now is None else now
    return exp - now


def _preflight_access_token(access_token: str, account_id: str | None) -> None:
    """Refuse only a server-conclusive canonical auth death."""
    query = urlparse.urlencode({"client_version": config.PINNED_CODEX_VERSION})
    headers = {"Authorization": f"Bearer {access_token}"}
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    request = urlrequest.Request(
        f"{CODEX_MODELS_ENDPOINT}?{query}",
        headers=headers,
        method="GET",
    )
    try:
        with urlrequest.urlopen(
            request,
            timeout=AUTH_PREFLIGHT_TIMEOUT_SECONDS,
        ):
            pass
    except urlerror.HTTPError as exc:
        if exc.code in {401, 403}:
            raise oauth_guard.AuthExpired(
                f"Codex canonical OAuth was rejected by the server ({exc.code}). "
                f"{AUTH_REPAIR_GUIDANCE}"
            ) from exc
    except (urlerror.URLError, TimeoutError, OSError):
        # Offline, TLS, DNS, and timeout failures do not prove auth death.
        return


def assert_canonical_ready(
    canonical_home: Path,
    *,
    min_access_seconds: int = MIN_ACCESS_SECONDS,
    now: int | None = None,
) -> dict[str, Any]:
    """Fail before pane-open when canonical Codex OAuth is missing or stale."""
    auth_path = canonical_home / "auth.json"
    if not auth_path.is_file():
        raise oauth_guard.AuthExpired(
            f"Codex OAuth auth.json missing at {auth_path}. {AUTH_REPAIR_GUIDANCE}"
        )
    auth = _read_json(auth_path)
    if auth.get("OPENAI_API_KEY"):
        raise oauth_guard.ApiKeyForbidden(
            "Codex canonical auth contains OPENAI_API_KEY; API keys are forbidden"
        )
    tokens = auth.get("tokens") if isinstance(auth.get("tokens"), dict) else {}
    if auth.get("auth_mode") != "chatgpt":
        raise oauth_guard.AuthExpired(
            "Codex canonical auth is not ChatGPT/OAuth mode. "
            f"{AUTH_REPAIR_GUIDANCE}"
        )
    if not tokens.get("access_token"):
        raise oauth_guard.AuthExpired(
            f"Codex access token missing. {AUTH_REPAIR_GUIDANCE}"
        )
    if not tokens.get("id_token"):
        raise oauth_guard.AuthExpired(f"Codex ID token missing. {AUTH_REPAIR_GUIDANCE}")
    if not tokens.get("refresh_token"):
        raise oauth_guard.AuthExpired(
            f"Codex canonical refresh token missing. {AUTH_REPAIR_GUIDANCE}"
        )
    remaining = _seconds_remaining(tokens.get("access_token"), now=now)
    if remaining is not None and remaining <= min_access_seconds:
        raise oauth_guard.AuthExpired(
            "Codex access token is expired or near expiry. "
            f"{AUTH_REPAIR_GUIDANCE}"
        )
    _preflight_access_token(
        str(tokens["access_token"]),
        str(tokens.get("account_id") or "") or None,
    )
    return auth


def access_seconds_remaining(auth: dict[str, Any], *, now: int | None = None) -> int | None:
    tokens = auth.get("tokens") if isinstance(auth.get("tokens"), dict) else {}
    return _seconds_remaining(tokens.get("access_token"), now=now)


def auth_version(canonical_home: Path) -> str:
    """Return a non-secret generation marker for the canonical Codex auth file."""
    auth_path = canonical_home / "auth.json"
    stat = auth_path.stat()
    return f"authv-{stat.st_mtime_ns}-{stat.st_size}"


def _private_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, mode=0o700)
    os.chmod(path, 0o700)


def _toml_string(value: str) -> str:
    return json.dumps(value)


def _write_minimal_config(
    home: Path,
    trust_cwd: str | None,
    *,
    reasoning_effort: str = "high",
    notify_argv: list[str] | None = None,
) -> None:
    lines = [
        "# Generated by harnessd.spawn.codex_auth.",
        "# No secrets live in this config file.",
        'approval_policy = "never"',
        f"model_reasoning_effort = {_toml_string(reasoning_effort)}",
        "",
    ]
    if notify_argv:
        values = ", ".join(_toml_string(str(value)) for value in notify_argv)
        lines.extend([f"notify = [{values}]", ""])
    lines.extend([
        "[tui.model_availability_nux]",
        '"gpt-5.5" = 4',
        "",
    ])
    if trust_cwd:
        real = os.path.realpath(trust_cwd)
        lines.extend(
            [
                f"[projects.{_toml_string(real)}]",
                'trust_level = "trusted"',
                "",
            ]
        )
    (home / "config.toml").write_text("\n".join(lines), encoding="utf-8")


def new_seat_id(node_address: str) -> str:
    seat_id = f"{addressing.session_name_for(node_address)}-{time.time_ns()}"
    return seat_id


def seat_home(canonical_home: Path, seat_id: str) -> Path:
    return canonical_home.parent / "seats" / seat_id


def new_seat_home(canonical_home: Path, node_address: str) -> Path:
    return seat_home(canonical_home, new_seat_id(node_address))


def seed_ephemeral_home(
    canonical_home: Path,
    node_address: str,
    *,
    trust_cwd: str | None,
    reasoning_effort: str = "high",
    notify_argv: list[str] | None = None,
) -> CodexSeat:
    """Create one isolated Codex home for a worker seat.

    The child receives the access/id/account fields needed for a fresh-token
    turn and a present-but-empty refresh_token field, matching the P2/P4 probes.
    The canonical refresh token is never copied.
    """
    auth = assert_canonical_ready(canonical_home)
    version = auth_version(canonical_home)
    remaining = access_seconds_remaining(auth)
    tokens = auth.get("tokens") if isinstance(auth.get("tokens"), dict) else {}
    seat_id = new_seat_id(node_address)
    home = seat_home(canonical_home, seat_id)
    _private_dir(home)
    _write_minimal_config(
        home,
        trust_cwd,
        reasoning_effort=reasoning_effort,
        notify_argv=notify_argv,
    )
    child_tokens = {
        key: tokens[key]
        for key in ("access_token", "id_token", "account_id")
        if tokens.get(key)
    }
    child_tokens["refresh_token"] = ""
    child_auth = {
        "auth_mode": auth.get("auth_mode"),
        "OPENAI_API_KEY": None,
        "tokens": child_tokens,
        "last_refresh": auth.get("last_refresh"),
    }
    auth_path = home / "auth.json"
    auth_path.write_text(json.dumps(child_auth, indent=2) + "\n", encoding="utf-8")
    os.chmod(auth_path, 0o600)
    return CodexSeat(
        seat_id=seat_id,
        home=home,
        auth_version=version,
        access_seconds_remaining=remaining,
    )
