"""Switch platform for Geely Galaxy."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Geely Galaxy switches based on a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    api = hass.data[DOMAIN][entry.entry_id]["api"]

    entities: list[SwitchEntity] = [
        GeelySentryModeSwitch(coordinator, entry, api),
        GeelySunroofSwitch(coordinator, entry, api),
        GeelySunshadeSwitch(coordinator, entry, api),
    ]

    async_add_entities(entities)


# 说明：后备箱（尾门）开关已移除。
# 此前错误地复用 /vc/app/v1/vehicle/control/door 接口 + position=1，
# 实测该参数会解锁主驾车门而非后备箱，会造成误操作。
# 上游青龙脚本 jlyh.js 中也没有尾门控制实现，说明银河 APP 走的是另一条接口
# （目前未公开/需抓包）。后备箱当前可通过状态字段
# vehicleDoorCoverStatus.trunkLockStatus 查看锁状态，暂不提供控制按钮，
# 待抓到正确接口后再补回。


class GeelySentryModeSwitch(CoordinatorEntity, SwitchEntity):
    """Switch for sentry mode (哨兵模式)."""

    _attr_has_entity_name = True
    _attr_name = "哨兵模式开关"
    _attr_icon = "mdi:shield-car"

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        entry: ConfigEntry,
        api,
    ) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self._entry = entry
        self._api = api
        self._attr_unique_id = f"{entry.entry_id}_sentry_mode_switch"

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device info."""
        vehicle_info = self.coordinator.data.get("vehicle_info", {}) if self.coordinator.data else {}
        vin = vehicle_info.get("vin", "unknown")
        model = vehicle_info.get("seriesNameVs", "吉利银河")

        return {
            "identifiers": {(DOMAIN, vin)},
            "name": f"{model}",
            "manufacturer": "吉利汽车",
            "model": model,
        }

    @property
    def is_on(self) -> bool | None:
        """Return true if sentry mode is on."""
        if not self.coordinator.data:
            return None
        switch_status = self.coordinator.data.get("switch_status", {})
        status = switch_status.get("vstdModeStatus")
        if status == "1":
            return True
        elif status == "0":
            return False
        return None

    @property
    def icon(self) -> str:
        """Return icon based on state."""
        if self.is_on:
            return "mdi:shield-car"
        return "mdi:shield-off"

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on sentry mode."""
        try:
            await self._api.control_switch(None, "vstdMode", True)
            await self.coordinator.async_request_refresh()
        except Exception as err:
            _LOGGER.error("开启哨兵模式失败: %s", err)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off sentry mode."""
        try:
            await self._api.control_switch(None, "vstdMode", False)
            await self.coordinator.async_request_refresh()
        except Exception as err:
            _LOGGER.error("关闭哨兵模式失败: %s", err)


class GeelySunroofSwitch(CoordinatorEntity, SwitchEntity):
    """Switch for sunroof (天窗).

    与车窗共用同一个控制接口 /vc/app/v1/vehicle/control/window：
      - 开天窗：position=[2], type=1
      - 关天窗：position=[2], type=2
    状态来自 vehicleDoorCoverStatus.sunroofStatus（'1'=打开, '2'=关闭）。
    """

    _attr_has_entity_name = True
    _attr_name = "天窗"
    _attr_icon = "mdi:car-windshield"

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        entry: ConfigEntry,
        api,
    ) -> None:
        """Initialize the sunroof switch."""
        super().__init__(coordinator)
        self._entry = entry
        self._api = api
        self._attr_unique_id = f"{entry.entry_id}_sunroof_switch"

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device info."""
        vehicle_info = self.coordinator.data.get("vehicle_info", {}) if self.coordinator.data else {}
        vin = vehicle_info.get("vin", "unknown")
        model = vehicle_info.get("seriesNameVs", "吉利银河")

        return {
            "identifiers": {(DOMAIN, vin)},
            "name": f"{model}",
            "manufacturer": "吉利汽车",
            "model": model,
        }

    @property
    def is_on(self) -> bool | None:
        """Return true if the sunroof is open."""
        if not self.coordinator.data:
            return None
        vehicle_status = self.coordinator.data.get("vehicle_status", {})
        door_cover = vehicle_status.get("vehicleDoorCoverStatus", {})
        status = door_cover.get("sunroofStatus")
        if status == "1":
            return True
        elif status == "2":
            return False
        return None

    @property
    def icon(self) -> str:
        """Return icon based on state."""
        if self.is_on:
            return "mdi:car-windshield-outline"
        return "mdi:car-windshield"

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Open the sunroof (position=[2], type=1)."""
        try:
            await self._api.control_window(None, action="sunroofOpen")
            await self.coordinator.async_request_refresh()
        except Exception as err:
            _LOGGER.error("开启天窗失败: %s", err)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Close the sunroof (position=[2], type=2)."""
        try:
            await self._api.control_window(None, action="sunroofClose")
            await self.coordinator.async_request_refresh()
        except Exception as err:
            _LOGGER.error("关闭天窗失败: %s", err)

# 注意：后备箱 GeelyTrunkSwitch 已移除，见 async_setup_entry 上方说明。


class GeelySunshadeSwitch(CoordinatorEntity, SwitchEntity):
    """Switch for sunshade/curtain (遮阳帘).

    与车窗共用同一个控制接口 /vc/app/v1/vehicle/control/window：
      - 开遮阳帘：position=[3], type=1
      - 关遮阳帘：position=[3], type=2
    状态字段优先读 sunshadeStatus，兼容 skylightCurtainStatus / sunCurtainStatus。
      '1'=打开, '2'=关闭。
    """

    _attr_has_entity_name = True
    _attr_name = "遮阳帘"
    _attr_icon = "mdi:blinds"

    _SUNSHADE_STATUS_KEYS = ("sunshadeStatus", "skylightCurtainStatus", "sunCurtainStatus")

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        entry: ConfigEntry,
        api,
    ) -> None:
        """Initialize the sunshade switch."""
        super().__init__(coordinator)
        self._entry = entry
        self._api = api
        self._attr_unique_id = f"{entry.entry_id}_sunshade_switch"

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device info."""
        vehicle_info = self.coordinator.data.get("vehicle_info", {}) if self.coordinator.data else {}
        vin = vehicle_info.get("vin", "unknown")
        model = vehicle_info.get("seriesNameVs", "吉利银河")

        return {
            "identifiers": {(DOMAIN, vin)},
            "name": f"{model}",
            "manufacturer": "吉利汽车",
            "model": model,
        }

    def _raw_status(self) -> str | None:
        if not self.coordinator.data:
            return None
        vehicle_status = self.coordinator.data.get("vehicle_status", {})
        door_cover = vehicle_status.get("vehicleDoorCoverStatus", {})
        if not isinstance(door_cover, dict):
            return None
        for key in self._SUNSHADE_STATUS_KEYS:
            val = door_cover.get(key)
            if val is not None:
                return str(val)
        return None

    @property
    def is_on(self) -> bool | None:
        """Return true if sunshade is open."""
        status = self._raw_status()
        if status == "1":
            return True
        if status == "2":
            return False
        return None

    @property
    def icon(self) -> str:
        if self.is_on:
            return "mdi:blinds-open"
        return "mdi:blinds"

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Open sunshade (position=[3], type=1)."""
        try:
            await self._api.control_window(None, action="sunshadeOpen")
            await self.coordinator.async_request_refresh()
        except Exception as err:
            _LOGGER.error("开启遮阳帘失败: %s", err)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Close sunshade (position=[3], type=2)."""
        try:
            await self._api.control_window(None, action="sunshadeClose")
            await self.coordinator.async_request_refresh()
        except Exception as err:
            _LOGGER.error("关闭遮阳帘失败: %s", err)
