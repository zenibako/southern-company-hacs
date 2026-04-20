"""Config flow for Southern Company integration."""

from __future__ import annotations

from collections.abc import Mapping
import json
import logging
from typing import Any

from southern_company_api.parser import (
    CantReachSouthernCompany,
    InvalidLogin,
    SouthernCompanyAPI,
)
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers import aiohttp_client

from .const import CONF_TARIFFS
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Southern Company."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlowHandler:
        """Return the options flow handler."""
        return OptionsFlowHandler()

    async def async_authenticate(
        self, user_input: Mapping[str, Any], errors: dict[str, str]
    ) -> ConfigFlowResult | None:
        """Handle authentication for all flows. Returns entry on success, None on failure."""
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

        if errors:
            return None
        return self.async_create_entry(title="Southern Company Hacs", data=user_input)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
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

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle reauthentication."""
        _LOGGER.debug("Reauth?")
        return await self.async_step_reauth_confirm(entry_data)

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a flow initiated by reauthentication."""
        errors: dict[str, str] = {}
        if user_input is not None:
            auth = await self.async_authenticate(user_input, errors)
            if auth is not None:
                return auth
        data_schema = vol.Schema(
            {
                vol.Required(
                    CONF_USERNAME,
                    default=(user_input or {}).get(CONF_USERNAME, ""),
                ): str,
                vol.Required(CONF_PASSWORD): str,
            }
        )
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=data_schema, errors=errors
        )


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle tariff configuration options."""

    def __init__(self) -> None:
        """Initialize options flow."""
        self._tariffs: list[dict] = []

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the tariff list."""
        tariffs = list(self.config_entry.options.get(CONF_TARIFFS, []))
        if user_input is not None:
            action = user_input.get("action", "save")
            if action == "add":
                self._tariffs = tariffs
                return await self.async_step_tariff()
            if action == "remove" and tariffs:
                self._tariffs = tariffs
                return await self.async_step_remove()
            return self.async_create_entry(title="", data={CONF_TARIFFS: tariffs})
        schema = vol.Schema(
            {
                vol.Required("action", default="save"): vol.In(
                    {"save": "Save and finish", "add": "Add tariff", "remove": "Remove tariff"}
                )
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)

    async def async_step_tariff(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add a single tariff entry."""
        errors: dict[str, str] = {}
        if user_input is not None:
            name = user_input.get("name", "").strip()
            if not name:
                errors["name"] = "name_required"
            elif name == "default":
                errors["name"] = "name_reserved"
            else:
                start_hour: int = user_input["start_hour"]
                end_hour: int = user_input["end_hour"]
                if not (0 <= start_hour < end_hour <= 24):
                    errors["base"] = "bad_hours"
                else:
                    entry = {
                        "name": name,
                        "days": user_input["days"],
                        "start_hour": start_hour,
                        "end_hour": end_hour,
                    }
                    rate = user_input.get("rate")
                    if rate is not None:
                        entry["rate"] = rate
                    months = user_input.get("months")
                    if months:
                        entry["months"] = months
                    self._tariffs.append(entry)
                    self.hass.data.setdefault(DOMAIN, {})
                    return await self.async_step_init()
        schema = vol.Schema(
            {
                vol.Required("name"): str,
                vol.Required("days"): vol.All(
                    vol.Coerce(list), [vol.All(vol.Coerce(int), vol.Range(0, 6))]
                ),
                vol.Required("start_hour"): vol.All(vol.Coerce(int), vol.Range(0, 23)),
                vol.Required("end_hour"): vol.All(vol.Coerce(int), vol.Range(1, 24)),
                vol.Optional("rate"): vol.Coerce(float),
                vol.Optional("months"): vol.All(
                    vol.Coerce(list), [vol.All(vol.Coerce(int), vol.Range(1, 12))]
                ),
            }
        )
        return self.async_show_form(step_id="tariff", data_schema=schema, errors=errors)

    async def async_step_remove(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Remove a tariff entry."""
        if user_input is not None and user_input.get("remove"):
            names = user_input["remove"]
            self._tariffs = [t for t in self._tariffs if t["name"] in names]
            return await self.async_step_init()
        names = [t["name"] for t in self._tariffs]
        schema = vol.Schema(
            {vol.Required("remove"): vol.All(vol.Coerce(list), [vol.In(names)])}
        )
        return self.async_show_form(step_id="remove", data_schema=schema)