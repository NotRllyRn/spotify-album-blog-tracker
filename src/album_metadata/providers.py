"""Provider retry, error, and circuit-breaker primitives."""

import json
import logging
import socket
import time
import urllib.error
import urllib.request

from album_metadata.common import safe_error as _safe_error

log = logging.getLogger("post_to_album")

class ProviderError(RuntimeError):
    """A safe, structured provider failure suitable for unresolved diagnostics."""

    def __init__(self, provider: str, operation: str, failure_kind: str,
                 message: str, *, retryable: bool, attempts: int,
                 http_status: int | None = None,
                 circuit_state: str = "closed"):
        super().__init__(message)
        self.provider = provider
        self.operation = operation
        self.failure_kind = failure_kind
        self.retryable = retryable
        self.attempts = attempts
        self.http_status = http_status
        self.circuit_state = circuit_state

    def diagnostic(self, code: str) -> dict:
        details = {
            "provider": self.provider,
            "operation": self.operation,
            "failure_kind": self.failure_kind,
            "retryable": self.retryable,
            "attempts": self.attempts,
            "circuit_state": self.circuit_state,
        }
        if self.http_status is not None:
            details["http_status"] = self.http_status
        return {"code": code, "message": _safe_error(self), "details": details}


class SpotifyProviderError(ProviderError):
    def __init__(self, message: str, *, operation: str = "response",
                 failure_kind: str = "malformed_response", retryable: bool = False,
                 attempts: int = 1, http_status: int | None = None,
                 circuit_state: str = "closed"):
        super().__init__("spotify", operation, failure_kind, message,
                         retryable=retryable, attempts=attempts,
                         http_status=http_status, circuit_state=circuit_state)


class LastFMProviderError(ProviderError):
    def __init__(self, message: str, *, operation: str = "response",
                 failure_kind: str = "malformed_response", retryable: bool = False,
                 attempts: int = 1, http_status: int | None = None,
                 circuit_state: str = "closed"):
        super().__init__("lastfm", operation, failure_kind, message,
                         retryable=retryable, attempts=attempts,
                         http_status=http_status, circuit_state=circuit_state)


class ProviderCircuit:
    """Stop a finite batch from hammering one persistently failing provider."""

    def __init__(self, provider: str, threshold: int = 3):
        self.provider = provider
        self.threshold = threshold
        self.consecutive_failures = 0
        self.is_open = False
        self.blocked_until = 0.0
        self.request_counts: dict[str, int] = {}

    def before_request(self, operation: str) -> None:
        if self.is_open:
            cls = SpotifyProviderError if self.provider == "spotify" else LastFMProviderError
            raise cls(f"{self.provider.title()} {operation} skipped: provider circuit is open.",
                      operation=operation, failure_kind="circuit_open", retryable=True,
                      attempts=0, circuit_state="open")

    def record_success(self) -> None:
        self.consecutive_failures = 0

    def record_transient_failure(self) -> None:
        self.consecutive_failures += 1
        self.is_open = self.is_open or self.consecutive_failures >= self.threshold

    def record_request(self, operation: str) -> None:
        self.request_counts[operation] = self.request_counts.get(operation, 0) + 1

    def block_for(self, seconds: int) -> None:
        self.is_open = True
        self.blocked_until = time.time() + seconds


def _retry_delay(exc: urllib.error.HTTPError, fallback: int) -> int:
    if exc.code != 429:
        return fallback
    try:
        value = exc.headers.get("Retry-After", fallback) if exc.headers else fallback
        return max(0, int(value))
    except (TypeError, ValueError):
        return fallback


def _http_error_reason(exc: urllib.error.HTTPError) -> str | None:
    try:
        payload = json.loads(exc.read())
        reason = payload.get("error", {}).get("reason")
        return reason if isinstance(reason, str) else None
    except (AttributeError, OSError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _request_json(req: urllib.request.Request, *, provider: str,
                  operation: str, circuit: ProviderCircuit,
                  timeout: int = 30, max_attempts: int = 3) -> dict:
    """Read one JSON object with bounded retries and safe error classification."""
    circuit.before_request(operation)
    error_cls = SpotifyProviderError if provider == "spotify" else LastFMProviderError
    for attempt in range(1, max_attempts + 1):
        try:
            circuit.record_request(operation)
            with urllib.request.urlopen(req, timeout=timeout) as response:
                raw = response.read()
            circuit.record_success()
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise error_cls(
                    f"{provider.title()} {operation} returned malformed JSON.",
                    operation=operation, failure_kind="malformed_response",
                    retryable=False, attempts=attempt) from exc
            if not isinstance(data, dict):
                raise error_cls(
                    f"{provider.title()} {operation} returned a malformed response.",
                    operation=operation, failure_kind="malformed_response",
                    retryable=False, attempts=attempt)
            return data
        except urllib.error.HTTPError as exc:
            retryable = exc.code == 429 or 500 <= exc.code <= 599
            delay = _retry_delay(exc, 2 ** (attempt - 1))
            if (provider == "spotify" and exc.code == 429 and
                    _http_error_reason(exc) == "QUOTA_EXCEEDED"):
                circuit.block_for(delay)
                log.error("%s %s quota exhausted; retry after %ds",
                          provider.title(), operation, delay)
            elif retryable and attempt < max_attempts:
                log.warning("%s %s HTTP %d, retrying in %ds",
                            provider.title(), operation, exc.code, delay)
                time.sleep(delay)
                continue
            if retryable:
                circuit.record_transient_failure()
            state = "open" if circuit.is_open else "closed"
            raise error_cls(
                f"{provider.title()} {operation} failed after {attempt} attempt"
                f"{'s' if attempt != 1 else ''}: HTTP {exc.code}.",
                operation=operation, failure_kind="http_status",
                retryable=retryable, attempts=attempt, http_status=exc.code,
                circuit_state=state) from exc
        except (TimeoutError, socket.timeout) as exc:
            kind, cause = "timeout", exc
        except urllib.error.URLError as exc:
            kind = ("timeout" if isinstance(exc.reason, (TimeoutError, socket.timeout))
                    else "network")
            cause = exc
        except OSError as exc:
            # Some transports raise connection failures directly, including
            # while reading a response, rather than wrapping them in URLError.
            kind, cause = "network", exc
        if attempt < max_attempts:
            delay = 2 ** (attempt - 1)
            log.warning("%s %s %s, retrying in %ds", provider.title(), operation, kind, delay)
            time.sleep(delay)
            continue
        circuit.record_transient_failure()
        state = "open" if circuit.is_open else "closed"
        raise error_cls(
            f"{provider.title()} {operation} failed after {attempt} attempts: {kind} error.",
            operation=operation, failure_kind=kind, retryable=True,
            attempts=attempt, circuit_state=state) from cause
    raise RuntimeError("unreachable")
