"""Unit tests for the ResultValidator service.

These tests mock the OpenAI client to verify confidence parsing, threshold
behavior, timeout handling, and error mapping without requiring network access.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import SecretStr

from pg_mcp.config.settings import OpenAIConfig, ValidationConfig
from pg_mcp.models.errors import LLMError, LLMTimeoutError, LLMUnavailableError
from pg_mcp.services.result_validator import ResultValidator


def _make_response(content: str | None) -> MagicMock:
    """Build a mock OpenAI ChatCompletion response."""
    response = MagicMock()
    response.choices = [MagicMock(message=MagicMock(content=content))]
    return response


class TestResultValidator:
    """Test ResultValidator with a mocked OpenAI client."""

    @pytest.fixture
    def config(self) -> OpenAIConfig:
        """Create test OpenAI config."""
        return OpenAIConfig(api_key=SecretStr("sk-test-key-12345"))

    @pytest.fixture
    def validator(self, config: OpenAIConfig) -> ResultValidator:
        """Create validator with default validation config."""
        return ResultValidator(
            openai_config=config,
            validation_config=ValidationConfig(enabled=True, confidence_threshold=70),
        )

    @pytest.mark.asyncio
    async def test_validate_success_high_confidence(self, validator: ResultValidator) -> None:
        """Test parsing a valid high-confidence JSON response."""
        payload = {"confidence": 95, "explanation": "Results match", "suggestion": None}
        with patch.object(
            validator.client.chat.completions,
            "create",
            new=AsyncMock(return_value=_make_response(json.dumps(payload))),
        ):
            result = await validator.validate(
                question="Count users",
                sql="SELECT COUNT(*) FROM users",
                results=[{"count": 42}],
                row_count=1,
            )

        assert result.confidence == 95
        assert result.explanation == "Results match"
        assert result.suggestion is None
        assert result.is_acceptable is True

    @pytest.mark.asyncio
    async def test_validate_below_threshold_not_acceptable(
        self, validator: ResultValidator
    ) -> None:
        """Test that confidence below the threshold is marked unacceptable."""
        payload = {"confidence": 40, "explanation": "Results do not match"}
        with patch.object(
            validator.client.chat.completions,
            "create",
            new=AsyncMock(return_value=_make_response(json.dumps(payload))),
        ):
            result = await validator.validate(
                question="Count users",
                sql="SELECT COUNT(*) FROM users",
                results=[{"count": 42}],
                row_count=1,
            )

        assert result.confidence == 40
        assert result.is_acceptable is False

    @pytest.mark.asyncio
    async def test_validate_with_suggestion(self, validator: ResultValidator) -> None:
        """Test that suggestions from the LLM are preserved."""
        payload = {
            "confidence": 75,
            "explanation": "Partially matches",
            "suggestion": "Filter by active status",
        }
        with patch.object(
            validator.client.chat.completions,
            "create",
            new=AsyncMock(return_value=_make_response(json.dumps(payload))),
        ):
            result = await validator.validate(
                question="Count active users",
                sql="SELECT COUNT(*) FROM users",
                results=[{"count": 42}],
                row_count=1,
            )

        assert result.suggestion == "Filter by active status"
        assert result.is_acceptable is True

    @pytest.mark.asyncio
    async def test_validate_disabled_returns_perfect(self, config: OpenAIConfig) -> None:
        """Test that disabled validation short-circuits with confidence 100."""
        validator = ResultValidator(
            openai_config=config,
            validation_config=ValidationConfig(enabled=False),
        )

        result = await validator.validate(
            question="q", sql="SELECT 1", results=[], row_count=0
        )

        assert result.confidence == 100
        assert result.is_acceptable is True

    @pytest.mark.asyncio
    async def test_invalid_json_returns_moderate_confidence(
        self, validator: ResultValidator
    ) -> None:
        """Test that unparseable responses degrade to confidence 60."""
        with patch.object(
            validator.client.chat.completions,
            "create",
            new=AsyncMock(return_value=_make_response("this is not json")),
        ):
            result = await validator.validate(
                question="Count users",
                sql="SELECT COUNT(*) FROM users",
                results=[{"count": 42}],
                row_count=1,
            )

        assert result.confidence == 60
        assert result.is_acceptable is False
        assert "parsing failed" in result.explanation.lower()

    @pytest.mark.asyncio
    async def test_confidence_out_of_bounds_clamped(self, validator: ResultValidator) -> None:
        """Test that out-of-range confidence values are clamped to [0, 100]."""
        payload = {"confidence": 150, "explanation": "too high"}
        with patch.object(
            validator.client.chat.completions,
            "create",
            new=AsyncMock(return_value=_make_response(json.dumps(payload))),
        ):
            result = await validator.validate(
                question="Count users",
                sql="SELECT COUNT(*) FROM users",
                results=[{"count": 42}],
                row_count=1,
            )

        assert result.confidence == 100

    @pytest.mark.asyncio
    async def test_non_numeric_confidence_defaults(self, validator: ResultValidator) -> None:
        """Test that non-numeric confidence values default to 50."""
        payload = {"confidence": "high", "explanation": "bad type"}
        with patch.object(
            validator.client.chat.completions,
            "create",
            new=AsyncMock(return_value=_make_response(json.dumps(payload))),
        ):
            result = await validator.validate(
                question="Count users",
                sql="SELECT COUNT(*) FROM users",
                results=[{"count": 42}],
                row_count=1,
            )

        assert result.confidence == 50

    @pytest.mark.asyncio
    async def test_timeout_raises_llm_timeout(self, validator: ResultValidator) -> None:
        """Test that client timeouts map to LLMTimeoutError."""
        with patch.object(
            validator.client.chat.completions,
            "create",
            new=AsyncMock(side_effect=TimeoutError("Request timed out")),
        ), pytest.raises(LLMTimeoutError) as exc_info:
            await validator.validate(
                question="Count users",
                sql="SELECT COUNT(*) FROM users",
                results=[{"count": 42}],
                row_count=1,
            )

        assert "timed out" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_auth_error_raises_unavailable(self, validator: ResultValidator) -> None:
        """Test that authentication failures map to LLMUnavailableError."""
        with patch.object(
            validator.client.chat.completions,
            "create",
            new=AsyncMock(side_effect=Exception("authentication failed: bad api_key")),
        ), pytest.raises(LLMUnavailableError):
            await validator.validate(
                question="Count users",
                sql="SELECT COUNT(*) FROM users",
                results=[{"count": 42}],
                row_count=1,
            )

    @pytest.mark.asyncio
    async def test_rate_limit_error_raises_unavailable(
        self, validator: ResultValidator
    ) -> None:
        """Test that rate limit errors map to LLMUnavailableError."""
        with patch.object(
            validator.client.chat.completions,
            "create",
            new=AsyncMock(side_effect=Exception("rate_limit exceeded")),
        ), pytest.raises(LLMUnavailableError):
            await validator.validate(
                question="Count users",
                sql="SELECT COUNT(*) FROM users",
                results=[{"count": 42}],
                row_count=1,
            )

    @pytest.mark.asyncio
    async def test_generic_error_raises_llm_error(self, validator: ResultValidator) -> None:
        """Test that generic failures map to LLMError."""
        with patch.object(
            validator.client.chat.completions,
            "create",
            new=AsyncMock(side_effect=Exception("something broke")),
        ), pytest.raises(LLMError) as exc_info:
            await validator.validate(
                question="Count users",
                sql="SELECT COUNT(*) FROM users",
                results=[{"count": 42}],
                row_count=1,
            )

        assert "result validation failed" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_empty_choices_raises_llm_error(self, validator: ResultValidator) -> None:
        """Test that empty choice lists raise LLMError."""
        response = MagicMock()
        response.choices = []
        with patch.object(
            validator.client.chat.completions,
            "create",
            new=AsyncMock(return_value=response),
        ), pytest.raises(LLMError) as exc_info:
            await validator.validate(
                question="Count users",
                sql="SELECT COUNT(*) FROM users",
                results=[{"count": 42}],
                row_count=1,
            )

        assert "empty response" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_empty_content_raises_llm_error(self, validator: ResultValidator) -> None:
        """Test that empty message content raises LLMError."""
        with patch.object(
            validator.client.chat.completions,
            "create",
            new=AsyncMock(return_value=_make_response(None)),
        ), pytest.raises(LLMError) as exc_info:
            await validator.validate(
                question="Count users",
                sql="SELECT COUNT(*) FROM users",
                results=[{"count": 42}],
                row_count=1,
            )

        assert "empty message content" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_sample_rows_limit_respected(self, config: OpenAIConfig) -> None:
        """Test that only the configured number of sample rows is sent."""
        validator = ResultValidator(
            openai_config=config,
            validation_config=ValidationConfig(enabled=True, sample_rows=2),
        )

        captured: dict = {}

        async def capture_create(**kwargs):
            captured["prompt"] = kwargs["messages"][1]["content"]
            return _make_response(json.dumps({"confidence": 90, "explanation": "ok"}))

        results = [{"id": i} for i in range(5)]
        with patch.object(validator.client.chat.completions, "create", new=capture_create):
            await validator.validate(
                question="List users",
                sql="SELECT id FROM users",
                results=results,
                row_count=5,
            )

        # Rows beyond the sample limit should not appear in the prompt
        assert '"id": 0' in captured["prompt"]
        assert '"id": 1' in captured["prompt"]
        assert '"id": 2' not in captured["prompt"]
