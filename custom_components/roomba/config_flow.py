"""Config flow to configure roomba component."""

from __future__ import annotations

import asyncio
from functools import partial
import logging
from typing import Any

from irbt import Cloud as IrbtCloud
from roombapy import RoombaFactory, RoombaInfo
from roombapy.discovery import RoombaDiscovery
from roombapy.getpassword import RoombaPassword
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_DELAY, CONF_HOST, CONF_NAME, CONF_PASSWORD
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from . import CannotConnect, async_connect_or_timeout, async_disconnect_or_timeout
from .const import (
    CONF_BLID,
    CONF_CLOUD_EMAIL,
    CONF_CLOUD_PASSWORD,
    CONF_CONTINUOUS,
    CONF_ROBOT_ID,
    DEFAULT_CONTINUOUS,
    DEFAULT_DELAY,
    DOMAIN,
    ROOMBA_SESSION,
)

_LOGGER = logging.getLogger(__name__)

ROOMBA_DISCOVERY_LOCK = "roomba_discovery_lock"
ALL_ATTEMPTS = 2
HOST_ATTEMPTS = 6
ROOMBA_WAKE_TIME = 6

DEFAULT_OPTIONS = {CONF_CONTINUOUS: DEFAULT_CONTINUOUS, CONF_DELAY: DEFAULT_DELAY}

MAX_NUM_DEVICES_TO_DISCOVER = 25

AUTH_HELP_URL_KEY = "auth_help_url"
AUTH_HELP_URL_VALUE = (
    "https://www.home-assistant.io/integrations/roomba/#retrieving-your-credentials"
)

STEP_CLOUD_LOGIN = "cloud_login"
STEP_LINK = "link"
STEP_LINK_MANUAL = "link_manual"

SETUP_USER_OPTIONS = {
    "cloud_login": "Login with iRobot Cloud",
    "manual": "Manually add a Roomba or Braava",
}

SETUP_AUTH_OPTIONS = {
    "cloud_login": "Login with iRobot Cloud (recommended)",
    "link": "Press button on device",
    "link_manual": "Enter password manually",
}


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect."""
    roomba = await hass.async_add_executor_job(
        partial(
            RoombaFactory.create_roomba,
            address=data[CONF_HOST],
            blid=data[CONF_BLID],
            password=data[CONF_PASSWORD],
            continuous=True,
            delay=data[CONF_DELAY],
        )
    )

    info = await async_connect_or_timeout(hass, roomba)
    if info:
        await async_disconnect_or_timeout(hass, roomba)

    return {
        ROOMBA_SESSION: info[ROOMBA_SESSION],
        CONF_NAME: info[CONF_NAME],
        CONF_HOST: data[CONF_HOST],
    }


class RoombaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Roomba configuration flow."""

    VERSION = 1

    name: str | None = None
    blid: str | None = None
    host: str | None = None

    def __init__(self) -> None:
        """Initialize the roomba flow."""
        self.discovered_robots: dict[str, RoombaInfo] = {}
        self._cloud: IrbtCloud | None = None
        self._cloud_robots: dict[str, Any] = {}
        self._cloud_email: str | None = None
        self._cloud_password: str | None = None
        self._cloud_robot_password: str | None = None
        self._cloud_robot_id: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> RoombaOptionsFlowHandler:
        """Get the options flow for this handler."""
        return RoombaOptionsFlowHandler()

    # ── Discovery entry points ──────────────────────────────────────────

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Handle zeroconf discovery."""
        return await self._async_step_discovery(
            discovery_info.host, discovery_info.hostname.lower().removesuffix(".local.")
        )

    async def async_step_dhcp(
        self, discovery_info: DhcpServiceInfo
    ) -> ConfigFlowResult:
        """Handle dhcp discovery."""
        return await self._async_step_discovery(
            discovery_info.ip, discovery_info.hostname
        )

    async def _async_step_discovery(
        self, ip_address: str, hostname: str
    ) -> ConfigFlowResult:
        """Handle any discovery."""
        self._async_abort_entries_match({CONF_HOST: ip_address})

        if not hostname.startswith(("irobot-", "roomba-")):
            return self.async_abort(reason="not_irobot_device")

        self.host = ip_address
        self.blid = _async_blid_from_hostname(hostname)
        await self.async_set_unique_id(self.blid)
        self._abort_if_unique_id_configured(updates={CONF_HOST: ip_address})

        for progress in self._async_in_progress():
            flow_unique_id = progress["context"].get("unique_id")
            if not flow_unique_id:
                continue
            if flow_unique_id.startswith(self.blid):
                return self.async_abort(reason="short_blid")
            if self.blid.startswith(flow_unique_id):
                self.hass.config_entries.flow.async_abort(progress["flow_id"])

        self.context["title_placeholders"] = {"host": self.host, "name": self.blid}
        return await self.async_step_link_or_cloud()

    # ── User step (manual add integration) ──────────────────────────────

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle manual add integration (no discovery)."""
        if user_input is not None:
            method = user_input["method"]
            if method == "cloud_login":
                return await self.async_step_cloud_login()
            return await self.async_step_manual()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required("method"): vol.In(SETUP_USER_OPTIONS)}
            ),
        )

    async def _async_back_to_choice(self) -> ConfigFlowResult:
        """Go back to the auth choice step."""
        if self.host:
            return await self.async_step_link_or_cloud()
        return await self.async_step_user()

    # ── Discovery path: choose auth method ──────────────────────────────

    async def async_step_link_or_cloud(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let user choose: cloud login, button press, or manual password."""
        if user_input is not None:
            method = user_input["method"]
            if method == "cloud_login":
                return await self.async_step_cloud_login()
            if method == "link":
                return await self.async_step_link()
            return await self.async_step_link_manual()

        return self.async_show_form(
            step_id="link_or_cloud",
            data_schema=vol.Schema(
                {vol.Required("method"): vol.In(SETUP_AUTH_OPTIONS)}
            ),
            description_placeholders={CONF_NAME: self.name or self.blid or ""},
        )

    # ── Manual host entry ───────────────────────────────────────────────

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle manual device setup."""
        if user_input is None:
            return self.async_show_form(
                step_id="manual",
                description_placeholders={AUTH_HELP_URL_KEY: AUTH_HELP_URL_VALUE},
                data_schema=vol.Schema(
                    {vol.Required(CONF_HOST, default=self.host): str}
                ),
            )

        self._async_abort_entries_match({CONF_HOST: user_input["host"]})
        self.host = user_input[CONF_HOST]

        devices = await _async_discover_roombas(self.hass, self.host)
        if not devices:
            return self.async_abort(reason="cannot_connect")
        self.blid = devices[0].blid
        self.name = devices[0].robot_name

        await self.async_set_unique_id(self.blid, raise_on_progress=False)
        self._abort_if_unique_id_configured()
        return await self.async_step_link_or_cloud()

    # ── Button press password flow ──────────────────────────────────────

    async def async_step_link(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Press button on device to get password."""
        if user_input is None:
            return self.async_show_form(
                step_id="link",
                description_placeholders={CONF_NAME: self.name or self.blid or ""},
            )
        assert self.host

        def _get_password(host: str) -> str | None:
            try:
                return RoombaPassword(host).get_password()
            except OSError:
                return None

        password = await self.hass.async_add_executor_job(_get_password, self.host)

        if not password:
            return await self.async_step_link_or_cloud()

        config = {
            CONF_HOST: self.host,
            CONF_BLID: self.blid,
            CONF_PASSWORD: password,
            **DEFAULT_OPTIONS,
        }

        if not self.name:
            try:
                info = await validate_input(self.hass, config)
            except CannotConnect:
                return self.async_abort(reason="cannot_connect")
            self.name = info[CONF_NAME]

        assert self.name
        return self.async_create_entry(title=self.name, data=config)

    async def async_step_link_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle manual password entry."""
        errors = {}

        if user_input is not None:
            config = {
                CONF_HOST: self.host,
                CONF_BLID: self.blid,
                CONF_PASSWORD: user_input[CONF_PASSWORD],
                **DEFAULT_OPTIONS,
            }
            try:
                info = await validate_input(self.hass, config)
            except CannotConnect:
                return await self._async_back_to_choice()
            else:
                return self.async_create_entry(title=info[CONF_NAME], data=config)

        return self.async_show_form(
            step_id="link_manual",
            description_placeholders={AUTH_HELP_URL_KEY: AUTH_HELP_URL_VALUE},
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
        )

    # ── Cloud login flow ────────────────────────────────────────────────

    async def async_step_cloud_login(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle iRobot cloud login."""
        errors = {}

        if user_input is not None:
            email = user_input[CONF_CLOUD_EMAIL]
            password = user_input[CONF_CLOUD_PASSWORD]
            try:
                cloud = await self.hass.async_add_executor_job(
                    IrbtCloud, email, password
                )
                robots = await self.hass.async_add_executor_job(cloud.robots)
            except Exception:
                _LOGGER.exception("Failed to login to iRobot cloud")
                return await self._async_back_to_choice()
            else:
                if not robots:
                    return await self._async_back_to_choice()

                self._cloud = cloud
                self._cloud_robots = robots
                self._cloud_email = email
                self._cloud_password = password

                # If blid is already known (from discovery), find password directly
                if self.blid:
                    return await self._async_cloud_finish_with_blid()

                # Otherwise, let user pick a robot
                return await self.async_step_cloud_robots()

        return self.async_show_form(
            step_id="cloud_login",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_CLOUD_EMAIL): str,
                    vol.Required(CONF_CLOUD_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    async def _async_cloud_finish_with_blid(self) -> ConfigFlowResult:
        """Finish cloud setup when blid is already known (from discovery)."""
        assert self.blid
        assert self.host

        # Find this robot in the cloud data
        robot_password = None
        for rid, rinfo in self._cloud_robots.items():
            if rid.upper() == self.blid.upper():
                robot_password = rinfo.get("password")
                self._cloud_robot_id = rid
                break

        if not robot_password:
            _LOGGER.error(
                "Robot %s not found in cloud account. Available: %s",
                self.blid,
                list(self._cloud_robots.keys()),
            )
            return self.async_abort(reason="robot_not_in_cloud")

        config = {
            CONF_HOST: self.host,
            CONF_BLID: self.blid,
            CONF_PASSWORD: robot_password,
            CONF_CLOUD_EMAIL: self._cloud_email,
            CONF_CLOUD_PASSWORD: self._cloud_password,
            CONF_ROBOT_ID: self._cloud_robot_id,
            **DEFAULT_OPTIONS,
        }

        try:
            info = await validate_input(self.hass, config)
        except CannotConnect:
            return self.async_abort(reason="cannot_connect")

        self.name = info[CONF_NAME]
        return self.async_create_entry(title=self.name, data=config)

    async def async_step_cloud_robots(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let user pick a robot (when blid is not known)."""
        errors = {}

        if user_input is not None:
            robot_id = user_input[CONF_ROBOT_ID]
            robot_info = self._cloud_robots[robot_id]
            self._cloud_robot_password = robot_info.get("password")
            self._cloud_robot_id = robot_id
            self.blid = robot_id

            await self.async_set_unique_id(self.blid, raise_on_progress=False)
            self._abort_if_unique_id_configured()

            return await self.async_step_cloud_host()

        # Build robot selection dropdown
        already_configured = self._async_current_ids(False)
        robot_options = {}
        for rid, rinfo in self._cloud_robots.items():
            if f"roomba_{rid}" not in already_configured:
                robot_name = rinfo.get("name", rid)
                robot_options[rid] = f"{robot_name} ({rid})"

        if not robot_options:
            return self.async_abort(reason="already_configured")

        return self.async_show_form(
            step_id="cloud_robots",
            data_schema=vol.Schema(
                {vol.Required(CONF_ROBOT_ID): vol.In(robot_options)}
            ),
            errors=errors,
        )

    async def async_step_cloud_host(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask user for robot IP address."""
        errors = {}

        if user_input is not None:
            self.host = user_input[CONF_HOST]
            config = {
                CONF_HOST: self.host,
                CONF_BLID: self._cloud_robot_id,
                CONF_PASSWORD: self._cloud_robot_password,
                CONF_CLOUD_EMAIL: self._cloud_email,
                CONF_CLOUD_PASSWORD: self._cloud_password,
                CONF_ROBOT_ID: self._cloud_robot_id,
                **DEFAULT_OPTIONS,
            }
            try:
                info = await validate_input(self.hass, config)
            except CannotConnect:
                errors = {"base": "cannot_connect"}
            else:
                self.name = info[CONF_NAME]
                return self.async_create_entry(title=self.name, data=config)

        return self.async_show_form(
            step_id="cloud_host",
            data_schema=vol.Schema({vol.Required(CONF_HOST): str}),
            errors=errors,
        )


class RoombaOptionsFlowHandler(OptionsFlow):
    """Handle options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_CONTINUOUS,
                        default=options.get(CONF_CONTINUOUS, DEFAULT_CONTINUOUS),
                    ): bool,
                    vol.Optional(
                        CONF_DELAY,
                        default=options.get(CONF_DELAY, DEFAULT_DELAY),
                    ): int,
                }
            ),
        )


@callback
def _async_get_roomba_discovery() -> RoombaDiscovery:
    """Create a discovery object."""
    discovery = RoombaDiscovery()
    discovery.amount_of_broadcasted_messages = MAX_NUM_DEVICES_TO_DISCOVER
    return discovery


@callback
def _async_blid_from_hostname(hostname: str) -> str:
    """Extract the blid from the hostname."""
    return hostname.split("-")[1].split(".")[0].upper()


async def _async_discover_roombas(
    hass: HomeAssistant, host: str | None = None
) -> list[RoombaInfo]:
    """Discover roombas on the network."""
    discovered_hosts: set[str] = set()
    devices: list[RoombaInfo] = []
    discover_lock = hass.data.setdefault(ROOMBA_DISCOVERY_LOCK, asyncio.Lock())
    discover_attempts = HOST_ATTEMPTS if host else ALL_ATTEMPTS

    for attempt in range(discover_attempts + 1):
        async with discover_lock:
            discovery = _async_get_roomba_discovery()
            discovered: set[RoombaInfo] = set()
            try:
                if host:
                    device = await hass.async_add_executor_job(discovery.get, host)
                    if device:
                        discovered.add(device)
                else:
                    discovered = await hass.async_add_executor_job(discovery.get_all)
            except OSError:
                await asyncio.sleep(ROOMBA_WAKE_TIME * attempt)
                continue
            else:
                for device in discovered:
                    if device.ip in discovered_hosts:
                        continue
                    discovered_hosts.add(device.ip)
                    devices.append(device)
            finally:
                discovery.server_socket.close()

        if host and host in discovered_hosts:
            return devices

        await asyncio.sleep(ROOMBA_WAKE_TIME)

    return devices
