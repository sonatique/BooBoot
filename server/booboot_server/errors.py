"""Errors that map to HTTP responses."""


class ApiError(Exception):
    status = 500
    code = "error"

    def __init__(self, message, code=None, **info):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.info = info

    def to_dict(self):
        d = {"error": self.code, "message": self.message}
        d.update(self.info)
        return d


class BadRequest(ApiError):
    status = 400
    code = "bad_request"


class NoSession(ApiError):
    status = 401
    code = "no_session"


class NotFound(ApiError):
    status = 404
    code = "not_found"


class Conflict(ApiError):
    status = 409
    code = "conflict"


class Busy(ApiError):
    """Another client holds the session."""

    status = 423
    code = "busy"


class HardwareError(ApiError):
    status = 500
    code = "hardware_error"


class Unavailable(ApiError):
    """Hardware is missing or not configured."""

    status = 503
    code = "unavailable"
