"""One error shape for every API failure.

    {"detail": "<summary>", "type": "<kind>", "errors": [{"field": "email", "message": "..."}]}

``errors`` is always present (possibly empty). ``field`` uses dotted paths relative to
the request body or query (``scores.0.score``), so the frontend can map each entry onto
the matching form control.
"""
import re
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class APIError(HTTPException):
    def __init__(
        self,
        status_code: int,
        detail: str,
        errors: list[dict[str, str]] | None = None,
        error_type: str = "http_error",
    ) -> None:
        super().__init__(status_code=status_code, detail=detail)
        self.errors = errors or []
        self.error_type = error_type


def field_error(field: str, message: str, status_code: int = 422) -> APIError:
    return APIError(
        status_code=status_code,
        detail=message,
        errors=[{"field": field, "message": message}],
        error_type="validation_error" if status_code == 422 else "http_error",
    )


def _field_path(loc: tuple[Any, ...]) -> str:
    parts = [str(p) for p in loc]
    if parts and parts[0] in ("body", "query", "path", "header", "cookie", "form"):
        parts = parts[1:]
    return ".".join(parts) or "__root__"


def _clean_message(msg: str) -> str:
    for prefix in ("Value error, ", "Assertion failed, "):
        if msg.startswith(prefix):
            return msg[len(prefix):]
    return msg


_SCOPED_MESSAGE = re.compile(r"^(?P<field>[a-z_][a-z0-9_.]*): (?P<message>.+)$", re.DOTALL)


def validation_errors(exc: RequestValidationError) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for err in exc.errors():
        field = _field_path(tuple(err.get("loc", ())))
        message = _clean_message(err.get("msg", "Invalid value"))
        # Cross-field model validators raise "field: message" so the error can still be
        # attached to a specific control instead of the form as a whole.
        scoped = _SCOPED_MESSAGE.match(message)
        if scoped:
            prefix = field if field != "__root__" else ""
            field = ".".join(p for p in (prefix, scoped["field"]) if p)
            message = scoped["message"]
        out.append({"field": field, "message": message})
    return out


def error_body(detail: str, error_type: str, errors: list[dict[str, str]]) -> dict[str, Any]:
    return {"detail": detail, "type": error_type, "errors": errors}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def _validation(_request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = validation_errors(exc)
        return JSONResponse(
            status_code=422,
            content=error_body("Validation failed", "validation_error", errors),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        errors = getattr(exc, "errors", [])
        error_type = getattr(exc, "error_type", "http_error")
        detail = exc.detail if isinstance(exc.detail, str) else "Request failed"
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(detail, error_type, errors),
            headers=getattr(exc, "headers", None),
        )
