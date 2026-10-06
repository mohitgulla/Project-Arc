"""Tests for arc.llm_routing — per-persona model tiers (PLAN §2.4, D8, E8.1)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from arc.config import ArcSettings
from arc.ingest.llm import HermesScalpLLM
from arc.llm_routing import (
    DEFAULT_ROUTING_PATH,
    LLMRouting,
    Persona,
    load_routing,
    resolve,
)
from arc.pipeline.env import PERSONAS, PipelineEnv

FRONTIER = "anthropic/claude-opus-5.5"
CHEAP = "anthropic/claude-opus-5"

# PLAN §2.4 / D8: every persona, its tier and model.
EXPECTED = {
    Persona.RESEARCH: ("frontier", FRONTIER),
    Persona.QUANT: ("frontier", FRONTIER),
    Persona.RISK: ("frontier", FRONTIER),
    Persona.SCALP: ("cheap", CHEAP),
}

SKILLS_DIR = Path(__file__).resolve().parent.parent / "hermes" / "skills"


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "routing.yaml"
    p.write_text(text)
    return p


class TestDefaultRouting:
    def test_covers_exactly_the_plan_personas(self) -> None:
        assert set(Persona) == set(EXPECTED)
        assert {p.value for p in Persona} == {
            "scalp",
            "research",
            "quant",
            "risk",
        }  # D56 (E13.2): Broker and Ops are deterministic, not LLM personas

    @pytest.mark.parametrize(("persona", "expected"), list(EXPECTED.items()))
    def test_every_persona_resolves_to_its_tier_model(
        self, persona: Persona, expected: tuple[str, str]
    ) -> None:
        tier, model = expected
        route = resolve(persona)
        assert route.persona is persona
        assert route.tier == tier
        assert route.model == model
        assert route.provider == "anthropic"
        # string names work too
        assert resolve(persona.value) == route

    def test_unknown_persona_rejected(self) -> None:
        with pytest.raises(ValueError, match="execution"):
            resolve("execution")

    def test_default_path(self) -> None:
        assert DEFAULT_ROUTING_PATH.name == "llm_routing.yaml"
        assert load_routing() == load_routing(DEFAULT_ROUTING_PATH)

    def test_no_fallback_provider_configured(self) -> None:
        """D8: no fallback provider; only Anthropic models in the routing file."""
        routing = load_routing()
        assert {t.model.split("/")[0] for t in routing.tiers.values()} == {"anthropic"}

    @pytest.mark.parametrize("persona", list(Persona))
    def test_skill_docs_agree_with_config(self, persona: Persona) -> None:
        text = (SKILLS_DIR / f"arc-{persona.value}" / "SKILL.md").read_text()
        m = re.search(r"\*\*Model tier:\*\*\s*(\w+)", text)
        assert m, f"arc-{persona.value} SKILL.md has no Model tier line"
        assert m.group(1) == resolve(persona).tier


class TestConfigDriven:
    """Changing the YAML (one place) changes what every call site gets."""

    def test_tier_change_moves_all_its_personas(self, tmp_path: Path) -> None:
        text = DEFAULT_ROUTING_PATH.read_text().replace(
            f"model: {CHEAP}\n", "model: anthropic/claude-other\n"
        )
        s = ArcSettings(env="paper", llm_routing_file=_write(tmp_path, text))
        for p in (Persona.SCALP,):
            assert resolve(p, s).model == "anthropic/claude-other"
        assert resolve(Persona.RESEARCH, s).model == FRONTIER

    def test_persona_change_moves_one_persona(self, tmp_path: Path) -> None:
        text = DEFAULT_ROUTING_PATH.read_text().replace("quant:    frontier", "quant:    cheap")
        s = ArcSettings(env="paper", llm_routing_file=_write(tmp_path, text))
        assert resolve("quant", s).model == CHEAP
        assert resolve("risk", s).model == FRONTIER

    def test_env_var_selects_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        text = DEFAULT_ROUTING_PATH.read_text().replace(
            f"model: {FRONTIER}\n", "model: anthropic/claude-x\n"
        )
        monkeypatch.setenv("ARC_LLM_ROUTING_FILE", str(_write(tmp_path, text)))
        s = ArcSettings(env="paper")
        assert resolve("research", s).model == "anthropic/claude-x"

    def test_scalp_backend_uses_routing(self, tmp_path: Path) -> None:
        text = DEFAULT_ROUTING_PATH.read_text().replace(
            f"model: {CHEAP}\n", "model: anthropic/claude-cheap-x\n"
        )
        s = ArcSettings(env="paper", llm_routing_file=_write(tmp_path, text))
        llm = HermesScalpLLM.from_settings(s)
        assert (llm.model, llm.provider) == ("anthropic/claude-cheap-x", "anthropic")
        assert llm.timeout_seconds == s.scalp_timeout_seconds

    def test_pipeline_personas_get_their_own_route(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import arc.data.alpaca as alpaca_mod

        monkeypatch.setattr(alpaca_mod, "AlpacaMarketData", lambda: object())
        s = ArcSettings(env="paper")
        env = PipelineEnv.live(s, broker=False)
        for p in PERSONAS:
            llm = env.llm(p)
            assert isinstance(llm, HermesScalpLLM)
            assert llm.model == EXPECTED[Persona(p)][1]
            assert llm.provider == "anthropic"
            assert llm.timeout_seconds == s.persona_timeout_seconds


class TestValidation:
    def test_missing_persona(self) -> None:
        with pytest.raises(ValidationError, match="personas without a tier: scalp"):
            LLMRouting.model_validate(
                {
                    "tiers": {"t": {"model": "anthropic/m"}},
                    "personas": {p.value: "t" for p in Persona if p is not Persona.SCALP},
                }
            )

    def test_unknown_tier(self) -> None:
        with pytest.raises(ValidationError, match="unknown tier"):
            LLMRouting.model_validate(
                {
                    "tiers": {"t": {"model": "anthropic/m"}},
                    "personas": {p.value: "nope" for p in Persona},
                }
            )

    def test_unknown_persona_key(self) -> None:
        personas = {p.value: "t" for p in Persona} | {"execution": "t"}
        with pytest.raises(ValidationError):
            LLMRouting.model_validate(
                {"tiers": {"t": {"model": "anthropic/m"}}, "personas": personas}
            )

    @pytest.mark.parametrize("bad", ["claude-opus-5", "/m", "anthropic/", ""])
    def test_model_must_be_qualified(self, bad: str) -> None:
        with pytest.raises(ValidationError, match="provider"):
            LLMRouting.model_validate(
                {"tiers": {"t": {"model": bad}}, "personas": {p.value: "t" for p in Persona}}
            )

    def test_extra_keys_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            LLMRouting.model_validate(
                {
                    "tiers": {"t": {"model": "anthropic/m"}},
                    "personas": {p.value: "t" for p in Persona},
                    "fallback_providers": [],
                }
            )

    def test_non_mapping_file(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="mapping"):
            load_routing(_write(tmp_path, "- a\n- b\n"))
