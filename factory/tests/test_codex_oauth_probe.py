import base64
import importlib.util
import json
import sys
import threading
from pathlib import Path


def _module():
    path = Path(__file__).resolve().parents[1] / "tools" / "probe_codex_oauth_concurrency.py"
    spec = importlib.util.spec_from_file_location("probe_codex_oauth_concurrency", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fake_jwt(claims):
    def enc(value):
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{enc({'alg': 'none'})}.{enc(claims)}.signature"


def _write_fake_home(path: Path):
    path.mkdir(parents=True)
    access = _fake_jwt(
        {
            "iss": "https://auth.example.test",
            "aud": ["https://api.example.test"],
            "scp": ["openid", "offline_access"],
            "iat": 1000,
            "nbf": 1000,
            "exp": 2000,
            "session_id": "fake-session-id",
        }
    )
    id_token = _fake_jwt(
        {
            "iss": "https://auth.example.test",
            "aud": ["client"],
            "iat": 1000,
            "exp": 1100,
        }
    )
    refresh = "fake-refresh-token-value"
    (path / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "OPENAI_API_KEY": None,
                "tokens": {
                    "access_token": access,
                    "id_token": id_token,
                    "refresh_token": refresh,
                    "account_id": "fake-account-id",
                },
                "last_refresh": "2026-06-16T00:00:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return access, id_token, refresh


def test_inspect_home_reports_metadata_without_token_values(tmp_path):
    mod = _module()
    home = tmp_path / "codex-home"
    access, id_token, refresh = _write_fake_home(home)

    metadata = mod.inspect_home(home, now=1500)
    text = json.dumps(metadata)

    assert access not in text
    assert id_token not in text
    assert refresh not in text
    assert metadata["auth_mode"] == "chatgpt"
    assert metadata["openai_api_key_present"] is False
    assert metadata["tokens"]["refresh_token_present"] is True
    assert metadata["tokens"]["access_token"]["seconds_remaining"] == 500
    assert metadata["tokens"]["access_token"]["has_session_id_claim"] is True


def test_seed_ephemeral_home_omits_usable_refresh_token(tmp_path):
    mod = _module()
    canonical = tmp_path / "canonical"
    access, id_token, refresh = _write_fake_home(canonical)
    output = tmp_path / "seat-home"
    trust_cwd = tmp_path / "workspace"

    summary = mod.seed_ephemeral_home(canonical, output, trust_cwd=trust_cwd)
    child_auth = json.loads((output / "auth.json").read_text(encoding="utf-8"))

    assert child_auth["OPENAI_API_KEY"] is None
    assert child_auth["tokens"]["access_token"] == access
    assert child_auth["tokens"]["id_token"] == id_token
    assert "refresh_token" not in child_auth["tokens"]
    assert refresh not in json.dumps(child_auth)
    assert summary["refresh_token_present"] is False
    assert summary["auth_json_mode"] == "0o600"
    assert summary["home_mode"] == "0o700"
    assert str(trust_cwd) in (output / "config.toml").read_text(encoding="utf-8")


def test_run_one_is_dry_by_default(tmp_path):
    mod = _module()
    payload = mod.run_one(
        tmp_path,
        tmp_path / "home",
        tmp_path,
        prompt="hello",
        model="gpt-5.5",
        real=False,
        timeout_s=1,
    )

    assert payload["would_run"] is True
    assert payload["argv_shape"][-1] == "<prompt>"


def test_run_one_reports_auth_json_change_without_digest(tmp_path, monkeypatch):
    mod = _module()
    home = tmp_path / "home"
    _write_fake_home(home)

    class FakeProc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(argv, **kwargs):
        auth_path = home / "auth.json"
        auth = json.loads(auth_path.read_text(encoding="utf-8"))
        auth["last_refresh"] = "mutated-by-probe"
        auth_path.write_text(json.dumps(auth) + "\n", encoding="utf-8")
        return FakeProc()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    payload = mod.run_one(
        tmp_path,
        home,
        tmp_path,
        prompt="hello",
        model="gpt-5.5",
        real=True,
        timeout_s=1,
    )
    text = json.dumps(payload)

    assert payload["auth_json_changed"] is True
    assert "sha256" not in text
    assert "fake-refresh-token-value" not in text


def test_run_many_seeds_isolated_no_refresh_homes_and_stays_dry(tmp_path):
    mod = _module()
    canonical = tmp_path / "canonical"
    _, _, refresh = _write_fake_home(canonical)

    payload = mod.run_many(
        tmp_path,
        canonical,
        tmp_path / "seats",
        tmp_path / "workspaces",
        count=3,
        refresh_token_mode="omit",
        prompt="hello",
        model="gpt-5.5",
        real=False,
        timeout_s=1,
        force=False,
    )

    assert payload["would_run"] is True
    assert payload["count"] == 3
    assert [seat["seat_id"] for seat in payload["seats"]] == ["seat-001", "seat-002", "seat-003"]
    for seat in payload["seats"]:
        auth_path = Path(seat["home"]) / "auth.json"
        auth_text = auth_path.read_text(encoding="utf-8")
        auth = json.loads(auth_text)
        assert "refresh_token" not in auth["tokens"]
        assert refresh not in auth_text
        assert seat["seed"]["refresh_token_present"] is False
        assert Path(seat["cwd"]).is_dir()


def test_run_many_reports_canonical_and_seat_auth_changes_without_digests(tmp_path, monkeypatch):
    mod = _module()
    canonical = tmp_path / "canonical"
    _, _, refresh = _write_fake_home(canonical)
    canonical_lock = threading.Lock()

    def fake_run_one(root, home, cwd, *, prompt, model, real, timeout_s):
        auth_path = home / "auth.json"
        auth = json.loads(auth_path.read_text(encoding="utf-8"))
        auth["last_refresh"] = f"seat-mutated-{home.name}"
        auth_path.write_text(json.dumps(auth) + "\n", encoding="utf-8")
        canonical_auth_path = canonical / "auth.json"
        with canonical_lock:
            canonical_auth = json.loads(canonical_auth_path.read_text(encoding="utf-8"))
            canonical_auth["last_refresh"] = "canonical-mutated"
            canonical_auth_path.write_text(json.dumps(canonical_auth) + "\n", encoding="utf-8")
        return {"returncode": 0, "stdout_tail": "ok", "stderr_tail": ""}

    monkeypatch.setattr(mod, "run_one", fake_run_one)

    payload = mod.run_many(
        tmp_path,
        canonical,
        tmp_path / "seats",
        tmp_path / "workspaces",
        count=2,
        refresh_token_mode="empty",
        prompt="hello",
        model="gpt-5.5",
        real=True,
        timeout_s=1,
        force=False,
    )
    changes = payload["auth_json_changes"]
    text = json.dumps(payload)

    assert payload["ok_count"] == 2
    assert changes["canonical_auth_json_changed"] is True
    assert changes["seat_auth_json_changed"] == [
        {"seat_id": "seat-001", "auth_json_changed": True},
        {"seat_id": "seat-002", "auth_json_changed": True},
    ]
    assert "sha256" not in text
    assert refresh not in text
