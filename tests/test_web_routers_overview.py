"""Tests for the dashboard Overview panel router.

Focus is on the three things most likely to regress silently:

1. **No secrets in the response.** The panel reads files that hold OAuth
   tokens and IMAP passwords; a refactor must never let one through.
2. **Config paths use ``cfg_get``'s varargs contract.** ``cfg_get`` takes the
   key path as ``*keys``, so a dotted ``"model.provider"`` string is looked up
   as one literal key and silently returns the default — the whole config
   summary renders as "—" with no error anywhere.
3. **A cron job whose run succeeded but whose *delivery* failed is not
   healthy.** That state is invisible in ``last_status`` (which stays "ok")
   and only shows up in ``last_delivery_error``.
"""

import json
import time

import pytest

from hermes_cli.web_routers import overview


# ── section envelope ─────────────────────────────────────────────────────────


def test_section_envelope_shape():
    sec = overview._section("ok", "all good", {"a": 1}, notes=["n"], link="/x")
    assert set(sec) == {
        "status", "headline", "detail", "notes", "link", "last_checked", "stale",
    }
    assert sec["status"] == "ok"
    assert sec["detail"] == {"a": 1}
    assert sec["last_checked"].endswith("+00:00")


def test_parse_refresh():
    assert overview._parse_refresh("") == {k: False for k in overview.SECTION_KEYS}
    assert all(overview._parse_refresh("all").values())
    single = overview._parse_refresh("email")
    assert single["email"] is True
    assert single["core"] is False
    pair = overview._parse_refresh("email,core")
    assert pair["email"] and pair["core"] and not pair["discord"]


# ── secret hygiene ───────────────────────────────────────────────────────────


def test_codex_probe_emits_no_token_values(tmp_path, monkeypatch):
    """Only booleans and non-secret metadata may leave the codex probe."""
    secret_access = "sk-secret-access-token-value-do-not-leak"
    secret_refresh = "refresh-token-value-do-not-leak"
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    (codex_dir / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "last_refresh": "2026-08-23T15:24:23.555100Z",
                "OPENAI_API_KEY": None,
                "tokens": {
                    "id_token": "id-token-value-do-not-leak",
                    "access_token": secret_access,
                    "refresh_token": secret_refresh,
                    "account_id": "acct-1234",
                },
            }
        )
    )
    # Keep the test off the real PATH: `codex --version` must not be required.
    monkeypatch.setattr(overview, "_run_cli", lambda *a, **k: {
        "ok": False, "missing": True, "exit_code": None, "stdout": "", "stderr": "",
    })

    out = overview._probe_codex(home=tmp_path)
    blob = json.dumps(out)
    for secret in (secret_access, secret_refresh, "id-token-value-do-not-leak"):
        assert secret not in blob

    assert out["auth_mode"] == "chatgpt"
    assert out["has_access_token"] is True
    assert out["has_refresh_token"] is True
    assert out["has_api_key"] is False
    assert out["logged_in"] is True


def test_codex_probe_handles_missing_auth_file(tmp_path, monkeypatch):
    monkeypatch.setattr(overview, "_run_cli", lambda *a, **k: {
        "ok": False, "missing": True, "exit_code": None, "stdout": "", "stderr": "",
    })
    out = overview._probe_codex(home=tmp_path)
    assert out["logged_in"] is False
    assert out["reason"] == "no auth.json"


# ── claude auth parsing ──────────────────────────────────────────────────────


def test_claude_probe_parses_json(monkeypatch):
    payload = {
        "loggedIn": True,
        "authMethod": "claude.ai",
        "apiProvider": "firstParty",
        "email": "user@example.com",
        "orgName": "ExampleOrg",
        "subscriptionType": "team",
    }

    def fake_cli(cmd, timeout=None):
        if cmd[:3] == ["claude", "auth", "status"]:
            return {"ok": True, "missing": False, "exit_code": 0,
                    "stdout": json.dumps(payload), "stderr": ""}
        return {"ok": True, "missing": False, "exit_code": 0,
                "stdout": "1.2.3 (Claude Code)", "stderr": ""}

    monkeypatch.setattr(overview, "_run_cli", fake_cli)
    out = overview._probe_claude_auth()
    assert out["logged_in"] is True
    assert out["org_name"] == "ExampleOrg"
    assert out["subscription_type"] == "team"
    assert out["cli_version"] == "1.2.3 (Claude Code)"


def test_claude_probe_survives_non_json_output(monkeypatch):
    """An older CLI prints only the human table — report unknown, not logged-out."""
    monkeypatch.setattr(overview, "_run_cli", lambda *a, **k: {
        "ok": True, "missing": False, "exit_code": 0,
        "stdout": "Login method: Claude Team account\n", "stderr": "",
    })
    out = overview._probe_claude_auth()
    assert out["parse_failed"] is True
    assert out["logged_in"] is None


# ── email: config read vs live reachability ──────────────────────────────────


def test_email_account_table_parsed():
    table = (
        "┌────────┬────────────┬─────────┐\n"
        "│ NAME   ┆ BACKENDS   ┆ DEFAULT │\n"
        "╞════════╪════════════╪═════════╡\n"
        "│ hermes ┆ imap, smtp ┆ yes     │\n"
        "└────────┴────────────┴─────────┘\n"
    )
    import hermes_cli.web_routers.overview as ov

    orig = ov._run_cli
    ov._run_cli = lambda *a, **k: {
        "ok": True, "missing": False, "exit_code": 0, "stdout": table, "stderr": "",
    }
    try:
        out = ov._probe_email_accounts()
    finally:
        ov._run_cli = orig

    assert out["installed"] is True
    assert out["accounts"] == [
        {"name": "hermes", "backends": ["imap", "smtp"], "default": True}
    ]


def test_imap_probe_is_not_run_on_a_plain_page_load(monkeypatch, tmp_path):
    """The ~2s IMAP round-trip must never be paid by an unforced request.

    With no cached value the section reports "not checked" rather than blocking.
    """
    monkeypatch.setattr(overview, "_probe_cache", {})
    monkeypatch.setattr(overview, "_probe_cache_loaded", True)
    monkeypatch.setattr(overview, "_save_probe_cache", lambda: None)

    calls = []

    def _never() -> dict:
        calls.append(1)
        return {"reachable": True}

    res = overview._cached_probe("email_reachable", _never, run_if_missing=False)
    assert calls == []
    assert res["value"] is None
    assert res["stale"] is True

    # Forcing it does run the probe.
    forced = overview._cached_probe("email_reachable", _never, force=True)
    assert calls == [1]
    assert forced["value"] == {"reachable": True}


def test_cached_probe_serves_last_known_good_on_failure(monkeypatch):
    """A failing probe keeps the previous value visible, flagged stale."""
    monkeypatch.setattr(
        overview,
        "_probe_cache",
        {"claude_auth": {"value": {"logged_in": True}, "checked_at": time.time() - 10_000}},
    )
    monkeypatch.setattr(overview, "_probe_cache_loaded", True)
    monkeypatch.setattr(overview, "_save_probe_cache", lambda: None)

    def _boom():
        raise RuntimeError("cli exploded")

    res = overview._cached_probe("claude_auth", _boom)
    assert res["value"] == {"logged_in": True}
    assert res["stale"] is True
    assert "cli exploded" in res["error"]


# ── cron classification ──────────────────────────────────────────────────────


def _integrations_from(jobs):
    """Build the integrations section from a canned cron job list."""
    import hermes_cli.web_routers.overview as ov

    orig_probe = ov._probe_integrations
    ov._probe_integrations = lambda profile: {
        "cron": {"available": True, "jobs": jobs},
        "credentials": {"providers": []},
        "skills": {"count": 0, "names": []},
    }
    try:
        return ov._build_integrations(False, None)
    finally:
        ov._probe_integrations = orig_probe


def test_delivery_failure_is_not_reported_healthy():
    """last_status == "ok" but delivery failed → warn, with the reason surfaced."""
    section = _integrations_from([
        {
            "id": "j1", "name": "host-watchdog", "enabled": True, "paused": False,
            "last_status": "ok", "last_error": None,
            "last_delivery_error": "no delivery target resolved for deliver=discord",
        }
    ])
    assert section["status"] == "warn"
    assert "not delivering" in section["headline"]
    assert any("delivery failed" in n for n in section["notes"])


def test_failed_run_outranks_delivery_warning():
    section = _integrations_from([
        {
            "id": "j1", "name": "broken", "enabled": True, "paused": False,
            "last_status": "error", "last_error": "boom", "last_delivery_error": None,
        }
    ])
    assert section["status"] == "error"
    assert "failing" in section["headline"]


def test_healthy_cron_reports_ok():
    section = _integrations_from([
        {
            "id": "j1", "name": "fine", "enabled": True, "paused": False,
            "last_status": "ok", "last_error": None, "last_delivery_error": None,
        }
    ])
    assert section["status"] == "ok"
    assert "1 active cron job(s)" in section["headline"]


def test_paused_job_is_not_counted_active():
    section = _integrations_from([
        {
            "id": "j1", "name": "sleeping", "enabled": True, "paused": True,
            "last_status": "ok", "last_error": None, "last_delivery_error": None,
        }
    ])
    assert section["status"] == "ok"
    assert "0 active cron job(s)" in section["headline"]


# ── config summary uses cfg_get's varargs contract ───────────────────────────


def test_core_config_summary_reads_nested_keys(monkeypatch):
    """Regression: a dotted "model.provider" string never matches a nested key.

    ``cfg_get`` takes ``*keys``; passing one dotted string returns the default,
    so every config field rendered blank while the section still said "ok".
    """
    from hermes_cli import config as cfg_mod

    cfg = {
        "model": {"provider": "anthropic", "default": "claude-sonnet-5"},
        "agent": {"reasoning_effort": "none", "max_turns": 120},
        "terminal": {"backend": "local"},
        "approvals": {"mode": "manual"},
        "prompt_caching": {"cache_ttl": "1h"},
        "_config_version": 38,
    }

    monkeypatch.setattr(overview, "late_attr", lambda name: {
        "read_runtime_status": lambda *a, **k: {},
        "get_config_path": lambda: "/tmp/config.yaml",
        "load_config": lambda: cfg,
        "cfg_get": cfg_mod.cfg_get,
        "get_hermes_home": lambda: "/tmp/hermes-home",
        "detect_install_method": lambda: "git",
    }[name])

    out = overview._probe_core()
    assert out["config"]["model_provider"] == "anthropic"
    assert out["config"]["model_default"] == "claude-sonnet-5"
    assert out["config"]["terminal_backend"] == "local"
    assert out["config"]["approvals_mode"] == "manual"
    assert out["config"]["reasoning_effort"] == "none"
    assert out["config"]["max_turns"] == 120
    assert out["config"]["prompt_cache_ttl"] == "1h"
    assert out["config"]["config_version"] == 38


# ── overall rollup ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "statuses,expected",
    [
        (["ok"] * 7, "ok"),
        (["ok", "ok", "warn", "ok", "ok", "ok", "ok"], "warn"),
        (["ok", "error", "warn", "ok", "ok", "ok", "ok"], "error"),
        (["unknown"] * 7, "unknown"),
    ],
)
def test_overall_status_rollup(monkeypatch, statuses, expected):
    """Worst section status wins; all-unknown stays unknown."""
    builders = [
        "_build_core", "_build_claude_code", "_build_codex", "_build_api_usage",
        "_build_discord", "_build_email", "_build_integrations",
    ]
    for name, status in zip(builders, statuses):
        monkeypatch.setattr(
            overview, name,
            lambda *a, _s=status, **k: overview._section(_s, "x"),
        )
    result = overview._build_overview(None, 30, "")
    assert result["overall_status"] == expected
    assert list(result["sections"]) == list(overview.SECTION_KEYS)
