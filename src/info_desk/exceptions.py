"""Stable service errors exposed to MCP and CLI clients."""

from __future__ import annotations

from typing import Any


class InfoDeskError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}
        self.retry_after_seconds = retry_after_seconds


class ValidationError(InfoDeskError):
    def __init__(self, message: str) -> None:
        super().__init__("INVALID_ARGUMENT", message)


class NotFoundError(InfoDeskError):
    def __init__(self, message: str) -> None:
        super().__init__("NOT_FOUND", message)


class RevisionConflictError(InfoDeskError):
    def __init__(self, item_id: int, current_revision: int) -> None:
        super().__init__(
            "REVISION_CONFLICT",
            "条目已更新，请重新读取后再提交。",
            details={"item_id": item_id, "current_revision": current_revision},
        )


class UpstreamError(InfoDeskError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(
            code,
            message,
            retryable=code in {"UPSTREAM_TIMEOUT", "UPSTREAM_UNAVAILABLE", "RATE_LIMITED"},
            retry_after_seconds=retry_after_seconds,
        )
