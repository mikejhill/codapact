"""Example auth plugin: HMAC request signing for AuthLab's custom ``signed`` scheme.

A generated CLI loads it from ``AUTHLAB_AUTH_PLUGIN`` (or ``cli.auth_plugin`` in
codapact.yaml): ``authlab_plugin:provider``. Libraries pass the signer directly:
``Client(auth=StaticAuth({"signed": HmacSigner("key-1", secret)}))``.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from urllib.parse import urlsplit

from codapact.model import AuthScheme
from codapact.runtime import AuthContext, HttpRequest


@dataclass(frozen=True)
class HmacSigner:
    """Sign method, path, and body; runs on the final request, so retries re-sign."""

    key_id: str
    secret: str

    def authenticate(self, request: HttpRequest, scheme: AuthScheme) -> HttpRequest:
        path = urlsplit(request.url).path
        message = request.method.upper().encode() + b"\n" + path.encode() + b"\n"
        message += request.body or b""
        digest = hmac.new(self.secret.encode(), message, hashlib.sha256).hexdigest()
        headers = {**request.headers, "X-Key-Id": self.key_id, "X-Signature": digest}
        return HttpRequest(request.method, request.url, headers, request.body)


def provider(context: AuthContext) -> dict[str, HmacSigner]:
    """Return a signer when the signing key is configured, otherwise nothing."""
    key_id = context.environ.get("AUTHLAB_SIGNING_KEY_ID")
    secret = context.environ.get("AUTHLAB_SIGNING_SECRET")
    if not key_id or not secret:
        return {}
    return {"signed": HmacSigner(key_id, secret)}
