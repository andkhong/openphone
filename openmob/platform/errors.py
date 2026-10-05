"""Domain errors raised by V1Service. Each front door maps them: REST to HTTP status codes,
MCP to tool errors (and HTTP 401 before a session starts)."""

from __future__ import annotations


class ServiceError(Exception):
    status = 400  # the HTTP status REST answers with
    code: str | None = None  # a stable machine-readable reason, when there is one

    def __init__(self, message: str, *, code: str | None = None, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.message = message
        self.code = code or self.code
        self.headers = headers or {}


class BadRequest(ServiceError):
    status = 400


class Unauthorized(ServiceError):
    status = 401

    def __init__(self, message: str, **kw):
        super().__init__(message, headers={"WWW-Authenticate": "Bearer"}, **kw)


class Forbidden(ServiceError):
    status = 403


class NotFound(ServiceError):
    status = 404


class Conflict(ServiceError):
    status = 409


class RateLimited(ServiceError):
    status = 429

    def __init__(self, message: str, retry_after: int | None = None, **kw):
        super().__init__(message, headers={"Retry-After": str(retry_after)} if retry_after else None, **kw)


class NotEnabled(ServiceError):
    status = 501


class Unavailable(ServiceError):
    status = 503
