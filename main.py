from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.middleware.cors import CORSMiddleware

from config import settings
from database import storage
from routers import all_routers

app = FastAPI(
    title="OpenGeode Index API",
    version="0.59.1",
    description="Reimplimentation of the Geode SDK index in Python!",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def add_private_network_access_headers(request: Request, call_next):
    if request.method == "OPTIONS":
        response = await call_next(request)
        response.headers["Access-Control-Allow-Private-Network"] = "true"
        return response
        
    response = await call_next(request)
    response.headers["Access-Control-Allow-Private-Network"] = "true"
    return response

@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": str(exc.detail), "payload": ""},
        headers=getattr(exc, "headers", None),
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    messages = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", []) if p not in ("body", "query", "path"))
        msg = err.get("msg", "Invalid value")
        messages.append(f"{loc}: {msg}" if loc else msg)
    return JSONResponse(
        status_code=404,
        content={"error": "; ".join(messages) or "Invalid request", "payload": ""},
    )

@app.on_event("startup")
def startup_event():
    storage.init_db()
    app.mount("/uploads", StaticFiles(directory=settings.MODS_STORAGE_DIR), name="uploads")

for router in all_routers:
    app.include_router(router)

@app.get("/", response_class=PlainTextResponse, tags=["health"], summary="Health check endpoint")
def root():
    return """
 _____                                                                        _____
( ___ )----------------------------------------------------------------------( ___ )
 |   |                                                                        |   |
 |   |   __          __  _                            _                       |   |
 |   |   \ \        / / | |                          | |                      |   |
 |   |    \ \  /\  / /__| | ___ ___  _ __ ___   ___  | |_ ___                 |   |
 |   |     \ \/  \/ / _ \ |/ __/ _ \| '_ ` _ \ / _ \ | __/ _ \                |   |
 |   |      \  /\  /  __/ | (_| (_) | | | | | |  __/ | || (_) |               |   |
 |   |     __\/  \/ \___|_|\___\___/|_|_|_| |_|\___|  \__\___/ _              |   |
 |   |    / __ \                  / ____|              | |    | |             |   |
 |   |   | |  | |_ __   ___ _ __ | |  __  ___  ___   __| | ___| |             |   |
 |   |   | |  | | '_ \ / _ \ '_ \| | |_ |/ _ \/ _ \ / _` |/ _ \ |             |   |
 |   |   | |__| | |_) |  __/ | | | |__| |  __/ (_) | (_| |  __/_|             |   |
 |   |    \____/| .__/ \___|_| |_|\_____|\___|\___/ \__,_|\___(_)             |   |
 |   |          | |                                                           |   |
 |   |          |_|                                                           |   |
 |___|                                                                        |___|
(_____)----------------------------------------------------------------------(_____)
Based on Geode Index v0.59.1
API at /docs
OpenGeode endpoints at /opengeode
Recreated in python!
Made by BlueToadMaker :3
    """


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host=settings.HOST, port=settings.PORT, reload=True)