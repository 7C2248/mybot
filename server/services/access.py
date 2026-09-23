"""Local browser boundary: explicit origins, host checks and a header for writes."""

from starlette.datastructures import Headers
from starlette.responses import JSONResponse


class LocalAccessMiddleware:
    def __init__(self, app, allowed_origins: tuple[str, ...]):
        self.app = app
        self.allowed_origins = allowed_origins

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = Headers(scope=scope)
            origins = headers.getlist("origin")
            if len(origins) > 1 or (origins and origins[0] not in self.allowed_origins):
                response = JSONResponse({"error": {"code": "origin_forbidden", "message": "不允许此请求来源。"}},
                                        status_code=403)
                return await response(scope, receive, send)
            if (scope["method"] not in ("GET", "HEAD", "OPTIONS")
                    and headers.get("x-mybot-client") != "mybot-desktop"):
                response = JSONResponse({"error": {"code": "client_header_required",
                                                   "message": "写请求需要 X-Mybot-Client: mybot-desktop。"}},
                                        status_code=403)
                return await response(scope, receive, send)
        await self.app(scope, receive, send)
