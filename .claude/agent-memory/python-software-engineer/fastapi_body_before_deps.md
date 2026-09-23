---
name: fastapi-body-before-deps
description: FastAPI parses multipart/form bodies before solving Depends, so header-based early rejects need a custom APIRoute route_class
metadata:
  type: reference
---

FastAPI's request handler awaits `request.form()` (spooling uploads to disk) BEFORE resolving
endpoint/router `dependencies=`. A `Depends` that checks `Content-Length` is therefore too late.

**How to apply:** for early rejects on upload endpoints use `APIRouter(route_class=...)` with an
`APIRoute.get_route_handler` override (see `api/workspace_import.py::_UploadCapRoute`). Auth 401 still
precedes it because auth runs in Starlette `AuthenticationMiddleware`. Return type must be
`Callable[[Request], Coroutine[Any, Any, Response]]` or mypy strict flags the override.
