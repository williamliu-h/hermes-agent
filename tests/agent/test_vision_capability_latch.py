"""Vision-capability probes must be latched for the life of a session.

``_model_supports_vision`` / ``_provider_supports_vision_tool_messages`` decide
whether an image-bearing tool result (browser screenshots, ``computer_use``)
goes on the wire as a native image block or as a text summary. The underlying
probes are impure — config.yaml on disk, the models.dev catalog behind a 4h TTL
with a cold-cache network fetch, provider profiles — so they can answer
differently at two points in one conversation.

If that answer flips mid-session, every message from the first screenshot
onward is re-serialized and the Anthropic prompt cache misses for the rest of
the conversation. These tests pin the behavioural contract: with the probe
flipping underneath, the bytes we hand to the provider are identical, and the
latch only resets at a session boundary.
"""

from __future__ import annotations

import copy
import json

import pytest

import agent.image_routing as image_routing
import hermes_cli.config as hermes_config
from run_agent import AIAgent


def _make_agent(provider: str = "anthropic", model: str = "claude-sonnet-4") -> AIAgent:
    """Bare AIAgent instance — these are pure-method tests, no provider setup."""
    agent = object.__new__(AIAgent)
    agent.provider = provider
    agent.model = model
    agent.session_id = "20260823_000000_aaaaaa"
    agent._anthropic_image_fallback_cache = {}
    agent._vision_capability_latch = {}
    agent._no_list_tool_content_models = set()
    return agent


class _FlippingProbe:
    """Capability lookup that returns a different answer on every call."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = 0

    def __call__(self, *args, **kwargs):
        answer = self.answers[min(self.calls, len(self.answers) - 1)]
        self.calls += 1
        return answer


@pytest.fixture()
def flipping_vision(monkeypatch):
    """Make the models.dev/config vision lookup flip True -> False -> ..."""
    probe = _FlippingProbe(True, False, True, False)
    monkeypatch.setattr(hermes_config, "load_config", lambda *a, **k: {})
    monkeypatch.setattr(image_routing, "_lookup_supports_vision", probe)
    return probe


def _screenshot_tool_result():
    """A ``browser_exec``-style multimodal tool result envelope."""
    return {
        "_multimodal": True,
        "content": [
            {"type": "text", "text": "Navigated to example.com"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,AAAABBBBCCCC"},
            },
        ],
        "text_summary": "Navigated to example.com [screenshot]",
    }


def _history_with_screenshot():
    """Conversation history whose middle turn carries a browser screenshot."""
    return [
        {"role": "user", "content": "open example.com and screenshot it"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "browser_exec", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "name": "browser_exec",
            "content": _screenshot_tool_result()["content"],
        },
        {"role": "assistant", "content": "Done — the page loaded."},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "compare it with this mock"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,DDDDEEEEFFFF"},
                },
            ],
        },
    ]


def _wire_bytes(messages) -> str:
    return json.dumps(messages, sort_keys=True, default=str)


class TestApiMessageBytesAreStable:
    """The wire payload must not change when the probe flips underneath."""

    def test_anthropic_path_is_byte_stable(self, flipping_vision):
        agent = _make_agent()
        first = _wire_bytes(
            agent._prepare_anthropic_messages_for_api(_history_with_screenshot())
        )
        second = _wire_bytes(
            agent._prepare_anthropic_messages_for_api(_history_with_screenshot())
        )
        third = _wire_bytes(
            agent._prepare_anthropic_messages_for_api(_history_with_screenshot())
        )
        assert first == second == third
        assert flipping_vision.calls >= 1  # the probe really did get consulted

    def test_chat_completions_path_is_byte_stable(self, flipping_vision):
        agent = _make_agent(provider="openrouter", model="some/vision-model")
        first = _wire_bytes(
            agent._prepare_messages_for_non_vision_model(_history_with_screenshot())
        )
        second = _wire_bytes(
            agent._prepare_messages_for_non_vision_model(_history_with_screenshot())
        )
        assert first == second

    def test_byte_stable_when_probe_flips_false_to_true(self, monkeypatch):
        """The dangerous direction: text-summary first, "vision" later."""
        monkeypatch.setattr(hermes_config, "load_config", lambda *a, **k: {})
        monkeypatch.setattr(
            image_routing, "_lookup_supports_vision", _FlippingProbe(False, True, True)
        )
        agent = _make_agent()
        # Deterministic stand-in for the auxiliary vision_analyze round trip.
        monkeypatch.setattr(
            agent,
            "_describe_image_for_anthropic_fallback",
            lambda image_url, role: f"[{role} image: {image_url[:24]}]",
        )
        first = _wire_bytes(
            agent._prepare_anthropic_messages_for_api(_history_with_screenshot())
        )
        second = _wire_bytes(
            agent._prepare_anthropic_messages_for_api(_history_with_screenshot())
        )
        assert first == second
        # And it stayed on the text-fallback rendering it committed to.
        assert "image_url" not in second

    def test_input_history_is_never_mutated(self, flipping_vision):
        """History alteration is compression's job only."""
        agent = _make_agent()
        history = _history_with_screenshot()
        before = _wire_bytes(history)
        agent._prepare_anthropic_messages_for_api(history)
        agent._prepare_anthropic_messages_for_api(history)
        assert _wire_bytes(history) == before


class TestToolResultRenderingIsStable:
    def test_same_tool_result_renders_identically_all_session(self, flipping_vision):
        agent = _make_agent()
        renders = [
            _wire_bytes(
                agent._tool_result_content_for_active_model(
                    "browser_exec", _screenshot_tool_result()
                )
            )
            for _ in range(4)
        ]
        assert len(set(renders)) == 1

    def test_provider_tool_message_support_is_latched(self, monkeypatch):
        """``supports_vision_tool_messages`` selects the same two renderings."""
        monkeypatch.setattr(hermes_config, "load_config", lambda *a, **k: {})
        monkeypatch.setattr(
            image_routing, "_lookup_supports_vision", lambda *a, **k: True
        )
        agent = _make_agent(provider="mimo", model="mimo-vl")
        probe = _FlippingProbe(True, False, True)
        monkeypatch.setattr(
            agent, "_probe_provider_supports_vision_tool_messages", probe
        )
        renders = [
            _wire_bytes(
                agent._tool_result_content_for_active_model(
                    "browser_exec", _screenshot_tool_result()
                )
            )
            for _ in range(3)
        ]
        assert len(set(renders)) == 1
        assert probe.calls == 1 or renders[0] == renders[-1]


class TestLatchScope:
    def test_latch_does_not_leak_across_agents(self, flipping_vision):
        """A fresh session must be free to resolve the current answer."""
        first_agent = _make_agent()
        assert first_agent._model_supports_vision() is True
        second_agent = _make_agent()
        assert second_agent._model_supports_vision() is False

    def test_session_reset_re_resolves(self, flipping_vision):
        agent = _make_agent()
        agent.context_compressor = None
        agent._transition_context_engine_session = lambda **kwargs: None
        assert agent._model_supports_vision() is True

        agent.reset_session_state()
        assert agent._model_supports_vision() is False

    def test_model_switch_re_resolves(self, flipping_vision):
        """A ``/model`` switch is a new cache lineage — not a probe flip."""
        agent = _make_agent()
        assert agent._model_supports_vision() is True
        agent.model = "some-text-only-model"
        assert agent._model_supports_vision() is False
        # ...and switching back reuses the value latched for that identity.
        agent.model = "claude-sonnet-4"
        assert agent._model_supports_vision() is True

    def test_missing_latch_attribute_is_tolerated(self, flipping_vision):
        """Older/partially-constructed agents must not crash the request path."""
        agent = _make_agent()
        del agent._vision_capability_latch
        assert agent._model_supports_vision() is True
        assert agent._model_supports_vision() is True
