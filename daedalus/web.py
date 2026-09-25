# -*- coding: utf-8 -*-
"""HTTP interface for the current Daedalus state.

The app is deliberately thin: it translates the existing domain objects into JSON
and leaves layout and motion to the HTML page.
"""
from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Callable, Iterator

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from . import actions
from .chain import chain, who_was_here
from .state import build
from .store import PgStore
from .timeutil import display


HTML_PATH = Path(__file__).with_name("web") / "index.html"
_STATE_RE = re.compile(
    r'(<script\s+id="state"\s+type="application/json">).*?(</script>)',
    re.DOTALL,
)


class AnnotationInput(BaseModel):
    """Only the allowed, length-limited annotation fields."""

    model_config = ConfigDict(extra="forbid")

    outlet: str | None = Field(default=None, max_length=120)
    room: str | None = Field(default=None, max_length=120)
    note: str | None = Field(default=None, max_length=2000)
    owner: str | None = Field(default=None, max_length=120)
    name: str | None = Field(default=None, max_length=120)
    expected: str | None = Field(default=None, max_length=300)


class ActionTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str = Field(max_length=15)


class ActionPort(ActionTarget):
    port: int


class ActionWol(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mac: str = Field(max_length=17)
    ip: str | None = Field(default=None, max_length=15)


def _json_value(value):
    """Make domain objects JSON-serialisable for the detail response, losing little."""
    if isinstance(value, datetime):
        return display(value)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {k: _json_value(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {k: _json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(v) for v in value]
    return value


def _annotation_value(annotation) -> dict:
    if annotation is None:
        return {}
    return _json_value(annotation)


def embed_state(html: str, state: dict) -> str:
    """Embed the state as data, not as executable JavaScript.

    Escaping ``</`` prevents payload data from closing the script block early.
    JSON special characters stay parseable unchanged.
    """
    data = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    data = data.replace("</", "<\\/")
    # A function as the replacement matters: re.sub would otherwise interpret
    # backslashes from the JSON as group or escape syntax.
    replaced, count = _STATE_RE.subn(
        lambda m: m.group(1) + data + m.group(2), html, count=1
    )
    if count != 1:
        raise RuntimeError("index.html has no unique state placeholder")
    return replaced


def build_app(
    connect: Callable[[], object],
    store_factory: Callable[[object], object] = PgStore,
    root: str | None = None,
) -> FastAPI:
    """Build a testable app with injectable connection and store.

    Without `root` the site configuration is read on the first request (the root
    switch, networks and switches come from `daedalus.toml`)."""
    app = FastAPI(title="Daedalus")
    site: dict = {}

    def root_switch() -> str:
        if root is not None:
            return root
        if "root" not in site:
            site["root"] = _root_from_config()
        return site["root"]

    @contextmanager
    def store() -> Iterator[object]:
        # One connection per request is the more robust choice here: after a proxy
        # or database outage no dead connection can poison the next request. For
        # this small service, connecting is cheaper and easier to follow than an
        # own pool with revival.
        db = connect()
        try:
            yield store_factory(db)
        finally:
            close = getattr(db, "close", None)
            if callable(close):
                close()

    def current_state(s) -> dict:
        from datetime import timedelta
        from .timeutil import now
        t = now()
        return build(
            s,
            root=root_switch(),
            annotations=s.annotations.all(),
            sightings=s.sightings(),
            timestamp=t,
            health=s.port_health(t),
            transitions=s.transitions_since("link_change", t - timedelta(hours=24)),
        )

    @app.get("/", response_class=HTMLResponse)
    def start_page():
        with store() as s:
            content = embed_state(
                HTML_PATH.read_text(encoding="utf-8"),
                current_state(s),
            )
        return HTMLResponse(content)

    @app.get("/api/state")
    def api_state():
        with store() as s:
            return current_state(s)

    @app.get("/api/object/{key:path}")
    def api_object(key: str):
        with store() as s:
            path = chain(s, key, root_switch())
            links = [
                {
                    "kind": link.kind,
                    "name": link.name,
                    "port": link.port,
                    "measured": link.measured,
                }
                for link in path.links
            ]
            history = [_json_value(i) for i in s.history(key)]
            # MACs and special ids contain colons as well. Only the form
            # "node:port" is a meaningful backwards question.
            is_port = key.count(":") == 1 and not key.startswith(("ap:", "net:"))
            was_here = []
            if is_port:
                was_here = [
                    {"object": obj, "since": _json_value(since), "until": _json_value(until)}
                    for obj, since, until in who_was_here(s, key)
                ]
            return {
                "chain": links,
                "unambiguous": path.unambiguous,
                "complete": path.complete,
                "history": history,
                "was_here": was_here,
                "annotation": _annotation_value(s.annotations.read(key)),
            }

    @app.put("/api/annotation/{key:path}")
    def api_write_annotation(
        key: str,
        body: AnnotationInput,
        preferred_name: str | None = Header(
            default=None, alias="X-Forwarded-Preferred-Username"
        ),
        forwarded_user: str | None = Header(
            default=None, alias="X-Forwarded-User"
        ),
    ):
        with store() as s:
            annotation = s.annotations.write(
                key,
                changed_by=preferred_name or forwarded_user or "",
                **body.model_dump(exclude_unset=True),
            )
            return _annotation_value(annotation)

    # --- actions -----------------------------------------------------------------
    # POST with a JSON body only: a foreign browser tab cannot send that without a
    # CORS preflight, and this service does not answer one.
    def _action(function, *args):
        # The allowed networks come from the site configuration - make sure it has
        # been read even if no page was loaded since the start.
        root_switch()
        try:
            return function(*args)
        except actions.Refused as e:
            raise HTTPException(status_code=422, detail=str(e)) from e

    @app.post("/api/action/ping")
    def action_ping(body: ActionTarget):
        return _action(actions.ping, body.target)

    @app.post("/api/action/traceroute")
    def action_traceroute(body: ActionTarget):
        return _action(actions.traceroute, body.target)

    @app.post("/api/action/port")
    def action_port(body: ActionPort):
        return _action(actions.check_port, body.target, body.port)

    @app.post("/api/action/wol")
    def action_wol(body: ActionWol):
        return _action(actions.wake_on_lan, body.mac, body.ip)

    @app.post("/api/action/scan")
    def action_scan(body: ActionTarget):
        return _action(actions.start_scan, body.target)

    @app.get("/api/action/scan/{ident}")
    def action_scan_status(ident: str):
        return _action(actions.scan_status, ident)

    @app.delete("/api/action/scan/{ident}")
    def action_scan_cancel(ident: str):
        return _action(actions.cancel_scan, ident)

    @app.get("/api/action/capabilities")
    def action_capabilities():
        # The UI only shows the scan button if it is allowed.
        return {"scan": actions.scan_enabled()}

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/ready")
    def ready():
        try:
            db = connect()
            try:
                with db.cursor() as cursor:
                    cursor.execute("SELECT 1")
                    cursor.fetchone()
            finally:
                close = getattr(db, "close", None)
                if callable(close):
                    close()
        except Exception as exc:
            raise HTTPException(status_code=503, detail="database not ready") from exc
        return {"ok": True}

    return app


def _connect():
    """Build the production connection only when a request comes in."""
    dsn = os.environ.get("DAEDALUS_DSN")
    if not dsn:
        raise RuntimeError("DAEDALUS_DSN is not set")
    from .db import connect
    return connect(dsn)


def _root_from_config() -> str:
    from . import config as site_config
    cfg = site_config.load()
    site_config.apply(cfg)
    return cfg.root_switch


app = build_app(_connect)
