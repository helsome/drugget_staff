from __future__ import annotations

from typing import Any


class AppError(Exception):
    status_code = 400
    code = "application_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class InvalidStateError(ConflictError):
    code = "invalid_state_transition"


class CollectorAccessError(AppError):
    status_code = 503
    code = "collector_access_error"

    def __init__(self, message: str, *, collection_status: str, details: dict[str, Any] | None = None):
        super().__init__(message, details=details)
        self.collection_status = collection_status


class AmbiguousControlPrice(AppError):
    code = "control_price_ambiguous"

