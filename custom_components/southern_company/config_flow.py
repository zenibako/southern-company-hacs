"""Config flow for Southern Company integration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
import yaml
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD
from homeassistant.const import CONF_USERNAME
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import aiohttp_client
from southern_company_api.parser import CantReachSouthernCompany
from southern_company_api.parser import InvalidLogin
from southern_company_api.parser import SouthernCompanyAPI

from .const import CONF_TARIFFS
from .const import DEFAULT_TARIFF_NAME
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


def _validate_tariffs(raw: str) -> tuple[list[dict] | None, str | None]:
    """Parse and validate the YAML tariff schedule.

    Returns ``(tariffs, error_key)``. On success, ``error_key`` is ``None``.
    Empty/whitespace input is treated as no tariffs configured.
    """
    if not raw or not raw.strip():
        return [], None
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError:
        return None, "invalid_yaml"
    if parsed is None:
        return [], None
    if not isinstance(parsed, list):
        return None, "invalid_schema"
    seen_names: set[str] = set()
    for entry in parsed:
        if not isinstance(entry, dict):
            return None, "invalid_schema"
        name = entry.get("name")
        days = entry.get("days")
        start_hour = entry.get("start_hour")
        end_hour = entry.get("end_hour")
        if (
            not isinstance(name, str)
            or not name
            or name == DEFAULT_TARIFF_NAME
            or not isinstance(days, list)
            or not all(isinstance(d, int) and 0 <= d <= 6 for d in days)
            or not isinstance(start_hour, int)
            or not isinstance(end_hour, int)
        ):
            return None, "invalid_schema"
        if not (0 <= start_hour < end_hour <= 24):
            return None, "bad_hours"
        if name in seen_names:
            return None, "duplicate_name"
        seen_names.add(name)
    return parsed, None


def _tariffs_to_yaml(tariffs: list[dict]) -> str:
    """Serialize stored tariffs back to a YAML string for the options form."""
    if not tariffs:
        return ""
    return yaml.safe_dump(tariffs, sort_keys=False).strip()


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Southern Company."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlowHandler:
        """Return the options flow handler."""
        return OptionsFlowHandler(config_entry)

    async def async_authenticate(
        self, user_input: Mapping[str, Any], errors: dict[str, str]
    ) -> FlowResult | None:
        """Handle authentication for all flows to reduce repetition of code."""
        sca = SouthernCompanyAPI(
            user_input["username"],
            user_input["password"],
            aiohttp_client.async_get_clientsession(self.hass),
        )
        try:
            await sca.authenticate()
        except CantReachSouthernCompany:
            errors["base"] = "cannot_connect"
        except InvalidLogin:
            errors["base"] = "invalid_auth"
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Unexpected exception")
            errors["base"] = "unknown"
        else:
            return self.async_create_entry(
                title="Southern Company Hacs", data=user_input
            )
        return None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        _LOGGER.debug("Added user step")
        errors: dict[str, str] = {}
        if user_input is not None:
            self._async_abort_entries_match({CONF_USERNAME: user_input[CONF_USERNAME]})
            auth = await self.async_authenticate(user_input, errors)
            if auth is not None:
                _LOGGER.debug("FINISHED AUTH OF USER")
                return auth
        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> FlowResult:
        """Handle reauthentication."""
        _LOGGER.debug("Reauth?")
        return await self.async_step_reauth_confirm(entry_data)

    async def async_step_reauth_confirm(
        self, user_input: Mapping[str, Any] | None = None
    ) -> FlowResult:
        """Handle a flow initiated by reauthentication."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data_schema = {
                vol.Required(CONF_USERNAME, default=user_input[CONF_USERNAME]): str,
                vol.Required(CONF_PASSWORD): str,
            }
            auth = await self.async_authenticate(user_input, errors)
            if auth is not None:
                return auth
        else:
            data_schema = STEP_USER_DATA_SCHEMA
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=data_schema, errors=errors
        )


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle tariff configuration options."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize the options flow."""
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage tariff windows."""
        errors: dict[str, str] = {}
        current_yaml = _tariffs_to_yaml(
            self.config_entry.options.get(CONF_TARIFFS, [])
        )
        if user_input is not None:
            tariffs, error_key = _validate_tariffs(user_input.get(CONF_TARIFFS, ""))
            if error_key is not None:
                errors["base"] = error_key
                current_yaml = user_input.get(CONF_TARIFFS, "")
            else:
                return self.async_create_entry(
                    title="", data={CONF_TARIFFS: tariffs}
                )
        data_schema = vol.Schema(
            {vol.Optional(CONF_TARIFFS, default=current_yaml): str}
        )
        return self.async_show_form(
            step_id="init", data_schema=data_schema, errors=errors
        )
