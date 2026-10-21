"""Errors the service raises. The HTTP layer maps each to a status code and a JSON body."""


class ServiceError(Exception):
    status = 400
    code = "invalid"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class Invalid(ServiceError):
    status, code = 400, "invalid"


class Unauthorized(ServiceError):
    status, code = 401, "unauthorized"


class Forbidden(ServiceError):
    status, code = 403, "forbidden"


class NotFound(ServiceError):
    status, code = 404, "not_found"


class Conflict(ServiceError):
    status, code = 409, "conflict"


class TooLarge(ServiceError):
    status, code = 413, "too_large"


class RateLimited(ServiceError):
    status, code = 429, "rate_limited"
