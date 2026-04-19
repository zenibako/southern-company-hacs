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
from southern_company_api.account import Account
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


async def _patched_get_service_point_number(self: Account, jwt_token: str) -> str:
    """Patched get_service_point_number: uses account's own company code and
    handles an empty meterAndServicePoints list gracefully."""
    import aiohttp

    headers = {
        "Authorization": f"bearer {jwt_token}",
        "content-type": "application/json, text/plain, */*",
    }
    company_code = self.company.name  # e.g. "GPC", "APC", "MPC"
    try:
        async with self.session.get(
            f"https://customerservice2api.southerncompany.com/api/MyPowerUsage/"
            f"getMPUBasicAccountInformation/{self.number}/{company_code}",
            headers=headers,
        ) as resp:
            try:
                service_info = await resp.json()
            except (ContentTypeError, json.JSONDecodeError) as err:
                try:
                    error_text = await resp.text()
                except aiohttp.ClientError:
                    error_text = str(err)
                raise CantReachSouthernCompany(
                    f"Incorrect mimetype while trying to get service point number. "
                    f"error:{error_text} Response headers:{resp.headers}"
                ) from err
            points = (service_info.get("Data") or {}).get("meterAndServicePoints") or []
            if points:
                self.service_point_number = points[0]["servicePointNumber"]
            else:
                # Log a snippet of the actual response so the upstream change
                # is debuggable. Avoid spamming the entire payload.
                preview = json.dumps(service_info)[:500]
                _LOGGER.warning(
                    "meterAndServicePoints empty for account %s (company %s); "
                    "monthly/hourly stats unavailable. Response preview: %s",
                    self.number,
                    company_code,
                    preview,
                )
                # yarl 1.x rejects None query params, so use empty string. The
                # coordinator skips accounts with a falsy service_point_number.
                self.service_point_number = ""
    except aiohttp.ClientConnectorError as err:
        raise CantReachSouthernCompany("Failed to connect to api") from err
    return self.service_point_number or ""


def apply() -> None:
    """Install the patch. Safe to call multiple times."""
    if getattr(SouthernCompanyAPI._get_sc_web_token, "_hacs_patched", False):
        return
    _patched_get_sc_web_token._hacs_patched = True  # type: ignore[attr-defined]
    SouthernCompanyAPI._get_sc_web_token = _patched_get_sc_web_token  # type: ignore[assignment]
    Account.get_service_point_number = _patched_get_service_point_number  # type: ignore[assignment]
    _LOGGER.debug("Applied ScWebToken parser patch and service point patch")
