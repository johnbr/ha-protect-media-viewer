"""Config flow for Protect Media Viewer."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import callback

from .const import (
    CONF_FLAG_IN_PROTECT,
    CONF_REMOVE_ADMIN_ONLY,
    CONF_SAVE_MIN_FREE_GB,
    CONF_SAVE_POST_ROLL,
    CONF_SAVE_PRE_ROLL,
    CONF_SAVED_DIR,
    CONF_TRASH_DAYS,
    CONF_VERIFY_SSL,
    DEFAULT_FLAG_IN_PROTECT,
    DEFAULT_PORT,
    DEFAULT_REMOVE_ADMIN_ONLY,
    DEFAULT_SAVE_MIN_FREE_GB,
    DEFAULT_SAVE_POST_ROLL,
    DEFAULT_SAVE_PRE_ROLL,
    DEFAULT_SAVED_DIR,
    DEFAULT_TRASH_DAYS,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
    SAVED_MARKER,
)
from .protect import (
    ProtectAuthError,
    ProtectClient,
    ProtectConnectionError,
    async_create_session,
)
from .saved import prepare_saved_dir

_LOGGER = logging.getLogger(__name__)


class ProtectMediaViewerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the UI config flow: connect to the NVR and confirm."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return ProtectMediaViewerOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            session = await async_create_session(
                self.hass, user_input[CONF_VERIFY_SSL]
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
            except Exception:
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


class ProtectMediaViewerOptionsFlow(OptionsFlow):
    """Saved-clip settings: where clips go, padding, space floor, permissions."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        current = {**self.config_entry.options}

        if user_input is not None:
            saved_dir = (user_input.get(CONF_SAVED_DIR) or "").strip()
            if saved_dir:
                error = await self.hass.async_add_executor_job(prepare_saved_dir, saved_dir, SAVED_MARKER)
                if error:
                    errors[CONF_SAVED_DIR] = error
            if not errors:
                return self.async_create_entry(data={**user_input, CONF_SAVED_DIR: saved_dir})
            current.update(user_input)

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_SAVED_DIR,
                    description={"suggested_value": current.get(CONF_SAVED_DIR, DEFAULT_SAVED_DIR)},
                ): str,
                vol.Required(
                    CONF_SAVE_PRE_ROLL, default=current.get(CONF_SAVE_PRE_ROLL, DEFAULT_SAVE_PRE_ROLL)
                ): vol.All(vol.Coerce(int), vol.Range(min=0, max=600)),
                vol.Required(
                    CONF_SAVE_POST_ROLL, default=current.get(CONF_SAVE_POST_ROLL, DEFAULT_SAVE_POST_ROLL)
                ): vol.All(vol.Coerce(int), vol.Range(min=0, max=600)),
                vol.Required(
                    CONF_SAVE_MIN_FREE_GB, default=current.get(CONF_SAVE_MIN_FREE_GB, DEFAULT_SAVE_MIN_FREE_GB)
                ): vol.All(vol.Coerce(int), vol.Range(min=0, max=100000)),
                vol.Required(
                    CONF_TRASH_DAYS, default=current.get(CONF_TRASH_DAYS, DEFAULT_TRASH_DAYS)
                ): vol.All(vol.Coerce(int), vol.Range(min=0, max=3650)),
                vol.Required(
                    CONF_FLAG_IN_PROTECT, default=current.get(CONF_FLAG_IN_PROTECT, DEFAULT_FLAG_IN_PROTECT)
                ): bool,
                vol.Required(
                    CONF_REMOVE_ADMIN_ONLY, default=current.get(CONF_REMOVE_ADMIN_ONLY, DEFAULT_REMOVE_ADMIN_ONLY)
                ): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)

