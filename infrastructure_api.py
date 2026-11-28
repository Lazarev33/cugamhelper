"""Публичный REST-контракт Cugam Helper и точка входа ASGI."""

import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
import os
from pathlib import Path
import sqlite3
from time import monotonic
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator
from starlette.exceptions import HTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware


from infrastructure import Infrastructure, IntegrationFeatures, InfraError, SOCKET, revision
from private_process import disable_core_dumps
from safe_metrics import Metrics
from telegram_bridge import Exports, MAX_BODY, dispatch, permitted, split_lines

ROOT = Path(__file__).resolve().parent
API = "/api/v1"
VERSION = (ROOT / "VERSION").read_text().strip()
REVISION_PATH = ROOT / "REVISION"
Key = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{24}$")]
RequestKey = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{16,64}$")]
Secret = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{43}$")]
UserID = Annotated[int, Field(gt=0, lt=2**52)]
Line = Annotated[str, StringConstraints(min_length=1, max_length=512)]
Lines = Annotated[list[Line], Field(max_length=2000)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class Error(Model):
    code: str


class Health(Model):
    status: str


class Version(Model):
    version: str
    api: int


class LinkStart(Model):
    telegram_id: UserID
    device_name: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    app_version: Annotated[str, StringConstraints(max_length=32)] = ""
    proof_sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

    @field_validator("device_name", "app_version")
    @classmethod
    def printable(cls, text):
        if text and (not text.isprintable() or not text.strip()):
            raise ValueError("Недопустимый текст")
        return text


class Link(Model):
    challenge: Key
    telegram_url: str
    expires_at: int


class LinkExchange(Model):
    challenge: Key
    verifier: Secret


class Refresh(Model):
    refresh_token: Secret


class Session(Model):
    access_token: Secret
    refresh_token: Secret
    expires_in: int
    device_id: Key


class Profile(Model):
    telegram_id: UserID
    device_id: Key
    can_export: bool


class Device(Model):
    id: Key
    name: str
    app_version: str
    created: int
    seen: int
    revoked: int | None
    platform: Literal["android"] = "android"


class Devices(Model):
    items: list[Device]
    next_page: int | None


class ProductCode(Model):
    id: int
    name: str
    value: str
    format: str
    revision: int


class CatalogPayload(Model):
    revision: str
    items: list[ProductCode]


class CatalogVersion(Model):
    revision: str


class FeaturePayload(Model):
    revision: str
    common: dict[str, bool]
    clients: dict[str, bool]
    android: dict[str, bool]
    android_client_released: Literal[False] = False


class Sync(Model):
    api_major: int = 1
    catalog_revision: str
    features_revision: str
    device_id: Key
    access: Literal["active"] = "active"
    offline_lease_seconds: Literal[0] = 0


class NsuReport(Model):
    matched: Annotated[int, Field(ge=0, le=4000)]
    expected: Annotated[int, Field(ge=1, le=4000)]
    missing_count: Annotated[int, Field(ge=0, le=4000)]
    extra_count: Annotated[int, Field(ge=0, le=4000)]
    missing: Lines = Field(default_factory=list)
    extra: Lines = Field(default_factory=list)

    @field_validator("extra", "missing")
    @classmethod
    def values(cls, lines):
        return clean_lines(lines)


def clean_lines(lines):
    if any(not value.isprintable() for value in lines):
        raise ValueError("Рабочая строка должна содержать только печатные символы")
    return lines


class NsuExport(Model):
    request_id: RequestKey
    mode: Literal["pairs", "two_passes"]
    report: NsuReport
    first: Lines
    second: Lines = Field(default_factory=list)

    @field_validator("first", "second")
    @classmethod
    def values(cls, lines):
        return clean_lines(lines)

    def messages(self):
        report = self.report
        if (not self.first or (self.mode == "two_passes") != bool(self.second)
                or report.matched > report.expected or len(report.missing) > report.missing_count
                or len(report.extra) > report.extra_count or sum(map(len, (self.first, self.second,
                                                                        report.missing, report.extra))) > 4000):
            raise InfraError(422, "invalid_result")
        matches = not (report.missing_count or report.extra_count or report.matched != report.expected)
        summary = [
            "НСУ — результат сверки", "Режим: пары" if self.mode == "pairs" else "Режим: два прохода",
            f"Совпало: {report.matched}/{report.expected}",
            f"Недостающих экземпляров: {report.missing_count}", f"Лишних экземпляров: {report.extra_count}",
            "Статус: совпадает" if matches else "Статус: обнаружены расхождения",
        ]
        if report.missing:
            summary.extend(("Недостающие значения:", *report.missing))
        if report.extra:
            summary.extend(("Лишние значения:", *report.extra))
        return split_lines(summary) + split_lines(self.first) + split_lines(self.second)


class Delivery(Model):
    request_id: RequestKey
    status: Literal["queued", "sending", "delivered", "partially_delivered", "failed",
                    "delivery_unknown", "cancelled", "expired"]
    confirmed_parts: int
    total_parts: int


@dataclass(frozen=True)
class WebSettings:
    host: str = "localhost"
    port: int = 8765
    local: bool = True

    @classmethod
    def from_env(cls):
        host = os.getenv("CUGAM_HOST", "localhost").lower()
        port = int(os.getenv("CUGAM_PORT", "8765"))
        if not 1024 <= port <= 65535 or not all(part.isascii() and part.replace("-", "").isalnum()
                                                for part in host.split(".")):
            raise ValueError("Некорректный адрес API")
        return cls(host, port, host == "localhost")

    @property
    def origin(self):
        return f"http://localhost:{self.port}" if self.local else f"https://{self.host}"


def checked_etag(request, response, value):
    tag = '"' + value + '"'
    response.headers["ETag"] = tag
    if request.headers.get("if-none-match") == tag:
        return Response(status_code=304, headers={"ETag": tag})
    return None


class Limits:
    def __init__(self):
        self.clients = OrderedDict()
        self.in_flight = 0

    def accept(self, client, group):
        now = monotonic()
        while self.clients and next(iter(self.clients.values()))[1] <= now:
            self.clients.popitem(last=False)
        key = client, group
        maximum = {"link": 12, "exchange": 60, "other": 180}[group]
        if key not in self.clients and len(self.clients) >= 8192:
            return False
        count, expiration = self.clients.get(key, (0, now + 60))
        if count >= maximum:
            return False
        self.clients[key] = count + 1, expiration
        return True


class Boundary:
    def __init__(self, app, settings, metrics):
        self.app, self.settings, self.limits, self.metrics = app, settings, Limits(), metrics

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def respond(message):
            if message["type"] == "http.response.start":
                self.metrics.add("http")
                status = message["status"]
                if status in {401, 403}:
                    self.metrics.add("http_denied")
                if status == 429:
                    self.metrics.add("throttled")
                if status >= 500:
                    self.metrics.add("http_error")
                headers = message.setdefault("headers", [])
                headers.extend([
                    (b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"same-origin"), (b"x-frame-options", b"DENY"),
                    (b"content-security-policy", b"default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
                ])
            await send(message)

        async def reject(status, code):
            await JSONResponse({"code": code}, status_code=status)(scope, receive, respond)

        if not self.settings.local and scope["scheme"] != "https":
            return await reject(400, "https_required")
        path = scope["path"]
        client = (scope.get("client") or ("local",))[0]
        group = ("link" if path.endswith(("/start", "/recovery")) else
                 "exchange" if path.endswith(("/exchange", "/refresh")) else "other")
        if self.limits.in_flight >= 24 or not self.limits.accept(client, group):
            return await reject(429, "rate_limit")
        scope["state"] = scope.get("state", {})
        self.limits.in_flight += 1
        try:
            if scope["method"] not in {"POST", "PUT", "PATCH", "DELETE"}:
                return await self.app(scope, receive, respond)
            maximum = MAX_BODY if path == API + "/telegram/exports" else 16 * 1024
            headers = dict(scope["headers"])
            try:
                length = int(headers.get(b"content-length", b"0"))
            except ValueError:
                return await reject(400, "invalid_length")
            if length < 0 or length > maximum:
                return await reject(413, "body_limit")
            content = bytearray()

            async def buffer():
                while True:
                    event = await receive()
                    if event["type"] != "http.request":
                        return False
                    content.extend(event.get("body", b""))
                    if len(content) > maximum:
                        return None
                    if not event.get("more_body"):
                        return True

            try:
                received = await asyncio.wait_for(buffer(), 10)
            except asyncio.TimeoutError:
                return await reject(408, "body_timeout")
            if received is None:
                return await reject(413, "body_limit")
            if received is False:
                return
            delivered = False

            async def replay():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": bytes(content)}
                return await receive()

            if length or content:
                form = headers.get(b"content-type", b"").split(b";")[0]
                expected = b"application/json" if path.startswith(API + "/") else b"application/x-www-form-urlencoded"
                if form != expected:
                    return await reject(415, "content_type")
            return await self.app(scope, replay, respond)
        finally:
            self.limits.in_flight -= 1


def create_app(service=None, *, config=None, transport=None):
    service = service or Infrastructure()
    config = config or WebSettings.from_env()
    features = IntegrationFeatures(service)

    async def send(session, text):
        return await dispatch(SOCKET, session, text)

    metrics = Metrics()
    exports = Exports(lambda sid: permitted(service, sid, features), transport or send, metrics=metrics)

    @asynccontextmanager
    async def lifespan(_):
        service.prepare()
        worker = asyncio.create_task(exports.worker())
        try:
            yield
        finally:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
            for record in exports.records.values():
                exports.finish(record, "cancelled")

    app = FastAPI(title="Cugam Helper", version="1.0", docs_url=None, redoc_url=None,
                  openapi_url=API + "/openapi.json", lifespan=lifespan,
                  responses={400: {"model": Error}, 401: {"model": Error}, 403: {"model": Error},
                             404: {"model": Error}, 409: {"model": Error}, 413: {"model": Error},
                             415: {"model": Error}, 422: {"model": Error}, 429: {"model": Error},
                             503: {"model": Error}})
    app.state.exports = exports
    app.state.metrics = metrics
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[config.host, "127.0.0.1"] if config.local else [config.host])
    app.add_middleware(Boundary, settings=config, metrics=metrics)

    @app.exception_handler(InfraError)
    async def infra_error(_, error):
        return JSONResponse({"code": error.code}, status_code=error.status)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_, error):
        return JSONResponse({"code": "invalid_request"}, status_code=422)

    @app.exception_handler(sqlite3.Error)
    async def database_unavailable(_, error):
        return JSONResponse({"code": "database_unavailable"}, status_code=503)

    @app.exception_handler(HTTPException)
    async def http_error(_, error):
        return JSONResponse({"code": "http_error"}, status_code=error.status_code)

    bearer = HTTPBearer(auto_error=False)

    def mobile(auth: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
        with service.repository() as repo:
            return service.identify(repo, auth.credentials if auth else None, "android")

    def policy():
        common, client = features.snapshot()
        android = {key.removeprefix("android_"): value for key, value in client.items()
                   if key.startswith("android_")}
        android["catalog"] &= common.enabled("barcode_catalog")
        android["catalog_sync"] &= android["catalog"]
        android["camera"] &= common.enabled("barcode_scan")
        android["clean"] &= common.permits("clean")
        android["sort"] &= common.permits("asc")
        android["reconcile"] &= any(common.permits(mode) for mode in ("check", "pairs", "ref", "align"))
        android["documents"] &= any(common.input_enabled(kind) for kind in ("xlsx", "docx", "txt"))
        android["duplicates"] &= any(common.permits(mode) for mode in ("dups", "unique"))
        android["export"] &= client["telegram_export"] and android["nsu"] and common.input_enabled("text")
        content = {"common": common.options(), "clients": client, "android": android}
        return {"revision": revision(content), **content}

    def versioned_catalog(state):
        if not state["android"]["catalog_sync"]:
            return {"revision": revision([]), "items": []}
        with service.repository() as repo:
            codes = [dict(row) for row in repo.db.execute(
                "SELECT id,name,value,format,revision FROM barcodes WHERE active=1 ORDER BY id")]
        return {"revision": revision(codes), "items": codes}

    @app.get(API + "/health")
    def health() -> Health:
        return Health(status="ok")

    @app.get(API + "/version")
    def version() -> Version:
        return Version(version=VERSION, api=1)

    @app.post(API + "/auth/link/start", status_code=201)
    def link_start(data: LinkStart) -> Link:
        challenge, expires, username = service.link_start(
            data.telegram_id, data.device_name, data.app_version, data.proof_sha256, "android")
        if not username:
            raise InfraError(503, "telegram_not_configured")
        return Link(challenge=challenge, telegram_url=f"https://t.me/{username}?start=link_{challenge}", expires_at=expires)

    @app.post(API + "/auth/link/exchange", status_code=201)
    def link_exchange(data: LinkExchange) -> Session:
        return Session.model_validate(service.exchange(data.challenge, data.verifier, "android"))

    @app.post(API + "/auth/refresh")
    def refresh(data: Refresh) -> Session:
        return Session.model_validate(service.refresh(data.refresh_token))

    @app.get(API + "/auth/me")
    def me(identity=Depends(mobile)) -> Profile:
        return Profile(telegram_id=identity.uid, device_id=identity.device, can_export=policy()["android"]["export"])

    @app.post(API + "/auth/logout", status_code=204)
    def logout(identity=Depends(mobile)):
        service.revoke(identity.uid, identity.uid, identity.device)
        return Response(status_code=204)

    @app.get(API + "/devices")
    def devices(page: Annotated[int, Query(ge=0, le=1000)] = 0, identity=Depends(mobile)) -> Devices:
        with service.repository() as repo:
            rows = service.devices(repo, identity.uid, page)
        return Devices(items=[Device.model_validate({key: v for key, v in row.items() if key != "uid"})
                              for row in rows[:20]], next_page=page + 1 if len(rows) > 20 else None)

    @app.delete(API + "/devices/{device}", status_code=204)
    def revoke(device: Key, identity=Depends(mobile)):
        service.revoke(identity.uid, identity.uid, device)
        return Response(status_code=204)

    @app.get(API + "/features", response_model=FeaturePayload, responses={304: {"description": "Ревизия совпадает"}})
    def get_features(request: Request, response: Response, identity=Depends(mobile)):
        value = policy()
        return checked_etag(request, response, value["revision"]) or value

    @app.get(API + "/catalog", response_model=CatalogPayload, responses={304: {"description": "Ревизия совпадает"}})
    def catalog(request: Request, response: Response, identity=Depends(mobile)):
        value = versioned_catalog(policy())
        return checked_etag(request, response, value["revision"]) or value

    @app.get(API + "/catalog/version")
    def catalog_version(identity=Depends(mobile)) -> CatalogVersion:
        return CatalogVersion(revision=versioned_catalog(policy())["revision"])

    @app.get(API + "/sync", response_model=Sync, responses={304: {"description": "Ревизия совпадает"}})
    def sync(request: Request, response: Response, identity=Depends(mobile)):
        flags = policy()
        value = Sync(catalog_revision=versioned_catalog(flags)["revision"], features_revision=flags["revision"],
                     device_id=identity.device)
        return checked_etag(request, response, revision(value.model_dump())) or value

    @app.post(API + "/telegram/exports", status_code=202)
    async def export(data: NsuExport, identity=Depends(mobile)) -> Delivery:
        if not permitted(service, identity.id, features):
            raise InfraError(403, "export_disabled")
        record = exports.submit(identity, data.request_id, data.messages())
        return Delivery.model_validate(record.view(data.request_id))

    @app.delete(API + "/telegram/exports/{request_id}")
    async def cancel(request_id: RequestKey, identity=Depends(mobile)) -> Delivery:
        record = exports.get(identity, request_id)
        if record.status != "queued":
            raise InfraError(409, "export_not_queued")
        exports.finish(record, "cancelled")
        return Delivery.model_validate(record.view(request_id))

    @app.get(API + "/telegram/exports/{request_id}")
    async def delivery(request_id: RequestKey, identity=Depends(mobile)) -> Delivery:
        return Delivery.model_validate(exports.get(identity, request_id).view(request_id))

    from control_center import install
    install(app, service, features, config)
    return app


def run():
    import uvicorn
    disable_core_dumps()
    settings = WebSettings.from_env()
    uvicorn.run(create_app(config=settings), host="127.0.0.1", port=settings.port, workers=1,
                proxy_headers=not settings.local, forwarded_allow_ips="127.0.0.1",
                access_log=False, limit_concurrency=32, timeout_keep_alive=5)


if __name__ == "__main__":
    run()
