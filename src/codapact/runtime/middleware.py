"""Built-in middleware."""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from codapact.core.errors import TransportError
from codapact.runtime.abstractions import CallContext, HttpRequest, HttpResponse, Middleware, Next

RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


@dataclass(frozen=True, slots=True)
class Retry:
    """Retry idempotent operations on transient failures with jittered backoff.

    Honors ``Retry-After`` (seconds) up to ``max_delay``. Non-idempotent
    operations are never retried, so a retried request cannot duplicate a write.
    """

    max_retries: int = 2
    base_delay: float = 0.5
    max_delay: float = 8.0
    statuses: frozenset[int] = RETRY_STATUSES
    sleep: Callable[[float], None] = field(default=time.sleep)

    def __call__(self, request: HttpRequest, context: CallContext, call_next: Next) -> HttpResponse:
        """Send, retrying while the policy allows."""
        attempt = 0
        while True:
            try:
                response = call_next(request)
            except TransportError:
                if not self._may_retry(context, attempt):
                    raise
                self.sleep(self._delay(attempt, None))
                attempt += 1
                continue
            if response.status not in self.statuses or not self._may_retry(context, attempt):
                return response
            self.sleep(self._delay(attempt, response.header("Retry-After")))
            attempt += 1

    def _may_retry(self, context: CallContext, attempt: int) -> bool:
        return context.operation.idempotent and attempt < self.max_retries

    def _delay(self, attempt: int, retry_after: str | None) -> float:
        if retry_after is not None and retry_after.strip().isdigit():
            return min(float(retry_after), self.max_delay)
        backoff = self.base_delay * (2**attempt)
        return min(backoff * (0.5 + random.random() / 2), self.max_delay)  # noqa: S311


def chain(middleware: Sequence[Middleware], context: CallContext, terminal: Next) -> Next:
    """Compose middleware so the first item is outermost."""
    handler = terminal
    for item in reversed(middleware):
        handler = _bind(item, context, handler)
    return handler


def _bind(item: Middleware, context: CallContext, inner: Next) -> Next:
    return lambda request: item(request, context, inner)
