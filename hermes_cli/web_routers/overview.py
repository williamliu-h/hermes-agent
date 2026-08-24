"""Dashboard Overview routes — the cross-cutting "everything status" panel.

This module backs the dashboard landing page (``/`` in the SPA).  It answers a
single question: *what is the state of every part of my Hermes install right
now?*  Nothing here owns data — each section composes an existing local source
(``/api/status``'s helpers, ``state.db``, ``gateway_state.json``, the credential
pool, cron) and normalises it into one uniform envelope so the SPA renders
every card the same way and a chat bot can summarise the whole thing from one
GET.

Section envelope
----------------
Every section is ``{status, headline, detail, notes, link, last_checked,
stale}``.  ``status`` is one of ``ok`` / ``warn`` / ``error`` / ``unknown`` and
drives the badge; ``notes`` carries human-readable caveats (missing CLI,
"estimate only", never-probed) so the UI never has to hardcode them.

Probe cost + caching
--------------------
Measured on a real install: reading ``gateway_state.json`` /
``channel_directory.json`` / ``~/.codex/auth.json`` and querying ``state.db``
are all sub-10ms; ``claude auth status`` ~0.28s and ``himalaya account list``
~0.00s (it only parses config and makes NO network call).  Those are cheap
enough to run per request behind a short TTL.

The one genuinely expensive probe is IMAP reachability
(``himalaya envelope list`` ~2s).  It is therefore **never** run during a
normal page load: the email section reports the cached last-known-good result
with its timestamp, and the client asks for a fresh one explicitly via
``?refresh=email``.  That keeps the landing page fast without silently
pretending an unverified account is healthy.

Cached probe results are persisted to ``<hermes_home>/cache/overview_probes.json``
so "last checked" survives a dashboard restart (same convention as the
existing ``cache/local_endpoint_probes.json``).

Secrets
-------
No section ever emits a secret value.  ``~/.codex/auth.json`` holds
``id_token`` / ``access_token`` / ``refresh_token`` and only ``auth_mode``,
``last_refresh`` and a *boolean* for the API key are read out of it.  The
credential pool goes through web_server's existing ``_pool_entry_summary``,
which already runs every token through ``redact_key``.  ``~/.hermes/secrets/``
is never opened.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, Query
from starlette.concurrency import run_in_threadpool

from hermes_cli.web_deps import late, late_attr

_log = logging.getLogger("hermes_cli.web_server")

router = APIRouter()

# Late-bound web_server helpers — resolved at call time so this module can be
# imported while web_server is still executing, and so tests that
# ``monkeypatch.setattr(web_server, ...)`` keep working for these routes too.
_open_session_db_for_profile = late("_open_session_db_for_profile")
_config_profile_scope = late("_config_profile_scope")
_pool_entry_summary = late("_pool_entry_summary")

# Section keys, in the order the panel renders them. Mirrors the 7 areas the
# panel is specified to cover; ``integrations`` is the catch-all (cron,
# credential pool, skills).
SECTION_KEYS = (
    "core",
    "claude_code",
    "codex",
    "api_usage",
    "discord",
    "email",
    "integrations",
)

# Per-probe TTLs (seconds). Only entries listed here are cached; everything
# else is recomputed per request because it is a local file/DB read.
_PROBE_TTL = {
    "claude_auth": 60.0,
    "codex_auth": 60.0,
    "email_accounts": 120.0,
    # Deliberately long: this is the ~2s IMAP round-trip. A normal page load
    # never triggers it (see _probe_email); it is refreshed on demand.
    "email_reachable": 900.0,
}

# Hard ceiling on any shelled-out probe, so a hung CLI can never wedge a
# dashboard request. Chosen well above the measured worst case (~2s IMAP).
_PROBE_TIMEOUT = 20.0

_MAX_SESSION_ROWS = 500


# ── probe cache ──────────────────────────────────────────────────────────────

_probe_cache: Dict[str, Dict[str, Any]] = {}
_probe_cache_loaded = False
_probe_cache_lock = asyncio.Lock()


def _hermes_home() -> Path:
    """Resolve the active hermes home through web_server's own helper."""
    return Path(late_attr("get_hermes_home")())


def _cache_path() -> Path:
    return _hermes_home() / "cache" / "overview_probes.json"


def _load_probe_cache() -> None:
    """Hydrate the in-memory probe cache from disk (best effort, once)."""
    global _probe_cache_loaded
    if _probe_cache_loaded:
        return
    _probe_cache_loaded = True
    try:
        raw = json.loads(_cache_path().read_text())
        if isinstance(raw, dict):
            for key, entry in raw.items():
                if isinstance(entry, dict) and "value" in entry:
                    _probe_cache[key] = entry
    except FileNotFoundError:
        pass
    except Exception:
        _log.debug("overview: probe cache unreadable; starting empty", exc_info=True)


def _save_probe_cache() -> None:
    """Persist probe results so ``last_checked`` survives a restart."""
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_probe_cache, indent=2))
        os.replace(tmp, path)
    except Exception:
        # A read-only or full disk must not break the panel.
        _log.debug("overview: could not persist probe cache", exc_info=True)


def _cached_probe(
    key: str,
    fn: Callable[[], Any],
    *,
    force: bool = False,
    run_if_missing: bool = True,
) -> Dict[str, Any]:
    """Run ``fn`` behind the TTL cache and return a result envelope.

    The envelope is ``{value, checked_at, stale, fresh}``.  ``value`` is None
    only when the probe has genuinely never produced a result.

    ``run_if_missing=False`` means "serve cache only, never pay the cost here"
    — used for the IMAP probe so page loads stay fast.  When there is no cached
    value at all the caller gets ``value=None`` and can render "not checked
    yet" instead of blocking.
    """
    _load_probe_cache()
    ttl = _PROBE_TTL.get(key, 0.0)
    entry = _probe_cache.get(key)
    now = time.time()

    if not force and entry is not None:
        age = now - float(entry.get("checked_at") or 0)
        if age < ttl:
            return {
                "value": entry.get("value"),
                "checked_at": entry.get("checked_at"),
                "stale": False,
                "fresh": False,
            }

    if not force and not run_if_missing:
        # Serve whatever we have (possibly nothing) without paying the cost.
        if entry is None:
            return {"value": None, "checked_at": None, "stale": True, "fresh": False}
        return {
            "value": entry.get("value"),
            "checked_at": entry.get("checked_at"),
            "stale": True,
            "fresh": False,
        }

    try:
        value = fn()
    except Exception as exc:
        _log.debug("overview: probe %s failed", key, exc_info=True)
        # Keep the previous good value visible rather than blanking the card,
        # but mark it stale and attach the error.
        if entry is not None:
            return {
                "value": entry.get("value"),
                "checked_at": entry.get("checked_at"),
                "stale": True,
                "fresh": False,
                "error": str(exc),
            }
        return {
            "value": None,
            "checked_at": None,
            "stale": True,
            "fresh": False,
            "error": str(exc),
        }

    _probe_cache[key] = {"value": value, "checked_at": now}
    _save_probe_cache()
    return {"value": value, "checked_at": now, "stale": False, "fresh": True}


# ── small shared helpers ─────────────────────────────────────────────────────


def _iso(ts: Optional[float]) -> Optional[str]:
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except Exception:
        return None


def _run_cli(cmd: List[str], timeout: float = _PROBE_TIMEOUT) -> Dict[str, Any]:
    """Run a local CLI and return ``{ok, exit_code, stdout, stderr, missing}``.

    ``missing`` distinguishes "this integration isn't installed" (a neutral
    state the panel reports as unknown) from "it ran and failed" (an error).
    """
    if shutil.which(cmd[0]) is None:
        return {"ok": False, "missing": True, "exit_code": None, "stdout": "", "stderr": ""}
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            # Never inherit a TTY; these are non-interactive status reads.
            stdin=subprocess.DEVNULL,
        )
        return {
            "ok": proc.returncode == 0,
            "missing": False,
            "exit_code": proc.returncode,
            "stdout": proc.stdout or "",
            "stderr": (proc.stderr or "")[:500],
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "missing": False,
            "exit_code": None,
            "stdout": "",
            "stderr": f"timed out after {timeout:.0f}s",
        }


def _section(
    status: str,
    headline: str,
    detail: Optional[Dict[str, Any]] = None,
    *,
    notes: Optional[List[str]] = None,
    link: Optional[str] = None,
    checked_at: Optional[float] = None,
    stale: bool = False,
) -> Dict[str, Any]:
    return {
        "status": status,
        "headline": headline,
        "detail": detail or {},
        "notes": notes or [],
        "link": link,
        "last_checked": _iso(checked_at if checked_at is not None else time.time()),
        "stale": stale,
    }


# ── § 1  Hermes core / structure ─────────────────────────────────────────────


def _probe_core() -> Dict[str, Any]:
    from hermes_cli import __version__, __release_date__

    read_runtime_status = late_attr("read_runtime_status")
    get_config_path = late_attr("get_config_path")
    load_config = late_attr("load_config")
    cfg_get = late_attr("cfg_get")

    home = _hermes_home()
    notes: List[str] = []

    runtime: Dict[str, Any] = {}
    try:
        runtime = read_runtime_status() or {}
    except Exception:
        notes.append("Could not read gateway_state.json.")

    pid = runtime.get("pid")
    gateway_state = runtime.get("gateway_state")
    running = False
    uptime_seconds: Optional[float] = None
    if isinstance(pid, int) and pid > 0:
        try:
            import psutil

            proc = psutil.Process(pid)
            running = proc.is_running()
            if running:
                uptime_seconds = max(0.0, time.time() - proc.create_time())
        except Exception:
            # psutil missing or process gone — fall back to the state file's
            # own claim rather than asserting a state we could not verify.
            running = gateway_state == "running"
            if uptime_seconds is None:
                notes.append("Gateway uptime unavailable (process not inspectable).")

    cfg: Dict[str, Any] = {}
    try:
        cfg = load_config() or {}
    except Exception:
        notes.append("Could not load config.yaml.")

    def _cfg(path: str, default: Any = None) -> Any:
        # cfg_get takes the key path as *varargs*, not a dotted string — a
        # dotted string is looked up as one literal key and always misses.
        try:
            return cfg_get(cfg, *path.split("."), default=default)
        except Exception:
            return default

    active_profile = "default"
    try:
        from hermes_cli import profiles as profiles_mod

        active_profile = (
            getattr(profiles_mod, "get_active_profile_name", lambda: None)()
            or getattr(profiles_mod, "get_active_profile", lambda: None)()
            or "default"
        )
    except Exception:
        pass

    install_method = None
    try:
        install_method = late_attr("detect_install_method")()
    except Exception:
        pass

    if gateway_state == "running" and not running:
        notes.append(
            "gateway_state.json says running but the recorded PID is not alive — stale state file."
        )

    return {
        "version": __version__,
        "release_date": __release_date__,
        "install_method": install_method,
        "hermes_home": str(home),
        "config_path": str(get_config_path()),
        "active_profile": active_profile,
        "gateway": {
            "running": running,
            "state": gateway_state,
            "pid": pid if isinstance(pid, int) else None,
            "uptime_seconds": uptime_seconds,
            "active_agents": runtime.get("active_agents"),
            "code_version": runtime.get("code_version"),
            "code_sha": runtime.get("code_sha"),
            "exit_reason": runtime.get("exit_reason"),
            "updated_at": runtime.get("updated_at"),
        },
        "config": {
            "model_provider": _cfg("model.provider"),
            "model_default": _cfg("model.default"),
            "reasoning_effort": _cfg("agent.reasoning_effort"),
            "max_turns": _cfg("agent.max_turns"),
            "terminal_backend": _cfg("terminal.backend"),
            "approvals_mode": _cfg("approvals.mode"),
            "prompt_cache_ttl": _cfg("prompt_caching.cache_ttl"),
            "config_version": cfg.get("_config_version") if isinstance(cfg, dict) else None,
        },
        "notes": notes,
    }


def _build_core(force: bool) -> Dict[str, Any]:
    try:
        data = _probe_core()
    except Exception as exc:
        _log.exception("overview: core section failed")
        return _section("unknown", "Core status unavailable", notes=[str(exc)], link="/system")

    gw = data["gateway"]
    notes = list(data.pop("notes", []))
    if gw["running"]:
        status = "ok"
        headline = f"Gateway running · v{data['version']}"
    elif gw["state"] in {"startup_failed", "error"}:
        status = "error"
        headline = f"Gateway {gw['state']}"
    else:
        status = "warn"
        headline = "Gateway stopped"
    return _section(status, headline, data, notes=notes, link="/system")


# ── § 2  Claude Code CLI ─────────────────────────────────────────────────────


def _probe_claude_auth() -> Dict[str, Any]:
    """``claude auth status`` emits JSON on stdout (``--text`` is the human form)."""
    res = _run_cli(["claude", "auth", "status"])
    if res["missing"]:
        return {"installed": False}

    out: Dict[str, Any] = {"installed": True, "exit_code": res["exit_code"]}
    parsed: Optional[Dict[str, Any]] = None
    try:
        candidate = json.loads(res["stdout"].strip() or "{}")
        if isinstance(candidate, dict):
            parsed = candidate
    except Exception:
        pass

    if parsed is None:
        # Older CLIs print only the human table. Keep the raw text so the panel
        # can still show something truthful rather than claiming logged-out.
        out["logged_in"] = None
        out["raw"] = res["stdout"].strip()[:400]
        out["parse_failed"] = True
        return out

    out.update(
        {
            "logged_in": bool(parsed.get("loggedIn")),
            "auth_method": parsed.get("authMethod"),
            "api_provider": parsed.get("apiProvider"),
            "email": parsed.get("email"),
            "org_name": parsed.get("orgName"),
            "org_id": parsed.get("orgId"),
            "subscription_type": parsed.get("subscriptionType"),
        }
    )

    version = _run_cli(["claude", "--version"], timeout=15.0)
    if version["ok"]:
        out["cli_version"] = version["stdout"].strip()[:80]

    # Delegated-task visibility: Claude Code writes worktrees under the repo
    # it runs in, which is the only locally discoverable trace of CLI runs.
    return out


def _count_claude_worktrees() -> Optional[int]:
    """Best-effort count of Claude Code delegated-task worktrees."""
    try:
        root = _hermes_home() / "hermes-agent" / ".claude" / "worktrees"
        if not root.is_dir():
            return None
        return sum(1 for p in root.iterdir() if p.is_dir())
    except Exception:
        return None


def _build_claude_code(force: bool) -> Dict[str, Any]:
    probe = _cached_probe("claude_auth", _probe_claude_auth, force=force)
    val = probe["value"] or {}
    notes: List[str] = []

    if not val:
        return _section(
            "unknown",
            "Claude Code status unknown",
            notes=["Could not read `claude auth status`."],
            checked_at=probe["checked_at"],
            stale=probe["stale"],
        )
    if not val.get("installed"):
        return _section(
            "unknown",
            "Claude Code CLI not installed",
            {"installed": False},
            notes=["`claude` is not on PATH."],
            checked_at=probe["checked_at"],
            stale=probe["stale"],
        )

    detail = dict(val)
    worktrees = _count_claude_worktrees()
    detail["delegated_worktrees"] = worktrees

    # Documented gap: the CLI reports no credential expiry, so the panel must
    # not imply it knows one.
    notes.append("`claude auth status` reports no token expiry, so none is shown.")
    if worktrees is None:
        notes.append(
            "No local Claude Code invocation log exists; delegated-task history is not tracked."
        )

    if val.get("parse_failed"):
        return _section(
            "warn",
            "Claude Code auth unparseable",
            detail,
            notes=notes + ["CLI did not return JSON."],
            checked_at=probe["checked_at"],
            stale=probe["stale"],
        )
    if val.get("logged_in"):
        org = val.get("org_name") or val.get("auth_method") or "logged in"
        sub = val.get("subscription_type")
        headline = f"Logged in · {org}" + (f" ({sub})" if sub else "")
        status = "ok"
    else:
        headline = "Not logged in"
        status = "warn"
    return _section(
        status,
        headline,
        detail,
        notes=notes,
        checked_at=probe["checked_at"],
        stale=probe["stale"],
    )


# ── § 3  ChatGPT / Codex CLI ─────────────────────────────────────────────────


def _probe_codex(home: Optional[Path] = None) -> Dict[str, Any]:
    """Read ``~/.codex/auth.json`` — status fields only, never the tokens.

    Note this is the *standalone* ``codex`` CLI's own login. Hermes' internal
    Codex provider credentials live in ``~/.hermes/auth.json`` and surface via
    the credential-pool section instead; the two are independent.

    ``home`` is injectable so tests can point at a fixture directory instead of
    monkeypatching ``pathlib.Path.home`` process-wide.
    """
    path = (home or Path.home()) / ".codex" / "auth.json"
    out: Dict[str, Any] = {"auth_file": str(path)}

    cli = _run_cli(["codex", "--version"], timeout=15.0)
    out["installed"] = not cli["missing"]
    if cli["ok"]:
        out["cli_version"] = cli["stdout"].strip()[:80]

    if not path.exists():
        out["logged_in"] = False
        out["reason"] = "no auth.json"
        return out

    try:
        raw = json.loads(path.read_text())
    except Exception as exc:
        out["logged_in"] = None
        out["reason"] = f"auth.json unreadable: {exc}"
        return out

    tokens = raw.get("tokens") if isinstance(raw.get("tokens"), dict) else {}
    # Only booleans and non-secret metadata leave this function.
    out.update(
        {
            "auth_mode": raw.get("auth_mode"),
            "last_refresh": raw.get("last_refresh"),
            "has_api_key": bool(raw.get("OPENAI_API_KEY")),
            "has_access_token": bool(tokens.get("access_token")),
            "has_refresh_token": bool(tokens.get("refresh_token")),
            "account_id_present": bool(tokens.get("account_id")),
            "logged_in": bool(tokens.get("access_token") or raw.get("OPENAI_API_KEY")),
        }
    )
    return out


def _build_codex(force: bool) -> Dict[str, Any]:
    probe = _cached_probe("codex_auth", _probe_codex, force=force)
    val = probe["value"] or {}
    if not val:
        return _section("unknown", "Codex status unknown", checked_at=probe["checked_at"])

    notes = [
        "This is the standalone `codex` CLI login. Hermes' own Codex provider "
        "credentials, if configured, appear under Credentials.",
    ]
    if not val.get("installed"):
        notes.append("`codex` is not on PATH.")

    stale_days: Optional[float] = None
    last_refresh = val.get("last_refresh")
    if last_refresh:
        try:
            ts = datetime.fromisoformat(str(last_refresh).replace("Z", "+00:00"))
            stale_days = (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0
        except Exception:
            pass
    detail = dict(val)
    detail["last_refresh_age_days"] = round(stale_days, 2) if stale_days is not None else None

    if val.get("logged_in"):
        mode = val.get("auth_mode") or "unknown mode"
        headline = f"Logged in via {mode}"
        status = "ok"
        if stale_days is not None and stale_days > 30:
            status = "warn"
            notes.append(f"Token last refreshed {stale_days:.0f} days ago.")
    elif val.get("logged_in") is None:
        headline = "Codex auth unreadable"
        status = "warn"
    else:
        headline = "Not logged in"
        status = "warn" if val.get("installed") else "unknown"
    return _section(
        status, headline, detail, notes=notes,
        checked_at=probe["checked_at"], stale=probe["stale"], link="/env",
    )


# ── § 4  Anthropic API usage / spend ─────────────────────────────────────────


def _usage_rollup(db: Any, days: int) -> Dict[str, Any]:
    """Token/cost rollup straight off ``state.db`` for the trailing window."""
    cutoff = time.time() - days * 86400

    totals = dict(
        db._conn.execute(
            """
            SELECT COUNT(*)                                AS sessions,
                   COALESCE(SUM(input_tokens), 0)          AS input_tokens,
                   COALESCE(SUM(output_tokens), 0)         AS output_tokens,
                   COALESCE(SUM(cache_read_tokens), 0)     AS cache_read_tokens,
                   COALESCE(SUM(cache_write_tokens), 0)    AS cache_write_tokens,
                   COALESCE(SUM(reasoning_tokens), 0)      AS reasoning_tokens,
                   COALESCE(SUM(estimated_cost_usd), 0)    AS estimated_cost_usd,
                   COALESCE(SUM(actual_cost_usd), 0)       AS actual_cost_usd,
                   COALESCE(SUM(api_call_count), 0)        AS api_calls,
                   COALESCE(SUM(tool_call_count), 0)       AS tool_calls,
                   COALESCE(SUM(message_count), 0)         AS messages
            FROM sessions WHERE started_at > ?
            """,
            (cutoff,),
        ).fetchone()
    )

    by_model = [
        dict(r)
        for r in db._conn.execute(
            """
            SELECT model,
                   COALESCE(billing_provider, '')          AS billing_provider,
                   COUNT(*)                                AS sessions,
                   COALESCE(SUM(input_tokens), 0)          AS input_tokens,
                   COALESCE(SUM(output_tokens), 0)         AS output_tokens,
                   COALESCE(SUM(cache_read_tokens), 0)     AS cache_read_tokens,
                   COALESCE(SUM(estimated_cost_usd), 0)    AS estimated_cost_usd
            FROM sessions
            WHERE started_at > ? AND model IS NOT NULL AND model != ''
            GROUP BY model, billing_provider
            ORDER BY estimated_cost_usd DESC
            """,
            (cutoff,),
        ).fetchall()
    ]

    # Auxiliary (non-main-agent) spend, e.g. background_review / compression /
    # title_generation. It is billed separately from the session counters, so
    # the panel shows it as its own line rather than folding it into the total
    # silently.
    by_task = [
        dict(r)
        for r in db._conn.execute(
            """
            SELECT smu.task,
                   smu.model,
                   COUNT(*)                                    AS rows,
                   COALESCE(SUM(smu.input_tokens), 0)          AS input_tokens,
                   COALESCE(SUM(smu.output_tokens), 0)         AS output_tokens,
                   COALESCE(SUM(smu.estimated_cost_usd), 0)    AS estimated_cost_usd
            FROM session_model_usage smu
            JOIN sessions s ON s.id = smu.session_id
            WHERE s.started_at > ? AND smu.task != ''
            GROUP BY smu.task, smu.model
            ORDER BY estimated_cost_usd DESC
            """,
            (cutoff,),
        ).fetchall()
    ]

    by_source = [
        dict(r)
        for r in db._conn.execute(
            """
            SELECT source,
                   COUNT(*)                                AS sessions,
                   COALESCE(SUM(estimated_cost_usd), 0)    AS estimated_cost_usd,
                   COALESCE(SUM(message_count), 0)         AS messages
            FROM sessions WHERE started_at > ?
            GROUP BY source ORDER BY estimated_cost_usd DESC
            """,
            (cutoff,),
        ).fetchall()
    ]

    unpriced = db._conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE started_at > ? "
        "AND (cost_status IS NULL OR cost_status = '' OR cost_status = 'unknown')",
        (cutoff,),
    ).fetchone()[0]

    aux_cost = sum(float(r.get("estimated_cost_usd") or 0) for r in by_task)
    return {
        "period_days": days,
        "totals": totals,
        "aux_estimated_cost_usd": aux_cost,
        "combined_estimated_cost_usd": float(totals.get("estimated_cost_usd") or 0) + aux_cost,
        "by_model": by_model,
        "by_task": by_task,
        "by_source": by_source,
        "unpriced_sessions": unpriced,
    }


def _session_rows(db: Any, days: int, limit: int) -> List[Dict[str, Any]]:
    """Per-session drill-down rows, newest first."""
    cutoff = time.time() - days * 86400
    rows = [
        dict(r)
        for r in db._conn.execute(
            """
            SELECT id, title, source, model,
                   COALESCE(billing_provider, '') AS billing_provider,
                   started_at, ended_at, end_reason,
                   COALESCE(message_count, 0)       AS message_count,
                   COALESCE(tool_call_count, 0)     AS tool_call_count,
                   COALESCE(api_call_count, 0)      AS api_call_count,
                   COALESCE(input_tokens, 0)        AS input_tokens,
                   COALESCE(output_tokens, 0)       AS output_tokens,
                   COALESCE(cache_read_tokens, 0)   AS cache_read_tokens,
                   COALESCE(cache_write_tokens, 0)  AS cache_write_tokens,
                   COALESCE(reasoning_tokens, 0)    AS reasoning_tokens,
                   COALESCE(estimated_cost_usd, 0)  AS estimated_cost_usd,
                   COALESCE(actual_cost_usd, 0)     AS actual_cost_usd,
                   cost_status, chat_type, display_name
            FROM sessions
            WHERE started_at > ?
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (cutoff, limit),
        ).fetchall()
    ]

    if not rows:
        return rows

    # Attach per-session auxiliary spend so a row's cost reconciles with the
    # totals card instead of appearing to under-report.
    placeholders = ",".join("?" for _ in rows)
    aux: Dict[str, float] = {}
    try:
        for sid, cost in db._conn.execute(
            f"SELECT session_id, COALESCE(SUM(estimated_cost_usd), 0) "
            f"FROM session_model_usage WHERE task != '' AND session_id IN ({placeholders}) "
            f"GROUP BY session_id",
            [r["id"] for r in rows],
        ).fetchall():
            aux[sid] = float(cost or 0)
    except Exception:
        _log.debug("overview: aux per-session rollup failed", exc_info=True)

    for row in rows:
        row["aux_estimated_cost_usd"] = aux.get(row["id"], 0.0)
        row["started_at_iso"] = _iso(row.get("started_at"))
        row["ended_at_iso"] = _iso(row.get("ended_at"))
    return rows


def _probe_api_usage(profile: Optional[str], days: int) -> Dict[str, Any]:
    db = _open_session_db_for_profile(profile, read_only=True)
    try:
        data = _usage_rollup(db, days)
        data["recent_sessions"] = _session_rows(db, days, 25)
        return data
    finally:
        db.close()


def _anthropic_key_state() -> Dict[str, Any]:
    """Masked view of which providers have credentials — never a raw value."""
    out: Dict[str, Any] = {"providers": []}
    try:
        from agent.credential_pool import load_pool
        from hermes_cli.auth import read_credential_pool

        for provider_id in sorted((read_credential_pool() or {}).keys()):
            try:
                entries = load_pool(provider_id).entries()
            except Exception:
                continue
            if not entries:
                continue
            out["providers"].append(
                {
                    "provider": provider_id,
                    "count": len(entries),
                    # _pool_entry_summary already runs redact_key on tokens.
                    "entries": [
                        _pool_entry_summary(e, i) for i, e in enumerate(entries, start=1)
                    ],
                }
            )
    except Exception:
        _log.debug("overview: credential pool read failed", exc_info=True)
    return out


def _build_api_usage(force: bool, profile: Optional[str], days: int) -> Dict[str, Any]:
    try:
        data = _probe_api_usage(profile, days)
    except Exception as exc:
        _log.exception("overview: api_usage section failed")
        return _section("unknown", "Usage data unavailable", notes=[str(exc)], link="/analytics")

    creds = _anthropic_key_state()
    data["credentials"] = creds
    anthropic = next(
        (p for p in creds["providers"] if p["provider"] == "anthropic"), None
    )
    data["anthropic_credentials_configured"] = bool(anthropic)

    notes = [
        "Spend is a LOCAL ESTIMATE computed from token counts and model pricing. "
        "Anthropic exposes no public balance API, so check console.anthropic.com "
        "for authoritative billing and account balance.",
    ]
    if data["unpriced_sessions"]:
        notes.append(
            f"{data['unpriced_sessions']} session(s) have no pricing data and are "
            "excluded from the estimate."
        )
    if not anthropic:
        notes.append("No pooled Anthropic credential is configured.")

    cost = data["combined_estimated_cost_usd"]
    headline = f"~${cost:,.2f} est. over {days}d · {data['totals']['sessions']} sessions"
    status = "ok" if anthropic else "warn"
    return _section(status, headline, data, notes=notes, link="/analytics")


# ── § 5  Discord ─────────────────────────────────────────────────────────────


def _probe_discord() -> Dict[str, Any]:
    read_runtime_status = late_attr("read_runtime_status")
    home = _hermes_home()
    out: Dict[str, Any] = {"channels": [], "dms": []}

    try:
        runtime = read_runtime_status() or {}
    except Exception:
        runtime = {}
    entry = ((runtime.get("platforms") or {}).get("discord")) or {}
    out["platform"] = {
        "state": entry.get("state"),
        "error_code": entry.get("error_code"),
        "error_message": entry.get("error_message"),
        "updated_at": entry.get("updated_at"),
        "needs_attention": entry.get("needs_attention"),
        "retrying_since": entry.get("retrying_since"),
    }
    out["configured"] = bool(entry)

    try:
        raw = json.loads((home / "channel_directory.json").read_text())
        out["directory_updated_at"] = raw.get("updated_at")
        for item in (raw.get("platforms") or {}).get("discord") or []:
            if not isinstance(item, dict):
                continue
            record = {
                "id": item.get("id"),
                "name": item.get("name"),
                "guild": item.get("guild"),
                "type": item.get("type"),
            }
            (out["dms"] if item.get("type") == "dm" else out["channels"]).append(record)
    except FileNotFoundError:
        out["directory_missing"] = True
    except Exception as exc:
        out["directory_error"] = str(exc)
    return out


def _build_discord(force: bool) -> Dict[str, Any]:
    try:
        data = _probe_discord()
    except Exception as exc:
        return _section("unknown", "Discord status unavailable", notes=[str(exc)], link="/channels")

    plat = data["platform"]
    state = plat.get("state")
    notes: List[str] = []
    n_ch, n_dm = len(data["channels"]), len(data["dms"])

    if not data["configured"]:
        status, headline = "unknown", "Discord not configured"
        notes.append("No discord entry in gateway_state.json.")
    elif state == "connected":
        status = "ok"
        headline = f"Connected · {n_ch} channel(s), {n_dm} DM(s)"
    elif state in {"fatal", "error"}:
        status = "error"
        headline = f"Discord {state}"
        if plat.get("error_message"):
            notes.append(str(plat["error_message"]))
    else:
        status = "warn"
        headline = f"Discord {state or 'disconnected'}"

    if plat.get("needs_attention"):
        status = "error" if status != "error" else status
        notes.append("Gateway flagged this platform as needing attention.")
    if data.get("directory_missing"):
        notes.append("channel_directory.json not found — known-channel list unavailable.")

    return _section(status, headline, data, notes=notes, link="/channels")


# ── § 6  Email (himalaya) ────────────────────────────────────────────────────


def _probe_email_accounts() -> Dict[str, Any]:
    """Parse ``himalaya account list``.

    IMPORTANT: this is a *config-only* read — it makes no network call and so
    says nothing about reachability. Reachability is a separate, cached probe.
    """
    res = _run_cli(["himalaya", "account", "list"])
    if res["missing"]:
        return {"installed": False, "accounts": []}

    accounts: List[Dict[str, Any]] = []
    for line in res["stdout"].splitlines():
        # Table rows look like: │ hermes ┆ imap, smtp ┆ yes │
        if "┆" not in line:
            continue
        cells = [c.strip() for c in line.strip().strip("│").split("┆")]
        if len(cells) < 3 or cells[0].upper() == "NAME":
            continue
        accounts.append(
            {
                "name": cells[0],
                "backends": [b.strip() for b in cells[1].split(",") if b.strip()],
                "default": cells[2].lower().startswith("y"),
            }
        )
    return {
        "installed": True,
        "ok": res["ok"],
        "accounts": accounts,
        "stderr": res["stderr"] if not res["ok"] else "",
    }


def _probe_email_reachable() -> Dict[str, Any]:
    """Live IMAP round-trip (~2s). Only ever run on an explicit refresh."""
    started = time.time()
    res = _run_cli(["himalaya", "envelope", "list", "-s", "1"])
    elapsed = time.time() - started
    if res["missing"]:
        return {"reachable": None, "reason": "himalaya not installed"}
    return {
        "reachable": bool(res["ok"]),
        "elapsed_seconds": round(elapsed, 2),
        # Never echo message bodies/subjects — only whether the call succeeded.
        "reason": None if res["ok"] else (res["stderr"] or "IMAP command failed")[:300],
    }


def _build_email(force: bool) -> Dict[str, Any]:
    accounts = _cached_probe("email_accounts", _probe_email_accounts, force=force)
    # run_if_missing=False is the whole point: a page load must not pay ~2s of
    # IMAP latency. The card shows last-known-good until the user refreshes.
    reach = _cached_probe(
        "email_reachable", _probe_email_reachable, force=force, run_if_missing=False
    )

    acc = accounts["value"] or {}
    notes: List[str] = []
    detail: Dict[str, Any] = {
        "installed": acc.get("installed"),
        "accounts": acc.get("accounts") or [],
        "accounts_checked_at": _iso(accounts["checked_at"]),
        "reachability": reach["value"],
        "reachability_checked_at": _iso(reach["checked_at"]),
        "reachability_stale": reach["stale"],
    }

    if not acc:
        return _section(
            "unknown", "Email status unknown",
            detail, notes=["Could not run `himalaya account list`."],
            checked_at=accounts["checked_at"], link="/config",
        )
    if not acc.get("installed"):
        return _section(
            "unknown", "himalaya not installed",
            detail, notes=["`himalaya` is not on PATH."],
            checked_at=accounts["checked_at"], link="/config",
        )

    names = [a["name"] for a in detail["accounts"]]
    if not names:
        return _section(
            "warn", "No email account configured",
            detail, notes=["`himalaya account list` returned no accounts."],
            checked_at=accounts["checked_at"], link="/config",
        )

    notes.append(
        "Account list is read from himalaya config only — it does not prove the "
        "mailbox is reachable. Use Check now for a live IMAP test."
    )
    reach_val = reach["value"]
    if reach_val is None:
        status = "warn"
        headline = f"{', '.join(names)} configured · not yet verified"
        notes.append("IMAP has never been checked from this dashboard.")
    elif reach_val.get("reachable"):
        status = "ok"
        headline = f"{', '.join(names)} · IMAP reachable"
        if reach["stale"]:
            notes.append("Reachability result is cached; press Check now to re-test.")
    else:
        status = "error"
        headline = f"{', '.join(names)} · IMAP unreachable"
        if reach_val.get("reason"):
            notes.append(str(reach_val["reason"]))

    return _section(
        status, headline, detail, notes=notes,
        checked_at=accounts["checked_at"], link="/config",
    )


# ── § 7  Cron / credentials / skills ─────────────────────────────────────────


def _probe_integrations(profile: Optional[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}

    # Cron — reuse the dashboard's own sync worker so this can never disagree
    # with the Cron page. It returns a plain list of job dicts.
    try:
        jobs_payload = late_attr("_list_cron_jobs_sync")("all")
        jobs = jobs_payload.get("jobs") if isinstance(jobs_payload, dict) else jobs_payload
        summary = []
        for job in jobs or []:
            if not isinstance(job, dict):
                continue
            summary.append(
                {
                    "id": job.get("id"),
                    "name": job.get("name"),
                    # ``schedule`` itself is a dict; the display string is
                    # precomputed alongside it.
                    "schedule": job.get("schedule_display"),
                    "state": job.get("state"),
                    "enabled": bool(job.get("enabled")),
                    # There is no ``paused`` boolean — pausing sets paused_at.
                    "paused": bool(job.get("paused_at")),
                    "paused_reason": job.get("paused_reason"),
                    "next_run": job.get("next_run_at"),
                    "last_run": job.get("last_run_at"),
                    "last_status": job.get("last_status"),
                    "last_error": job.get("last_error"),
                    # Distinct from last_error: the job body can succeed while
                    # delivery of its output fails, which is silent otherwise.
                    "last_delivery_error": job.get("last_delivery_error"),
                    "failure_streak": job.get("failure_streak") or 0,
                    "delivery": job.get("deliver"),
                    "runs_completed": (job.get("repeat") or {}).get("completed"),
                    "profile": job.get("profile_name") or job.get("profile"),
                }
            )
        out["cron"] = {"available": True, "jobs": summary}
    except Exception as exc:
        _log.debug("overview: cron rollup failed", exc_info=True)
        out["cron"] = {"available": False, "error": str(exc), "jobs": []}

    out["credentials"] = _anthropic_key_state()

    # Skills — cheap count only; the Skills page owns the detail.
    try:
        skills_root = _hermes_home() / "skills"
        names = (
            sorted(p.name for p in skills_root.iterdir() if p.is_dir())
            if skills_root.is_dir()
            else []
        )
        out["skills"] = {"count": len(names), "names": names[:40]}
    except Exception:
        out["skills"] = {"count": None, "names": []}
    return out


def _build_integrations(force: bool, profile: Optional[str]) -> Dict[str, Any]:
    try:
        data = _probe_integrations(profile)
    except Exception as exc:
        return _section("unknown", "Integrations unavailable", notes=[str(exc)], link="/cron")

    jobs = data["cron"]["jobs"]
    # A failed run and a failed *delivery* are different problems: the second
    # means the job worked but its output never reached the user, so it must
    # not be reported as healthy.
    failing = [
        j for j in jobs
        if (j.get("last_status") or "").lower() in {"error", "failed", "fail"}
        or j.get("last_error")
    ]
    undelivered = [
        j for j in jobs if j.get("last_delivery_error") and j not in failing
    ]
    active = [j for j in jobs if j.get("enabled") and not j.get("paused")]
    providers = data["credentials"]["providers"]
    notes: List[str] = []
    cred_summary = f"{len(providers)} credential provider(s)"

    if not data["cron"]["available"]:
        status, headline = "unknown", "Cron status unavailable"
        notes.append(str(data["cron"].get("error") or "cron rollup failed"))
    elif failing:
        status = "error"
        headline = f"{len(failing)} cron job(s) failing · {cred_summary}"
        for job in failing[:3]:
            notes.append(
                f"{job.get('name') or job.get('id')}: "
                f"{job.get('last_error') or job.get('last_status')}"
            )
    elif undelivered:
        status = "warn"
        headline = f"{len(undelivered)} cron job(s) not delivering · {cred_summary}"
        for job in undelivered[:3]:
            notes.append(
                f"{job.get('name') or job.get('id')} ran OK but delivery failed: "
                f"{job.get('last_delivery_error')}"
            )
    else:
        status = "ok"
        headline = f"{len(active)} active cron job(s) · {cred_summary}"

    notes.append("Credential values are never returned by this API — identifiers are masked.")
    return _section(status, headline, data, notes=notes, link="/cron")


# ── routes ───────────────────────────────────────────────────────────────────


def _parse_refresh(refresh: str) -> Dict[str, bool]:
    """``?refresh=`` accepts ``all`` or a comma-separated list of section keys."""
    raw = {p.strip() for p in (refresh or "").split(",") if p.strip()}
    if not raw:
        return {k: False for k in SECTION_KEYS}
    if "all" in raw or "1" in raw or "true" in raw:
        return {k: True for k in SECTION_KEYS}
    return {k: (k in raw) for k in SECTION_KEYS}


def _build_overview(profile: Optional[str], days: int, refresh: str) -> Dict[str, Any]:
    force = _parse_refresh(refresh)
    sections = {
        "core": _build_core(force["core"]),
        "claude_code": _build_claude_code(force["claude_code"]),
        "codex": _build_codex(force["codex"]),
        "api_usage": _build_api_usage(force["api_usage"], profile, days),
        "discord": _build_discord(force["discord"]),
        "email": _build_email(force["email"]),
        "integrations": _build_integrations(force["integrations"], profile),
    }

    counts: Dict[str, int] = {}
    for sec in sections.values():
        counts[sec["status"]] = counts.get(sec["status"], 0) + 1
    if counts.get("error"):
        overall = "error"
    elif counts.get("warn"):
        overall = "warn"
    elif counts.get("unknown") and not counts.get("ok"):
        overall = "unknown"
    else:
        overall = "ok"

    return {
        "generated_at": _iso(time.time()),
        "period_days": days,
        "overall_status": overall,
        "status_counts": counts,
        "section_order": list(SECTION_KEYS),
        "sections": sections,
    }


@router.get("/api/overview")
async def get_overview(
    profile: Optional[str] = None,
    days: int = Query(30, ge=1, le=365),
    refresh: str = Query("", description="'all' or comma-separated section keys"),
):
    """Aggregate status for every inspectable part of this Hermes install.

    Read-only and secret-free: safe for the SPA landing page and for a chat
    bot to summarise. Slow probes are cached — see the module docstring.
    """
    scope = None
    requested = (profile or "").strip()
    if requested and requested.lower() != "current":
        scope = _config_profile_scope(requested)
        scope.__enter__()
    try:
        async with _probe_cache_lock:
            return await run_in_threadpool(_build_overview, profile, days, refresh)
    finally:
        if scope is not None:
            scope.__exit__(None, None, None)


@router.get("/api/overview/sessions")
async def get_overview_sessions(
    profile: Optional[str] = None,
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(200, ge=1, le=_MAX_SESSION_ROWS),
):
    """Per-session token/cost rows for the usage drill-down table.

    Separate from ``/api/overview`` so the landing page stays small and the
    table can re-query on its own window/row-count without refetching the
    whole panel.
    """

    def _work() -> Dict[str, Any]:
        db = _open_session_db_for_profile(profile, read_only=True)
        try:
            rows = _session_rows(db, days, limit)
            return {
                "period_days": days,
                "limit": limit,
                "count": len(rows),
                "truncated": len(rows) >= limit,
                "sessions": rows,
            }
        finally:
            db.close()

    scope = None
    requested = (profile or "").strip()
    if requested and requested.lower() != "current":
        scope = _config_profile_scope(requested)
        scope.__enter__()
    try:
        return await run_in_threadpool(_work)
    finally:
        if scope is not None:
            scope.__exit__(None, None, None)
