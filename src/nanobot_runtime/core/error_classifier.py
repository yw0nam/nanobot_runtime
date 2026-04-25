"""Project-wide error classification system.

Provides severity-based error classification for consistent error handling
across all services (WebSocket, service_manager, health checks, etc.).
"""

from enum import StrEnum

from loguru import logger


class ErrorSeverity(StrEnum):
    """Classification of error severity for handling strategy."""

    TRANSIENT = "transient"  # Temporary network issues, retry immediately
    RECOVERABLE = "recoverable"  # Retry after backoff
    FATAL = "fatal"  # Terminate connection / abort operation immediately


class ErrorClassifier:
    """Classifies exceptions into severity levels for appropriate handling."""

    # Mapping of exception types to severity levels
    SEVERITY_MAP: dict[type[Exception], ErrorSeverity] = {
        # Transient errors - network/connection issues that may resolve quickly
        TimeoutError: ErrorSeverity.TRANSIENT,
        ConnectionResetError: ErrorSeverity.TRANSIENT,
        ConnectionAbortedError: ErrorSeverity.TRANSIENT,
        BrokenPipeError: ErrorSeverity.TRANSIENT,
        # Recoverable errors - validation/parsing issues that won't fix themselves
        ValueError: ErrorSeverity.RECOVERABLE,
        KeyError: ErrorSeverity.RECOVERABLE,
        # Fatal errors - unrecoverable issues
        RuntimeError: ErrorSeverity.FATAL,
    }

    @classmethod
    def classify(cls, exc: Exception) -> ErrorSeverity:
        """Classify an exception into a severity level.

        Args:
            exc: The exception to classify.

        Returns:
            ErrorSeverity: The severity level of the exception.
        """
        exc_type = type(exc)

        # Direct type match
        if exc_type in cls.SEVERITY_MAP:
            severity = cls.SEVERITY_MAP[exc_type]
            logger.debug(f"Classified {exc_type.__name__} as {severity}")
            return severity

        # Check inheritance chain
        for mapped_type, severity in cls.SEVERITY_MAP.items():
            if isinstance(exc, mapped_type):
                logger.debug(
                    f"Classified {exc_type.__name__} (subclass of {mapped_type.__name__}) as {severity}"
                )
                return severity

        # Default to FATAL for unknown exceptions
        logger.warning(
            f"Unknown exception type {exc_type.__name__}, defaulting to FATAL: {exc}"
        )
        return ErrorSeverity.FATAL

    @classmethod
    def should_retry(cls, exc: Exception, error_count: int, max_tolerance: int) -> bool:
        """Determine if an operation should be retried based on error classification.

        Args:
            exc: The exception that occurred.
            error_count: Current consecutive error count.
            max_tolerance: Maximum number of errors to tolerate.

        Returns:
            bool: True if the operation should be retried.
        """
        severity = cls.classify(exc)

        # Fatal errors never retry
        if severity == ErrorSeverity.FATAL:
            return False

        # Check if we've exceeded tolerance
        if error_count >= max_tolerance:
            logger.warning(
                f"Error tolerance exceeded ({error_count}/{max_tolerance}), not retrying"
            )
            return False

        # Transient and recoverable errors can be retried within tolerance
        return True

    @classmethod
    def get_backoff_delay(cls, exc: Exception, base_delay: float) -> float:
        """Get the appropriate backoff delay for an exception.

        Args:
            exc: The exception that occurred.
            base_delay: Base delay time in seconds.

        Returns:
            float: Delay in seconds before retry.
        """
        severity = cls.classify(exc)

        if severity == ErrorSeverity.TRANSIENT:
            return 0.0
        elif severity == ErrorSeverity.RECOVERABLE:
            return base_delay
        else:
            return 0.0
