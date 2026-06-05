"""Config flow for Protect Media Viewer."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .const import (
    CONF_VERIFY_SSL,
    DEFAULT_PORT,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
)
from .protect import ProtectAuthError, ProtectClient, ProtectConnectionError

_LOGGER = logging.getLogger(__name__)


class ProtectMediaViewerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the UI config flow: connect to the NVR and confirm."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            session = async_create_clientsession(
                self.hass, verify_ssl=user_input[CONF_VERIFY_SSL]
            )
            client = ProtectClient(
                host=user_input[CONF_HOST],
                port=user_input[CONF_PORT],
                username=user_input[CONF_USERNAME],
                password=user_input[CONF_PASSWORD],
                verify_ssl=user_input[CONF_VERIFY_SSL],
                session=session,
            )
            try:
                await client.connect()
            except ProtectAuthError:
                errors["base"] = "invalid_auth"
            except ProtectConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001 - surface as generic to the user
                _LOGGER.exception("Unexpected error connecting to UniFi Protect")
                errors["base"] = "unknown"
            else:
                nvr_id = client.nvr_id()
                title = client.nvr_name()
                await client.close()

                await self.async_set_unique_id(nvr_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=title, data=user_input)

            await client.close()

        schema = vol.Schema(
            {
                vol.Required(CONF_HOST): str,
                vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Required(CONF_VERIFY_SSL, default=DEFAULT_VERIFY_SSL): bool,
            }
        )
        return self.async_show_form(
            step_id="user", data_schema=schema, errors=errors
        )
