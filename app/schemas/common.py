"""Envelope estandar de respuesta (mismo contrato que BrokerOS)."""
from typing import Any, Optional

from fastapi.responses import JSONResponse
from pydantic import BaseModel, model_serializer


class ErrorDetail(BaseModel):
    code: str
    message: str
    detail: Optional[str] = None


class APIResponse(BaseModel):
    success: bool
    http_status: int
    message: Optional[str] = None
    data: Optional[Any] = None
    error: Optional[ErrorDetail] = None

    @model_serializer
    def serialize(self):
        if self.success:
            return {
                "success": True,
                "http_status": self.http_status,
                "message": self.message,
                "data": self.data,
            }
        return {
            "success": False,
            "http_status": self.http_status,
            "error": self.error.model_dump() if self.error else None,
        }


def error_response(exc) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status,
        content=APIResponse(
            success=False,
            http_status=exc.http_status,
            error=ErrorDetail(code=exc.code, message=exc.message, detail=exc.detail),
        ).model_dump(mode="json"),
    )
