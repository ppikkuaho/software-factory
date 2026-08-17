import base64
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from urllib import error as urlerror

import pytest

from harnessd.spawn import codex_auth, oauth_guard


def _fake_jwt(claims):
    def encode(value):
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode(claims)}.signature"


def _write_auth(home: Path, *, expires_at: int = 2_000_000_000) -> dict:
    home.mkdir(parents=True, exist_ok=True)
    auth = {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "access_token": _fake_jwt({"iat": 1_000, "exp": expires_at}),
            "id_token": _fake_jwt({"iat": 1_000, "exp": expires_at}),
            "refresh_token": "fake-refresh-token",
            "account_id": "fake-account-id",
        },
        "last_refresh": "2026-07-28T00:00:00Z",
    }
    (home / "auth.json").write_text(json.dumps(auth) + "\n", encoding="utf-8")
    return auth


class _NoBodyResponse:
    def __init__(self):
        self.entered = False
        self.exited = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.exited = True
        return False


def _capture_request(monkeypatch):
    captured = {}
    response = _NoBodyResponse()

    def fake_urlopen(request, *, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return response

    monkeypatch.setattr(codex_auth.urlrequest, "urlopen", fake_urlopen)
    return captured, response


@pytest.mark.parametrize("status", [401, 403])
def test_server_rejected_access_token_fails_loudly(tmp_path, monkeypatch, status):
    canonical = tmp_path / "canonical"
    _write_auth(canonical)

    def rejected(request, *, timeout):
        raise urlerror.HTTPError(
            request.full_url,
            status,
            "rejected",
            hdrs={},
            fp=None,
        )

    monkeypatch.setattr(codex_auth.urlrequest, "urlopen", rejected)

    with pytest.raises(oauth_guard.AuthExpired) as exc_info:
        codex_auth.assert_canonical_ready(canonical, now=1_000)

    message = str(exc_info.value)
    assert "codex login" in message
    assert "~/.codex" in message
    assert "tools/repair_pinned_codex_auth.py" in message


def test_valid_access_token_preflight_uses_pinned_endpoint_and_no_body(
    tmp_path, monkeypatch
):
    canonical = tmp_path / "canonical"
    auth = _write_auth(canonical)
    captured, response = _capture_request(monkeypatch)

    assert codex_auth.assert_canonical_ready(canonical, now=1_000) == auth

    request = captured["request"]
    assert request.full_url == (
        "https://chatgpt.com/backend-api/codex/models?client_version=0.144.1"
    )
    assert request.get_header("Authorization") == (
        f"Bearer {auth['tokens']['access_token']}"
    )
    assert request.get_header("Chatgpt-account-id") == "fake-account-id"
    assert request.get_method() == "GET"
    assert captured["timeout"] == 3.0
    assert response.entered is True
    assert response.exited is True


@pytest.mark.parametrize(
    "failure",
    [
        urlerror.URLError("network unavailable"),
        urlerror.HTTPError(
            "https://chatgpt.com/backend-api/codex/models",
            500,
            "server error",
            hdrs={},
            fp=None,
        ),
    ],
)
def test_inconclusive_models_preflight_does_not_refuse(
    tmp_path, monkeypatch, failure
):
    canonical = tmp_path / "canonical"
    auth = _write_auth(canonical)

    def inconclusive(request, *, timeout):
        raise failure

    monkeypatch.setattr(codex_auth.urlrequest, "urlopen", inconclusive)

    assert codex_auth.assert_canonical_ready(canonical, now=1_000) == auth


def test_symlinked_canonical_auth_seeds_worker_without_refresh(
    tmp_path, monkeypatch
):
    default_home = tmp_path / "user" / ".codex"
    auth = _write_auth(default_home)
    canonical = tmp_path / "repo" / ".codex-pinned" / "config"
    canonical.mkdir(parents=True)
    (canonical / "auth.json").symlink_to(default_home / "auth.json")
    monkeypatch.setattr(
        codex_auth,
        "_preflight_access_token",
        lambda access_token, account_id: None,
        raising=False,
    )

    seat = codex_auth.seed_ephemeral_home(
        canonical,
        "L5#exec",
        trust_cwd=str(tmp_path / "workspace"),
    )
    child_auth = json.loads((seat.home / "auth.json").read_text(encoding="utf-8"))

    assert child_auth["tokens"]["access_token"] == auth["tokens"]["access_token"]
    assert child_auth["tokens"]["id_token"] == auth["tokens"]["id_token"]
    assert child_auth["tokens"]["account_id"] == auth["tokens"]["account_id"]
    assert child_auth["tokens"]["refresh_token"] == ""
    assert auth["tokens"]["refresh_token"] not in json.dumps(child_auth)


def test_dangling_canonical_auth_link_is_missing_auth_refusal(
    tmp_path, monkeypatch
):
    canonical = tmp_path / "repo" / ".codex-pinned" / "config"
    canonical.mkdir(parents=True)
    (canonical / "auth.json").symlink_to(tmp_path / "missing" / "auth.json")
    monkeypatch.setattr(
        codex_auth,
        "_preflight_access_token",
        lambda access_token, account_id: None,
        raising=False,
    )

    with pytest.raises(oauth_guard.AuthExpired, match="auth.json missing"):
        codex_auth.assert_canonical_ready(canonical)


def _repair_module():
    path = (
        Path(__file__).resolve().parents[1]
        / "tools"
        / "repair_pinned_codex_auth.py"
    )
    spec = importlib.util.spec_from_file_location("repair_pinned_codex_auth", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_repair_tool_backs_up_regular_auth_and_links_idempotently(tmp_path):
    repair = _repair_module()
    repo = tmp_path / "repo"
    pinned_auth = repo / ".codex-pinned" / "config" / "auth.json"
    pinned_auth.parent.mkdir(parents=True)
    pinned_auth.write_text("old pinned auth\n", encoding="utf-8")
    os.chmod(pinned_auth, 0o600)
    os.utime(pinned_auth, ns=(1_234_567_890, 1_234_567_890))
    user_home = tmp_path / "user"
    default_auth = user_home / ".codex" / "auth.json"
    default_auth.parent.mkdir(parents=True)
    default_auth.write_text("default auth\n", encoding="utf-8")

    first = repair.repair_pinned_codex_auth(
        repo_root=repo,
        user_home=user_home,
    )
    second = repair.repair_pinned_codex_auth(
        repo_root=repo,
        user_home=user_home,
    )

    backup = (
        repo
        / ".codex-pinned"
        / "auth-backups"
        / "auth.json.pre-link-1234567890"
    )
    assert first.changed is True
    assert first.backup_path == backup
    assert second.changed is False
    assert second.backup_path is None
    assert pinned_auth.is_symlink()
    assert os.readlink(pinned_auth) == str(default_auth.resolve())
    assert backup.read_text(encoding="utf-8") == "old pinned auth\n"
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert stat.S_IMODE(backup.parent.stat().st_mode) == 0o700
    assert list(backup.parent.iterdir()) == [backup]


def test_repair_tool_refuses_when_default_auth_is_missing(tmp_path):
    repair = _repair_module()
    repo = tmp_path / "repo"
    (repo / ".codex-pinned" / "config").mkdir(parents=True)
    user_home = tmp_path / "user"

    with pytest.raises(RuntimeError) as exc_info:
        repair.repair_pinned_codex_auth(
            repo_root=repo,
            user_home=user_home,
        )

    message = str(exc_info.value)
    assert "codex login" in message
    assert "~/.codex/auth.json" in message
    assert not (repo / ".codex-pinned" / "config" / "auth.json").exists()
