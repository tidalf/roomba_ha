"""Select platform for Roomba room selection."""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .entity import IRobotEntity
from .models import RoombaData

_LOGGER = logging.getLogger(__name__)

OPTION_ALL_ROOMS = "Toutes les pièces"


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Roomba room select entity."""
    domain_data: RoombaData = hass.data[DOMAIN][config_entry.entry_id]

    if not domain_data.rooms:
        return

    select = RoombaRoomSelect(domain_data)
    domain_data.room_select = select
    async_add_entities([select])


class RoombaRoomSelect(IRobotEntity, SelectEntity):
    """Select entity for choosing which room to clean."""

    _attr_translation_key = "room"
    _attr_icon = "mdi:floor-plan"

    def __init__(self, domain_data: RoombaData) -> None:
        """Initialize the room select entity."""
        super().__init__(domain_data.roomba, domain_data.blid)
        self._attr_current_option = OPTION_ALL_ROOMS
        self._attr_options = [OPTION_ALL_ROOMS] + [
            room.get("name", room.get("id", ""))
            for room in domain_data.rooms
        ]

    @property
    def unique_id(self) -> str:
        """Return a unique ID."""
        return f"room_select_{self._blid}"

    async def async_select_option(self, option: str) -> None:
        """Handle room selection."""
        self._attr_current_option = option
        self.async_write_ha_state()
