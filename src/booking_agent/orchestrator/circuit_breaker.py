import time
from enum import Enum


class CircuitState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreaker:
    """
    Standard three-state circuit breaker per supplier.

    CLOSED  → normal operation
    OPEN    → all calls blocked until recovery_timeout elapses
    HALF_OPEN → one probe allowed; success → CLOSED, failure → OPEN
    """

    def __init__(
        self,
        supplier_id: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
    ) -> None:
        self.supplier_id = supplier_id
        self._threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._opened_at: float | None = None
        self._half_open_probing = False

    @property
    def state(self) -> CircuitState:
        if (
            self._state == CircuitState.OPEN
            and self._opened_at is not None
            and time.monotonic() - self._opened_at >= self._recovery_timeout
        ):
            self._state = CircuitState.HALF_OPEN
            self._half_open_probing = False
        return self._state

    def is_allowed(self) -> bool:
        s = self.state
        if s == CircuitState.CLOSED:
            return True
        if s == CircuitState.OPEN:
            return False
        # HALF_OPEN: allow only one probe at a time
        if self._half_open_probing:
            return False
        self._half_open_probing = True
        return True

    def record_success(self) -> None:
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._opened_at = None
        self._half_open_probing = False

    def record_failure(self) -> None:
        if self._state == CircuitState.HALF_OPEN:
            self._trip()
            return
        self._failure_count += 1
        if self._failure_count >= self._threshold:
            self._trip()

    def _trip(self) -> None:
        self._state = CircuitState.OPEN
        self._opened_at = time.monotonic()
        self._half_open_probing = False
