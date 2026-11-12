"""Build ``AuthScheme`` values from the compact configuration syntax.

Shared by the ``auth`` shaping transform and the GraphQL source's ``auth``
option, so both accept exactly the same spellings:

```yaml
token: bearer                       # bearer | basic | oauth2 | custom
key: {api_key: X-Api-Key, in: header}   # in: header | query | cookie
sso:
  oauth2:
    client_credentials: {token_url: https://idp/token, scopes: [read]}
    device_code: {device_authorization_url: https://idp/device, token_url: https://idp/token}
    authorization_code: {authorization_url: https://idp/authorize, token_url: https://idp/token}
oidc: {openid: https://idp/.well-known/openid-configuration}
signed: {custom: HMAC request signing}
```
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from codapact.core.errors import ConfigError
from codapact.model.api import AuthKind, AuthScheme, OAuthFlow, OAuthFlowKind, ParamLocation

_FLOW_FIELDS = {"token_url", "authorization_url", "device_authorization_url", "refresh_url"}
SPELLINGS = (
    "bearer, basic, oauth2, custom, {api_key: NAME, in: header}, {oauth2: {FLOW: {...}}}, "
    "{openid: URL}, or {custom: DESCRIPTION}"
)


def scheme_from_config(name: str, spec: Any) -> AuthScheme:
    """Return the scheme ``spec`` describes or raise ``ConfigError``."""
    if spec in ("bearer", "basic", "oauth2", "custom"):
        return AuthScheme(name, AuthKind(spec))
    if not isinstance(spec, Mapping) or not spec:
        raise ConfigError(f"auth scheme {name!r} must be {SPELLINGS}")
    if "api_key" in spec:
        _only(name, spec, {"api_key", "in"})
        if not isinstance(spec["api_key"], str) or not spec["api_key"]:
            raise ConfigError(f"auth scheme {name!r}: api_key must name the header/param")
        where = spec.get("in", "header")
        if where not in ("header", "query", "cookie"):
            raise ConfigError(f"auth scheme {name!r}: 'in' must be header, query, or cookie")
        return AuthScheme(name, AuthKind.API_KEY, ParamLocation(where), wire_name=spec["api_key"])
    if "oauth2" in spec:
        _only(name, spec, {"oauth2", "docs"})
        flows = spec["oauth2"]
        if not isinstance(flows, Mapping) or not flows:
            raise ConfigError(f"auth scheme {name!r}: oauth2 must map flow names to settings")
        return AuthScheme(
            name,
            AuthKind.OAUTH2,
            flows=tuple(_flow(name, str(k), v) for k, v in flows.items()),
            docs=str(spec.get("docs", "")),
        )
    if "openid" in spec:
        _only(name, spec, {"openid", "docs"})
        if not isinstance(spec["openid"], str) or not spec["openid"]:
            raise ConfigError(f"auth scheme {name!r}: openid must be a discovery URL")
        return AuthScheme(
            name, AuthKind.OAUTH2, discovery_url=spec["openid"], docs=str(spec.get("docs", ""))
        )
    if "custom" in spec:
        _only(name, spec, {"custom"})
        return AuthScheme(name, AuthKind.CUSTOM, docs=str(spec["custom"] or ""))
    raise ConfigError(f"auth scheme {name!r} must be {SPELLINGS}")


def _flow(scheme: str, key: str, raw: Any) -> OAuthFlow:
    try:
        kind = OAuthFlowKind(key)
    except ValueError:
        choices = ", ".join(k.value for k in OAuthFlowKind)
        raise ConfigError(
            f"auth scheme {scheme!r}: unknown oauth2 flow {key!r} ({choices})"
        ) from None
    if not isinstance(raw, Mapping):
        raise ConfigError(f"auth scheme {scheme!r}: oauth2.{key} must be a mapping")
    _only(f"{scheme}.oauth2.{key}", raw, _FLOW_FIELDS | {"scopes"})
    urls = {}
    for field in _FLOW_FIELDS:
        value = raw.get(field)
        if value is not None and (not isinstance(value, str) or not value):
            raise ConfigError(f"auth scheme {scheme!r}: oauth2.{key}.{field} must be a URL")
        urls[field] = value
    scopes = raw.get("scopes", [])
    if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
        raise ConfigError(f"auth scheme {scheme!r}: oauth2.{key}.scopes must be a list")
    return OAuthFlow(kind, scopes=tuple(scopes), **urls)


def _only(where: str, spec: Mapping[str, Any], allowed: set[str]) -> None:
    extra = set(spec) - allowed
    if extra:
        raise ConfigError(f"auth scheme {where!r}: unknown keys {sorted(extra)}")
