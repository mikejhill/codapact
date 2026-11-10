"""Test doubles shared across layers: in-memory APIs, transports, and HTTP servers."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, override
from urllib.parse import parse_qs, urlsplit

from graphql import build_schema, graphql_sync

from codapact.core.net import HttpRequest, HttpResponse
from codapact.model.api import Api
from codapact.sources.base import SourceInput
from codapact.sources.graphql import GraphQLSource
from codapact.sources.loader import Fetcher
from codapact.sources.openapi import OpenApiSource

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
Handler = Callable[[HttpRequest], HttpResponse]


def json_response(status: int, body: Any, headers: dict[str, str] | None = None) -> HttpResponse:
    """Build a JSON response."""
    data = b"" if body is None else json.dumps(body).encode()
    return HttpResponse(status, {"Content-Type": "application/json", **(headers or {})}, data)


class FakeTransport:
    """Transport that records requests and answers through a handler."""

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.requests: list[HttpRequest] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        return self.handler(request)


def petstore_api() -> Api:
    """Translate the petstore example."""
    document = Fetcher().fetch(str(EXAMPLES / "petstore" / "openapi.yaml"))
    result = OpenApiSource().parse(SourceInput(document))
    assert result.api is not None, list(result.diagnostics)
    return result.api


def library_api(*, documents: bool = True) -> Api:
    """Translate the GraphQL library example."""
    fetcher = Fetcher()
    schema = fetcher.fetch(str(EXAMPLES / "library" / "schema.graphql"))
    companions = (
        (fetcher.fetch(str(EXAMPLES / "library" / "operations.graphql")),) if documents else ()
    )
    options = {"name": "Library", "url": "https://library.example.com/graphql", "auth": "bearer"}
    result = GraphQLSource().parse(SourceInput(schema, companions, options))
    assert result.api is not None, list(result.diagnostics)
    return result.api


class Petstore:
    """In-memory petstore implementing the example contract."""

    def __init__(self) -> None:
        self.pets: dict[int, dict[str, Any]] = {
            i: {"id": i, "name": name, "status": "available", "tag": None}
            for i, name in enumerate(["Miso", "Tofu", "Natto", "Udon", "Soba"], start=1)
        }

    def __call__(self, request: HttpRequest) -> HttpResponse:
        parts = urlsplit(request.url)
        path = parts.path.removeprefix("/v1")
        query = parse_qs(parts.query)
        if path == "/health":
            return HttpResponse(200, {"Content-Type": "text/plain"}, b"ok")
        if request.headers.get("Authorization") != "Bearer secret":
            return json_response(401, {"message": "unauthorized"})
        if path == "/pets" and request.method == "GET":
            limit = int(query.get("limit", ["2"])[0])
            start = int(query.get("cursor", ["0"])[0])
            ids = sorted(self.pets)[start : start + limit]
            following = start + limit
            return json_response(
                200,
                {
                    "items": [self.pets[i] for i in ids],
                    "next_cursor": str(following) if following < len(self.pets) else None,
                },
            )
        if path == "/pets" and request.method == "POST":
            body = json.loads(request.body or b"{}")
            if not body.get("name"):
                return json_response(422, {"message": "name required"})
            pet_id = max(self.pets) + 1
            self.pets[pet_id] = {"id": pet_id, "status": "available", **body}
            return json_response(201, self.pets[pet_id])
        if path.startswith("/pets/"):
            pet_id = int(path.rsplit("/", 1)[1])
            if pet_id not in self.pets:
                return json_response(404, {"message": "no such pet"})
            if request.method == "DELETE":
                del self.pets[pet_id]
                return HttpResponse(204, {}, b"")
            return json_response(200, self.pets[pet_id])
        return json_response(404, {"message": "not found"})


LIBRARY_DATA = {
    "authors": {"a1": {"id": "a1", "name": "Ursula"}},
    "books": {
        "1": {"id": "1", "title": "The Dispossessed", "genre": "FICTION", "authorId": "a1"},
        "2": {"id": "2", "title": "Earthsea", "genre": "FICTION", "authorId": "a1"},
    },
}


class Library:
    """In-memory GraphQL server for the library example, executed by graphql-core."""

    def __init__(self) -> None:
        self.schema = build_schema((EXAMPLES / "library" / "schema.graphql").read_text("utf-8"))
        authors = LIBRARY_DATA["authors"]
        books = LIBRARY_DATA["books"]

        def book(raw: dict[str, Any]) -> dict[str, Any]:
            return {**raw, "__typename": "Book", "author": author(authors[raw["authorId"]])}

        def author(raw: dict[str, Any]) -> dict[str, Any]:
            return {
                **raw,
                "__typename": "Author",
                "books": lambda *_: [book(b) for b in books.values() if b["authorId"] == raw["id"]],
            }

        self.root = {
            "book": lambda _info, id: book(books[id]) if id in books else None,
            "books": lambda _info, genre=None, first=10: [
                book(b) for b in books.values() if genre in (None, b["genre"])
            ][:first],
            "search": lambda _info, text: (
                [book(b) for b in books.values()] + [author(a) for a in authors.values()]
            ),
            "node": lambda _info, id: book(books[id]) if id in books else None,
            "addBook": lambda _info, input: book({"id": "3", **input}),
        }

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if request.headers.get("Authorization") != "Bearer secret":
            return json_response(401, {"errors": [{"message": "unauthorized"}]})
        payload = json.loads(request.body or b"{}")
        result = graphql_sync(
            self.schema,
            payload["query"],
            root_value=self.root,
            variable_values=payload.get("variables"),
            operation_name=payload.get("operationName"),
            type_resolver=lambda value, *_: value["__typename"],
        )
        body: dict[str, Any] = {"data": result.data}
        if result.errors:
            body["errors"] = [{"message": e.message} for e in result.errors]
        return json_response(200, body)


@contextmanager
def serve(handler: Handler) -> Generator[str]:
    """Serve a handler over real HTTP on loopback; yield the base URL."""

    class RequestHandler(BaseHTTPRequestHandler):
        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None
            request = HttpRequest(
                self.command,
                f"http://127.0.0.1{self.path}",
                {k: v for k, v in self.headers.items()},
                body,
            )
            response = handler(request)
            self.send_response(response.status)
            for key, value in response.headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(response.body)))
            self.end_headers()
            self.wfile.write(response.body)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _dispatch

        @override
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), RequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
