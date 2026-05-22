class SQuantError(Exception):
    """Base exception for all S-Quant errors."""


class DataQualityError(SQuantError):
    """Raised when market data fails validation."""


class StateError(SQuantError):
    """Raised when an unexpected state transition is attempted."""


class StateConflictError(StateError):
    """Raised when Google Sheets state does not match expected value."""


class CircuitBreakerTrippedError(SQuantError):
    """Raised when the circuit breaker has halted trading."""


class InsufficientCapitalError(SQuantError):
    """Raised when capital is insufficient to buy even 1 share."""


class SheetsError(SQuantError):
    """Raised on Google Sheets API failures."""


class SlackError(SQuantError):
    """Raised on Slack notification failures."""


class FetchTimeoutError(SQuantError):
    """Raised when a market-data fetch exceeds the allowed timeout."""
