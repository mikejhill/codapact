import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import override
from urllib.parse import parse_qs, urlsplit

import pytest


@pytest.fixture
def contract():
    pet = {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "name": {"type": "string"}},
        "required": ["id", "name"],
        "additionalProperties": False,
    }
    return {
        "openapi": "3.0.3",
        "info": {"title": "Fixture pets", "version": "1"},
        "servers": [{"url": "https://example.com/v1"}],
        "components": {
            "schemas": {"Pet": pet},
            "securitySchemes": {
                "token": {"type": "http", "scheme": "bearer"},
                "key": {"type": "apiKey", "in": "header", "name": "X-API-Key"},
                "basic": {"type": "http", "scheme": "basic"},
            },
        },
        "security": [{"token": []}],
        "paths": {
            "/pets/{pet_id}": {
                "get": {
                    "operationId": "get_pet",
                    "parameters": [
                        {
                            "name": "pet_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "integer"},
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "pet",
                            "content": {
                                "application/json": {"schema": {"$ref": "#/components/schemas/Pet"}}
                            },
                        },
                        "404": {"description": "missing"},
                    },
                }
            },
            "/pets": {
                "get": {
                    "operationId": "list_pets",
                    "parameters": [
                        {"name": "page", "in": "query", "schema": {"type": "integer", "minimum": 1}}
                    ],
                    "responses": {
                        "200": {
                            "description": "pets",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "array",
                                        "items": {"$ref": "#/components/schemas/Pet"},
                                    }
                                }
                            },
                        }
                    },
                },
                "post": {
                    "operationId": "create_pet",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/Pet"}}
                        },
                    },
                    "responses": {
                        "201": {
                            "description": "created",
                            "content": {
                                "application/json": {"schema": {"$ref": "#/components/schemas/Pet"}}
                            },
                        }
                    },
                },
            },
            "/empty": {
                "delete": {
                    "operationId": "delete_pet",
                    "security": [],
                    "responses": {"204": {"description": "deleted"}},
                }
            },
        },
    }


@pytest.fixture
def server(contract):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        @override
        def log_message(self, format, *args):
            pass

        def do_GET(self):
            parts = urlsplit(self.path)
            requests.append((self.command, self.path, dict(self.headers)))
            if parts.path == "/contract":
                return self.respond(200, contract)
            if parts.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/contract")
                self.end_headers()
                return
            if parts.path == "/huge":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"x" * 2_000_001)
                return
            if self.headers.get("Authorization") != "Bearer fixture-secret":
                return self.respond(401, {"secret": "fixture-secret"})
            if parts.path == "/pets/404":
                return self.respond(404, {"secret": "fixture-secret"})
            if parts.path == "/pets/999":
                return self.respond(200, {"wrong": "fixture-secret"})
            if parts.path == "/pets":
                page = int(parse_qs(parts.query).get("page", [1])[0])
                return self.respond(200, [{"id": page, "name": "Miso"}])
            return self.respond(200, {"id": int(parts.path.rsplit("/", 1)[1]), "name": "Miso"})

        def do_POST(self):
            requests.append((self.command, self.path, dict(self.headers)))
            return self.respond(
                201, json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            )

        def do_DELETE(self):
            requests.append((self.command, self.path, dict(self.headers)))
            self.send_response(204)
            self.end_headers()

        def respond(self, status, body):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

    instance = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{instance.server_port}"
    contract["servers"] = [{"url": url}]
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        yield url, requests
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join()
