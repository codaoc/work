"""Query orchestrator for coordinating the complete query flow.

This module provides the QueryOrchestrator class that coordinates all components
of the query processing pipeline: SQL generation, validation, execution, and result
validation. It implements retry logic with exponential backoff, rate limiting,
error handling, metrics instrumentation, and request tracking.
"""

import asyncio
import time
import uuid
from contextlib import AbstractAsyncContextManager, nullcontext
from typing import Any

from asyncpg import Pool

from pg_mcp.cache.schema_cache import SchemaCache
from pg_mcp.config.settings import ResilienceConfig, ValidationConfig
from pg_mcp.models.errors import (
    DatabaseError,
    ErrorCode,
    LLMError,
    LLMTimeoutError,
    LLMUnavailableError,
    PgMcpError,
    RateLimitExceededError,
    SchemaLoadError,
    SecurityViolationError,
    SQLParseError,
)
from pg_mcp.models.query import (
    QueryRequest,
    QueryResponse,
    QueryResult,
    ReturnType,
    ValidationResult,
)
from pg_mcp.models.schema import DatabaseSchema
from pg_mcp.observability.metrics import MetricsCollector
from pg_mcp.observability.tracing import get_tracing_logger
from pg_mcp.resilience.circuit_breaker import CircuitBreaker
from pg_mcp.resilience.rate_limiter import MultiRateLimiter
from pg_mcp.services.result_validator import ResultValidator
from pg_mcp.services.sql_executor import SQLExecutor
from pg_mcp.services.sql_generator import SQLGenerator
from pg_mcp.services.sql_validator import SQLValidator

logger = get_tracing_logger(__name__)

# Transient LLM errors that are worth retrying with backoff.
TRANSIENT_LLM_ERRORS = (LLMTimeoutError, LLMUnavailableError)


class QueryOrchestrator:
    """Orchestrates the complete query processing pipeline.

    This class coordinates SQL generation, validation, execution, and result
    validation across multiple configured databases. It implements retry logic
    with error feedback and exponential backoff, rate limiting for LLM and
    database operations, a circuit breaker for fault tolerance, and optional
    Prometheus metrics instrumentation.

    Example:
        >>> orchestrator = QueryOrchestrator(
        ...     sql_generator=generator,
        ...     sql_validator=validator,
        ...     sql_executors={"mydb": executor},
        ...     result_validator=result_validator,
        ...     schema_cache=cache,
        ...     pools={"mydb": pool},
        ...     resilience_config=resilience_config,
        ...     validation_config=validation_config,
        ... )
        >>> response = await orchestrator.execute_query(QueryRequest(
        ...     question="How many users?",
        ...     database="mydb"
        ... ))
    """

    def __init__(
        self,
        sql_generator: SQLGenerator,
        sql_validator: SQLValidator,
        sql_executors: dict[str, SQLExecutor],
        result_validator: ResultValidator,
        schema_cache: SchemaCache,
        pools: dict[str, Pool],
        resilience_config: ResilienceConfig,
        validation_config: ValidationConfig,
        rate_limiter: MultiRateLimiter | None = None,
        metrics: MetricsCollector | None = None,
    ) -> None:
        """Initialize query orchestrator.

        Args:
            sql_generator: SQL generation service.
            sql_validator: SQL validation service.
            sql_executors: SQL execution services keyed by database name.
            result_validator: Result validation service.
            schema_cache: Schema cache instance.
            pools: Dictionary mapping database names to connection pools.
            resilience_config: Resilience configuration for retries and circuit breaker.
            validation_config: Validation configuration including thresholds.
            rate_limiter: Optional rate limiter guarding LLM and query concurrency.
            metrics: Optional metrics collector for request instrumentation.
        """
        self.sql_generator = sql_generator
        self.sql_validator = sql_validator
        self.sql_executors = sql_executors
        self.result_validator = result_validator
        self.schema_cache = schema_cache
        self.pools = pools
        self.resilience_config = resilience_config
        self.validation_config = validation_config
        self.rate_limiter = rate_limiter
        self.metrics = metrics

        # Single circuit breaker instance shared for all LLM calls
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=resilience_config.circuit_breaker_threshold,
            recovery_timeout=resilience_config.circuit_breaker_timeout,
        )

    async def execute_query(self, request: QueryRequest) -> QueryResponse:
        """Execute complete query flow from question to results.

        This method orchestrates the entire pipeline:
        1. Generate request_id for tracking
        2. Enforce input length limits
        3. Resolve and validate database name
        4. Load schema from cache
        5. Generate and validate SQL with retry/backoff logic
        6. Execute SQL on the resolved database (if return_type == RESULT)
        7. Validate results (optional)
        8. Return structured response

        Args:
            request: Query request containing question and parameters.

        Returns:
            QueryResponse: Complete response with SQL, results, or error information.

        Example:
            >>> response = await orchestrator.execute_query(
            ...     QueryRequest(question="Count all users", return_type="result")
            ... )
            >>> if response.success:
            ...     print(f"Found {response.data.row_count} rows")
        """
        # Generate request_id for full-chain tracing
        request_id = str(uuid.uuid4())
        logger.info(
            "Starting query execution",
            extra={"request_id": request_id, "question": request.question[:100]},
        )

        start_time = self._get_current_time_ms()
        status = "success"
        database_name: str | None = None

        try:
            # Step 1: Enforce configured input length limit
            max_len = self.validation_config.max_question_length
            if len(request.question) > max_len:
                raise PgMcpError(
                    message=(
                        f"Question length {len(request.question)} exceeds "
                        f"maximum of {max_len} characters"
                    ),
                    code=ErrorCode.QUESTION_TOO_LONG,
                    details={"length": len(request.question), "max_length": max_len},
                )

            # Step 2: Resolve database name
            database_name = self._resolve_database(request.database)
            logger.debug(
                "Resolved database",
                extra={"request_id": request_id, "database": database_name},
            )

            # Step 3: Get schema from cache
            schema = self.schema_cache.get(database_name)
            if schema is None:
                # Schema not in cache, load it
                pool = self.pools.get(database_name)
                if pool is None:
                    raise DatabaseError(
                        message=f"No connection pool available for database '{database_name}'",
                        details={"database": database_name},
                    )
                try:
                    schema = await self.schema_cache.load(database_name, pool)
                except Exception as e:
                    raise SchemaLoadError(
                        message=f"Failed to load schema for database '{database_name}': {e!s}",
                        details={"database": database_name, "error": str(e)},
                    ) from e

            logger.debug(
                "Schema loaded",
                extra={
                    "request_id": request_id,
                    "database": database_name,
                    "tables": len(schema.tables),
                },
            )

            # Step 4: Generate and validate SQL with retry logic
            generated_sql, validation_result, tokens_used = await self._generate_sql_with_retry(
                question=request.question,
                schema=schema,
                request_id=request_id,
            )

            # Step 5: If return_type is SQL, return early
            if request.return_type == ReturnType.SQL:
                logger.info(
                    "Returning SQL only",
                    extra={"request_id": request_id, "sql_length": len(generated_sql)},
                )
                return QueryResponse(
                    success=True,
                    generated_sql=generated_sql,
                    validation=validation_result,
                    data=None,
                    error=None,
                    confidence=100,
                    tokens_used=tokens_used,
                    warning=None,
                )

            # Step 6: Execute SQL on the resolved database
            logger.debug("Executing SQL", extra={"request_id": request_id})
            execution_start = self._get_current_time_ms()

            results, total_count = await self._execute_sql(database_name, generated_sql)

            execution_time_ms = self._get_current_time_ms() - execution_start
            logger.info(
                "SQL executed successfully",
                extra={
                    "request_id": request_id,
                    "database": database_name,
                    "row_count": total_count,
                    "execution_time_ms": execution_time_ms,
                },
            )

            # Step 7: Validate results (non-blocking, failures don't fail the request)
            result_confidence, warning = await self._validate_results_safely(
                question=request.question,
                sql=generated_sql,
                results=results,
                row_count=total_count,
                request_id=request_id,
            )

            # Step 8: Build successful response
            query_result = QueryResult(
                columns=list(results[0].keys()) if results else [],
                rows=results,
                row_count=len(results),  # Limited row count (after max_rows applied)
                execution_time_ms=execution_time_ms,
            )

            return QueryResponse(
                success=True,
                generated_sql=generated_sql,
                validation=validation_result,
                data=query_result,
                error=None,
                confidence=result_confidence,
                tokens_used=tokens_used,
                warning=warning,
            )

        except PgMcpError as e:
            status = e.code.value
            # Handle known application errors
            logger.warning(
                "Query execution failed with known error",
                extra={
                    "request_id": request_id,
                    "error_code": e.code,
                    "error_message": str(e),
                },
            )
            return QueryResponse(
                success=False,
                generated_sql=None,
                validation=None,
                data=None,
                error=e.to_error_detail(),
                confidence=0,
                tokens_used=None,
                warning=None,
            )
        except Exception as e:
            status = ErrorCode.INTERNAL_ERROR.value
            # Handle unexpected errors
            logger.exception(
                "Query execution failed with unexpected error",
                extra={"request_id": request_id},
            )
            return QueryResponse(
                success=False,
                generated_sql=None,
                validation=None,
                data=None,
                error=PgMcpError(
                    message=f"Internal server error: {e!s}",
                ).to_error_detail(),
                confidence=0,
                tokens_used=None,
                warning=None,
            )
        finally:
            if self.metrics is not None:
                duration_seconds = (self._get_current_time_ms() - start_time) / 1000
                self.metrics.query_duration.observe(duration_seconds)
                self.metrics.increment_query_request(
                    status=status, database=database_name or "unknown"
                )

    def _resolve_database(self, database: str | None) -> str:
        """Resolve database name from request or auto-select.

        If database is specified, validate it exists.
        If not specified and only one database available, auto-select it.

        Args:
            database: Database name from request (optional).

        Returns:
            str: Resolved database name.

        Raises:
            DatabaseError: If database is invalid or cannot be auto-selected.

        Example:
            >>> name = orchestrator._resolve_database("mydb")  # Validates "mydb" exists
            >>> name = orchestrator._resolve_database(None)  # Auto-selects if only one DB
        """
        if database is not None:
            # Validate specified database exists
            if database not in self.pools:
                raise DatabaseError(
                    message=f"Database '{database}' not found",
                    details={
                        "requested_database": database,
                        "available_databases": list(self.pools.keys()),
                    },
                )
            return database

        # Auto-select if only one database available
        available_dbs = list(self.pools.keys())
        if len(available_dbs) == 0:
            raise DatabaseError(
                message="No databases configured",
                details={},
            )
        if len(available_dbs) == 1:
            return available_dbs[0]

        # Multiple databases, must specify
        raise DatabaseError(
            message="Multiple databases available, please specify which to query",
            details={"available_databases": available_dbs},
        )

    async def _execute_sql(self, database_name: str, sql: str) -> tuple[list[dict[str, Any]], int]:
        """Execute SQL on the executor bound to the resolved database.

        Concurrency is guarded by the query rate limiter when configured.

        Args:
            database_name: Resolved target database name.
            sql: Validated SQL query to execute.

        Returns:
            tuple: (results, total_row_count) from the executor.

        Raises:
            RateLimitExceededError: If the query concurrency limit cannot be
                acquired within the configured timeout.
            DatabaseError: If no executor exists for the database.
        """
        executor = self.sql_executors.get(database_name)
        if executor is None:
            raise DatabaseError(
                message=f"No executor available for database '{database_name}'",
                details={"database": database_name},
            )

        try:
            async with self._query_slot():
                start = self._get_current_time_ms()
                results, total_count = await executor.execute(sql)
                if self.metrics is not None:
                    self.metrics.observe_db_query_duration(
                        (self._get_current_time_ms() - start) / 1000
                    )
                return results, total_count
        except TimeoutError as e:
            raise RateLimitExceededError(
                message="Rate limit exceeded, try again later",
                details={"reason": "query concurrency limit reached"},
            ) from e

    def _query_slot(self) -> AbstractAsyncContextManager[None]:
        """Acquire a query rate-limiter slot (no-op when limiter is absent)."""
        if self.rate_limiter is None:
            return nullcontext()
        return self.rate_limiter.for_queries(timeout=self.resilience_config.rate_limit_timeout)

    def _llm_slot(self) -> AbstractAsyncContextManager[None]:
        """Acquire an LLM rate-limiter slot (no-op when limiter is absent)."""
        if self.rate_limiter is None:
            return nullcontext()
        return self.rate_limiter.for_llm(timeout=self.resilience_config.rate_limit_timeout)

    async def _generate_sql_with_retry(
        self,
        question: str,
        schema: DatabaseSchema,
        request_id: str,
    ) -> tuple[str, ValidationResult, int | None]:
        """Generate and validate SQL with retry logic on validation failures.

        This method implements a retry loop that:
        1. Checks circuit breaker state
        2. Generates SQL using LLM (guarded by the LLM rate limiter)
        3. Validates the generated SQL
        4. On validation failure, retries with error feedback after backoff
        5. On transient LLM errors (timeout/unavailable), retries after backoff
        6. Records success/failure to circuit breaker and metrics

        Args:
            question: User's natural language question.
            schema: Database schema for context.
            request_id: Request ID for tracking.

        Returns:
            tuple: (generated_sql, validation_result, tokens_used)

        Raises:
            LLMError: If circuit breaker is open or generation fails permanently.
            SecurityViolationError: If SQL fails validation after all retries.
            SQLParseError: If SQL cannot be parsed.
        """
        # Check circuit breaker
        if not self.circuit_breaker.allow_request():
            raise LLMError(
                message="SQL generation service is temporarily unavailable (circuit breaker open)",
                details={
                    "circuit_state": self.circuit_breaker.state,
                    "failure_count": self.circuit_breaker.failure_count,
                },
            )

        previous_sql: str | None = None
        error_feedback: str | None = None
        max_retries = self.resilience_config.max_retries
        tokens_used: int | None = None

        for attempt in range(max_retries + 1):
            try:
                logger.debug(
                    "Generating SQL",
                    extra={
                        "request_id": request_id,
                        "attempt": attempt + 1,
                        "max_retries": max_retries + 1,
                    },
                )

                # Generate SQL (rate limited)
                try:
                    async with self._llm_slot():
                        if self.metrics is not None:
                            self.metrics.increment_llm_call("generate_sql")
                            llm_start = self._get_current_time_ms()

                        generated_sql = await self.sql_generator.generate(
                            question=question,
                            schema=schema,
                            previous_attempt=previous_sql,
                            error_feedback=error_feedback,
                        )

                        if self.metrics is not None:
                            self.metrics.observe_llm_latency(
                                "generate_sql",
                                (self._get_current_time_ms() - llm_start) / 1000,
                            )
                except TimeoutError as e:
                    raise RateLimitExceededError(
                        message="Rate limit exceeded, try again later",
                        details={"reason": "LLM concurrency limit reached"},
                    ) from e

                # Track real token usage reported by the LLM client
                last_usage = getattr(self.sql_generator, "last_tokens_used", None)
                if last_usage is not None:
                    tokens_used = last_usage
                    if self.metrics is not None:
                        self.metrics.increment_llm_tokens("generate_sql", last_usage)

                logger.debug(
                    "SQL generated",
                    extra={
                        "request_id": request_id,
                        "sql_length": len(generated_sql),
                    },
                )

                # Validate SQL
                try:
                    self.sql_validator.validate_or_raise(generated_sql)
                except (SecurityViolationError, SQLParseError) as validation_error:
                    if self.metrics is not None:
                        self.metrics.increment_sql_rejected(validation_error.code.value)
                    if attempt < max_retries:
                        # Record as failure and retry with feedback after backoff
                        logger.warning(
                            "SQL validation failed, retrying with feedback",
                            extra={
                                "request_id": request_id,
                                "attempt": attempt + 1,
                                "error": str(validation_error),
                            },
                        )
                        self.circuit_breaker.record_failure()
                        previous_sql = generated_sql
                        error_feedback = str(validation_error)
                        await self._backoff(attempt)
                        continue

                    # Out of retries, record failure and raise
                    self.circuit_breaker.record_failure()
                    logger.error(
                        "SQL validation failed after all retries",
                        extra={
                            "request_id": request_id,
                            "attempts": attempt + 1,
                            "error": str(validation_error),
                        },
                    )
                    raise

                # Validation successful
                self.circuit_breaker.record_success()
                logger.info(
                    "SQL generated and validated successfully",
                    extra={
                        "request_id": request_id,
                        "attempts": attempt + 1,
                    },
                )

                # Build validation result reflecting the actual outcome
                validation_result = ValidationResult(
                    is_valid=True,
                    is_select=True,
                    allows_data_modification=False,
                    uses_blocked_functions=[],
                    error_message=None,
                )

                return generated_sql, validation_result, tokens_used

            except (LLMError, SecurityViolationError, SQLParseError) as e:
                if isinstance(e, TRANSIENT_LLM_ERRORS) and attempt < max_retries:
                    # Transient LLM failure: record and retry with backoff
                    self.circuit_breaker.record_failure()
                    logger.warning(
                        "Transient LLM error, retrying with backoff",
                        extra={
                            "request_id": request_id,
                            "attempt": attempt + 1,
                            "error": str(e),
                        },
                    )
                    await self._backoff(attempt)
                    continue
                # Non-retryable, or retries exhausted: re-raise known errors
                raise
            except Exception as e:
                # Unexpected error during generation
                self.circuit_breaker.record_failure()
                logger.exception(
                    "Unexpected error during SQL generation",
                    extra={"request_id": request_id},
                )
                raise LLMError(
                    message=f"SQL generation failed unexpectedly: {e!s}",
                    details={"error_type": type(e).__name__},
                ) from e

        # Should not reach here, but just in case
        self.circuit_breaker.record_failure()
        raise LLMError(
            message="SQL generation failed after all retry attempts",
            details={"max_retries": max_retries},
        )

    async def _backoff(self, attempt: int) -> None:
        """Sleep for the exponential backoff delay after the given attempt.

        Args:
            attempt: Zero-based index of the attempt that just failed.
        """
        delay = self.resilience_config.retry_delay * (
            self.resilience_config.backoff_factor**attempt
        )
        await asyncio.sleep(delay)

    async def _validate_results_safely(
        self,
        question: str,
        sql: str,
        results: list[dict[str, Any]],
        row_count: int,
        request_id: str,
    ) -> tuple[int, str | None]:
        """Validate query results with error handling (non-blocking).

        This method attempts to validate results using LLM, but failures
        don't cause the overall query to fail. Returns a confidence score
        and an optional warning message. The configured minimum confidence
        score is respected: validation failures no longer default to a
        perfect score, and low-confidence results carry a warning.

        Args:
            question: User's original question.
            sql: Generated SQL query.
            results: Query results.
            row_count: Total row count.
            request_id: Request ID for tracking.

        Returns:
            tuple: (confidence 0-100, warning message or None).

        Example:
            >>> confidence, warning = await orchestrator._validate_results_safely(
            ...     question="Count users",
            ...     sql="SELECT COUNT(*) FROM users",
            ...     results=[{"count": 42}],
            ...     row_count=1,
            ...     request_id="123",
            ... )
        """
        if not self.validation_config.enabled:
            return 100, None

        try:
            logger.debug(
                "Validating results",
                extra={"request_id": request_id},
            )

            validation_result = await self.result_validator.validate(
                question=question,
                sql=sql,
                results=results,
                row_count=row_count,
            )

            logger.info(
                "Result validation completed",
                extra={
                    "request_id": request_id,
                    "confidence": validation_result.confidence,
                    "is_acceptable": validation_result.is_acceptable,
                },
            )

            return self._apply_confidence_threshold(validation_result.confidence)

        except Exception as e:
            # Log and flag rather than fail the query or mask with confidence 100
            logger.warning(
                "Result validation failed, flagging response with warning",
                extra={
                    "request_id": request_id,
                    "error": str(e),
                },
            )
            floor = self.validation_config.min_confidence_score
            return floor, f"Result validation failed: {e!s}"

    def _apply_confidence_threshold(self, confidence: int) -> tuple[int, str | None]:
        """Flag confidence scores below the configured minimum threshold.

        Args:
            confidence: Confidence score (0-100) from result validation.

        Returns:
            tuple: (confidence, warning message or None).
        """
        threshold = self.validation_config.min_confidence_score
        if confidence < threshold:
            return confidence, (
                f"Result confidence {confidence} is below the configured "
                f"minimum of {threshold}; results may not answer the question."
            )
        return confidence, None

    @staticmethod
    def _get_current_time_ms() -> float:
        """Get current time in milliseconds.

        Returns:
            float: Current time in milliseconds since epoch.
        """
        return time.time() * 1000
