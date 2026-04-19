"""Monkey-patch ``SouthernCompanyAPI._get_sc_web_token`` for the current login API.

The upstream ``southern-company-api`` library scrapes ``data.html`` with a
single-quoted regex (``NAME='ScWebToken' value='...'``). Southern Company's
login API now either returns the token directly in ``data.token`` or embeds
it in HTML with double-quoted attributes, so the original regex no longer
matches and every login fails with ``NoScTokenFound``.

This patch tries, in order:
1. ``connection["data"]["token"]`` if it looks like a JWT.
2. Case-insensitive, quote-agnostic regex against ``data.html``.
3. A generic JWT-shaped token anywhere in ``data.html``.
"""

from __future__ import annotations

import datetime
import json
import logging
import re

import jwt
from aiohttp import ContentTypeError
from southern_company_api.exceptions import (
    CantReachSouthernCompany,
    InvalidLogin,
    NoScTokenFound,
)
from southern_company_api.parser import SouthernCompanyAPI

_LOGGER = logging.getLogger(__name__)

_JWT_RE = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
_SC_ATTR_RE = re.compile(
    r"""name\s*=\s*['"]ScWebToken['"][^>]*?value\s*=\s*['"]([^'"]+)['"]""",
    re.IGNORECASE,
)


def _extract_sc_token(connection: dict) -> str | None:
    data = connection.get("data") or {}
    token = data.get("token")
    if isinstance(token, str) and _JWT_RE.fullmatch(token):
        return token
    html = data.get("html") or ""
    if isinstance(html, str) and html:
        attr_match = _SC_ATTR_RE.search(html)
        if attr_match:
            candidate = attr_match.group(1)
            if _JWT_RE.fullmatch(candidate):
                return candidate
        jwt_match = _JWT_RE.search(html)
        if jwt_match:
            return jwt_match.group(0)
    return None


async def _patched_get_sc_web_token(self: SouthernCompanyAPI) -> str:
    if await self.request_token is None:
        raise CantReachSouthernCompany("Request Token could not be refreshed")
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "RequestVerificationToken": self._request_token,
    }
    data = {
        "username": self.username,
        "password": self.password,
        "targetPage": 1,
        "params": {"ReturnUrl": "null"},
    }
    async with self.session.post(
        "https://webauth.southernco.com/api/login", json=data, headers=headers
    ) as response:
        if response.status != 200:
            raise CantReachSouthernCompany()
        try:
            connection = await response.json()
        except (ContentTypeError, json.JSONDecodeError) as err:
            raise InvalidLogin from err
        if connection.get("statusCode") == 500:
            raise InvalidLogin()

    token = _extract_sc_token(connection)
    if token is None:
        self._sc = None
        keys = sorted((connection.get("data") or {}).keys())
        raise NoScTokenFound(
            f"Login request did not return a sc token (data keys: {keys})"
        )
    self._sc = token
    sc_decoded = jwt.decode(self._sc, options={"verify_signature": False})
    self._sc_expiry = datetime.datetime.fromtimestamp(sc_decoded["exp"])
    return self._sc


def apply() -> None:
    """Install the patch. Safe to call multiple times."""
    if getattr(SouthernCompanyAPI._get_sc_web_token, "_hacs_patched", False):
        return
    _patched_get_sc_web_token._hacs_patched = True  # type: ignore[attr-defined]
    SouthernCompanyAPI._get_sc_web_token = _patched_get_sc_web_token  # type: ignore[assignment]
    _LOGGER.debug("Applied ScWebToken parser patch")
