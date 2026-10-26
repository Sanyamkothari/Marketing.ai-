"""`generative.llm.data_agent_max_output_tokens` is left out of a dump only while it holds the shipped value."""

from __future__ import annotations

from engine.config import DEFAULT_DATA_AGENT_MAX_OUTPUT_TOKENS, LlmConfig


def test_the_shipped_value_is_left_out_of_a_dump() -> None:
    config = LlmConfig()

    assert config.data_agent_max_output_tokens == DEFAULT_DATA_AGENT_MAX_OUTPUT_TOKENS
    assert "data_agent_max_output_tokens" not in config.model_dump()
    assert "data_agent_max_output_tokens" not in config.model_dump(mode="json")
    assert LlmConfig.model_validate(config.model_dump()).data_agent_max_output_tokens == (
        DEFAULT_DATA_AGENT_MAX_OUTPUT_TOKENS
    )


def test_a_changed_value_is_dumped_and_survives_a_round_trip() -> None:
    config = LlmConfig(data_agent_max_output_tokens=8000)

    assert config.model_dump()["data_agent_max_output_tokens"] == 8000
    assert config.model_dump(mode="json")["data_agent_max_output_tokens"] == 8000
    assert LlmConfig.model_validate(config.model_dump()).data_agent_max_output_tokens == 8000
    assert LlmConfig.model_validate(config.model_dump(mode="json")).data_agent_max_output_tokens == 8000
