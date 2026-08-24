"""Bare platform-name delivery (``deliver='discord'``) with no captured origin.

Reported against a live install: a CLI-created ``no_agent`` job with
``deliver='discord'`` and ``origin: null`` ran fine 46 times and stamped
``last_delivery_error = "no delivery target resolved for deliver=discord"``
on every one of them.

The resolution path itself is correct — a bare platform name already falls
back to that platform's home channel without needing an origin (that path is
covered by ``test_relay_fronted_delivery.py``). The install simply had no
Discord home channel configured, which is a real misconfiguration and must
keep failing. What made it a 46-run silent failure is that neither the
per-run error nor the create-time notice said *which* knob was missing:

* the run error named only the symptom, so it read as an internal bug rather
  than "you never ran /sethome";
* the create-time notice actively misdirected — it told the user to set
  ``deliver`` to "a gateway-connected platform, e.g. deliver='telegram'",
  which is exactly what they had already done with ``deliver='discord'``.

These tests pin both diagnostics, and pin that the resolvable-home-channel
case still succeeds while the unresolvable one still reports an error.
"""

from unittest.mock import MagicMock, patch

import pytest

from cron.scheduler import (
    _deliver_result,
    _resolve_delivery_targets,
    _unresolved_delivery_reason,
)
from gateway.config import HomeChannel, Platform
from tools.cronjob_tools import _local_delivery_notice

HOME_CHAT_ID = "1540906576745996321"


def _job(deliver="discord", origin=None):
    """The shape of the reported job: bare platform name, explicit null origin."""
    return {
        "id": "e59ace9b1da4",
        "name": "host-watchdog",
        "script": "watchdog-host.sh",
        "no_agent": True,
        "deliver": deliver,
        "origin": origin,
    }


def _config_with_discord_home(chat_id=HOME_CHAT_ID):
    """Discord natively enabled (so the send path's enabled gate passes) with a
    home channel that lives only in config.yaml."""
    from gateway.config import PlatformConfig

    home = HomeChannel(platform=Platform.DISCORD, chat_id=chat_id, name="general")
    config = MagicMock()
    config.platforms = {Platform.DISCORD: PlatformConfig(enabled=True)}
    config.get_home_channel = lambda p: home if p == Platform.DISCORD else None
    return config


def _config_without_home():
    config = MagicMock()
    config.platforms = {}
    config.get_home_channel = lambda p: None
    return config


@pytest.fixture()
def no_home_env(monkeypatch):
    """No env mirror for any platform, so config.yaml is the only home source."""
    for var in (
        "DISCORD_HOME_CHANNEL",
        "DISCORD_HOME_CHANNEL_THREAD_ID",
        "TELEGRAM_HOME_CHANNEL",
        "SLACK_HOME_CHANNEL",
    ):
        monkeypatch.delenv(var, raising=False)


class TestBarePlatformResolution:
    def test_null_origin_resolves_via_home_channel(self, no_home_env):
        """deliver='discord' + origin=None + a home channel → concrete target."""
        with patch(
            "gateway.config.load_gateway_config",
            return_value=_config_with_discord_home(),
        ):
            assert _resolve_delivery_targets(_job()) == [
                {
                    "platform": "discord",
                    "chat_id": HOME_CHAT_ID,
                    "thread_id": None,
                }
            ]

    def test_null_origin_without_home_channel_resolves_nothing(self, no_home_env):
        """The legitimate-failure case: nothing to resolve, so no target."""
        with patch(
            "gateway.config.load_gateway_config",
            return_value=_config_without_home(),
        ):
            assert _resolve_delivery_targets(_job()) == []


class TestDeliveryOutcome:
    def test_resolvable_home_channel_delivers(self, no_home_env):
        """A resolved bare-platform target is actually sent, not reported as an
        error — the end state the reported job should reach once /sethome runs."""
        sent = []

        async def fake_send(platform, pconfig, chat_id, content, **kwargs):
            sent.append((platform, chat_id, content))
            return {"success": True}

        with patch(
            "gateway.config.load_gateway_config",
            return_value=_config_with_discord_home(),
        ), patch("tools.send_message_tool._send_to_platform", new=fake_send):
            error = _deliver_result(_job(), "watchdog: all hosts up")

        assert error is None
        assert len(sent) == 1
        assert sent[0][0] == Platform.DISCORD
        assert sent[0][1] == HOME_CHAT_ID
        assert "watchdog: all hosts up" in sent[0][2]

    def test_no_home_channel_still_reports_an_error(self, no_home_env):
        """Do NOT silently swallow a genuine misconfiguration: an explicitly
        named platform that cannot resolve must still fail loudly."""
        with patch(
            "gateway.config.load_gateway_config",
            return_value=_config_without_home(),
        ):
            error = _deliver_result(_job(), "watchdog: all hosts up")

        assert error
        assert "no delivery target resolved for deliver=discord" in error

    def test_local_and_origin_jobs_are_still_not_errors(self, no_home_env):
        """Unchanged behaviour: deliver=local never delivers, and deliver=origin
        with no origin and no home channels stays a non-error (#43014)."""
        with patch(
            "gateway.config.load_gateway_config",
            return_value=_config_without_home(),
        ):
            assert _deliver_result(_job(deliver="local"), "out") is None
            assert _deliver_result(_job(deliver="origin"), "out") is None


class TestUnresolvedReason:
    def test_reason_names_the_missing_home_channel_and_the_remedy(self, no_home_env):
        with patch(
            "gateway.config.load_gateway_config",
            return_value=_config_without_home(),
        ):
            reason = _unresolved_delivery_reason(_job(), "discord")

        # Symptom preserved so existing log greps/tests keep matching.
        assert reason.startswith("no delivery target resolved for deliver=discord")
        # Cause and every remedy the user can actually act on.
        assert "no home channel is configured for 'discord'" in reason
        assert "the job has no origin" in reason
        assert "/sethome" in reason
        assert "DISCORD_HOME_CHANNEL" in reason
        assert "'discord:<chat_id>'" in reason

    def test_reason_reports_a_cross_platform_origin_accurately(self, no_home_env):
        """With an origin on a different platform, don't claim there is none."""
        job = _job(origin={"platform": "slack", "chat_id": "C123"})
        with patch(
            "gateway.config.load_gateway_config",
            return_value=_config_without_home(),
        ):
            reason = _unresolved_delivery_reason(job, "discord")

        assert "the job's origin is on 'slack', not 'discord'" in reason
        assert "the job has no origin" not in reason

    def test_unknown_platform_says_so_instead_of_blaming_a_home_channel(self):
        reason = _unresolved_delivery_reason(_job(deliver="nope"), "nope")
        assert "'nope' is not a known cron delivery platform" in reason
        assert "home channel" not in reason

    def test_explicit_chat_id_target_adds_no_home_channel_hint(self):
        """platform:chat_id carries its own target — a home-channel hint would
        be wrong advice, and resolve_send_target already logged the real cause."""
        reason = _unresolved_delivery_reason(
            _job(deliver="discord:12345"), "discord:12345"
        )
        assert reason == "no delivery target resolved for deliver=discord:12345"


class TestCreateTimeNotice:
    def test_named_platform_notice_points_at_the_home_channel(self, no_home_env):
        """The misdirection fix: a job that already names a real platform must
        not be told to 'set deliver to a gateway-connected platform'."""
        with patch(
            "gateway.config.load_gateway_config",
            return_value=_config_without_home(),
        ):
            notice = _local_delivery_notice(_job(), "discord")

        assert notice
        assert "no home channel is configured for 'discord'" in notice
        assert "/sethome" in notice
        assert "'discord:<chat_id>'" in notice
        # The old, wrong advice must be gone for this case.
        assert "deliver='telegram'" not in notice

    def test_resolvable_job_gets_no_notice(self, no_home_env):
        with patch(
            "gateway.config.load_gateway_config",
            return_value=_config_with_discord_home(),
        ):
            assert _local_delivery_notice(_job(), "discord") is None

    def test_origin_job_keeps_the_local_only_notice(self, no_home_env):
        """deliver=origin from a CLI session is the original #51568 trap and
        keeps its own wording — this is a different failure with a different fix."""
        with patch(
            "gateway.config.load_gateway_config",
            return_value=_config_without_home(),
        ):
            notice = _local_delivery_notice(_job(deliver="origin"), "origin")

        assert notice
        assert "local-only cron job" in notice

    def test_explicit_local_request_still_silent(self, no_home_env):
        with patch(
            "gateway.config.load_gateway_config",
            return_value=_config_without_home(),
        ):
            assert _local_delivery_notice(_job(deliver="local"), "local") is None
