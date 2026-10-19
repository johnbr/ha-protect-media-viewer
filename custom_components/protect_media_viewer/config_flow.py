"""Config flow for Protect Media Viewer."""

from __future__ import annotations

import logging
from collections.abc import Mapping
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

    async def _async_connect(self, data: Mapping[str, Any]) -> tuple[dict[str, str], str | None, str | None]:
        """Log in to the NVR described by ``data``.

        Returns ``(errors, nvr_id, nvr_name)``; the id and name are ``None``
        when the login failed.
        """
        session = await async_create_session(self.hass, data[CONF_VERIFY_SSL])
        client = ProtectClient(
            host=data[CONF_HOST],
            port=data[CONF_PORT],
            username=data[CONF_USERNAME],
            password=data[CONF_PASSWORD],
            verify_ssl=data[CONF_VERIFY_SSL],
            session=session,
        )
        try:
            await client.connect()
        except ProtectAuthError:
            return {"base": "invalid_auth"}, None, None
        except ProtectConnectionError:
            return {"base": "cannot_connect"}, None, None
        except Exception:
            _LOGGER.exception("Unexpected error connecting to UniFi Protect")
            return {"base": "unknown"}, None, None
        else:
            return {}, client.nvr_id(), client.nvr_name()
        finally:
            await client.close()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            errors, nvr_id, title = await self._async_connect(user_input)
            if not errors:
                await self.async_set_unique_id(nvr_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=title, data=user_input)

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

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Setup raised ConfigEntryAuthFailed: the NVR refused the saved login."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Take a new username and password for the same NVR.

        Host, port and the saved-clip options are kept, so a password change
        no longer means deleting the integration and losing its settings.
        """
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            errors, nvr_id, _name = await self._async_connect({**entry.data, **user_input})
            if not errors:
                # The login must open the NVR this entry belongs to, not
                # another console at the same address.
                await self.async_set_unique_id(nvr_id)
                self._abort_if_unique_id_mismatch(reason="wrong_nvr")
                return self.async_update_reload_and_abort(entry, data_updates=user_input)

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_USERNAME,
                    default=(user_input or {}).get(CONF_USERNAME, entry.data[CONF_USERNAME]),
                ): str,
                vol.Required(CONF_PASSWORD): str,
            }
        )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=schema,
            errors=errors,
            description_placeholders={"host": entry.data[CONF_HOST]},
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

