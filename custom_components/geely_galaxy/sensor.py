"""Sensor platform for Geely Galaxy."""
from __future__ import annotations

import logging
from collections import deque
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfLength,
    UnitOfTemperature,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfPressure,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 油量字段自动发现
#
# 不同车型的状态接口返回结构并不完全一致：
#   * VC 结构（/vc/app/v1/vehicle/control/status，A7/E5 等新车型）：
#       basicVehicleStatus / vehicleRunningStatus / vehicleBatteryStatus / ...
#   * XCHANGER 结构（/remote-control/vehicle/status/{vin}，L7 等）：
#       additionalVehicleStatus.runningStatus.fuelLevelPct / fuelLevel
#     集成会把 XCHANGER 数据转换后放进 _xchanger_extra。
#
# 这里不写死某一车型的字段路径，而是在整棵状态树里按
# 「精确字段名 -> 字段名包含关系」两级策略搜索，最大化兼容性。
# ---------------------------------------------------------------------------

# 油量百分比候选字段名（小写、精确匹配）
_FUEL_PCT_KEYS = (
    "fuellevelpct", "fuelpercent", "fuelpercentage", "fuelpct",
    "fuellevelpercentage", "oillevelpct", "oilpercent", "fueltanklevelpct",
)
# 油量容积/升候选字段名
_FUEL_VOLUME_KEYS = (
    "fuelvolume", "fuellevel", "fuelliters", "fuellitres", "fuelamount",
    "oillevel", "fueltanklevel", "fuelcapacity",
)
# 燃油续航候选字段名
_FUEL_RANGE_KEYS = (
    "distancetoempty", "fuelrange", "distancetoemptyonfuelonly",
    "fueldistancetoempty", "oilrange", "fuelremainingrange",
)
# 纯电续航候选字段名
_EV_RANGE_KEYS = (
    "distancetoemptyonbatteryonly", "electricrange", "evrange",
    "distancetoemptyonbattery",
)


def _walk_pairs(node: Any):
    """递归遍历嵌套 dict/list，产出所有 (key, value) 键值对。"""
    if isinstance(node, dict):
        for key, value in node.items():
            yield key, value
            if isinstance(value, (dict, list)):
                yield from _walk_pairs(value)
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, (dict, list)):
                yield from _walk_pairs(item)


def _iter_pairs_bfs(node: Any):
    """广度优先遍历，优先命中层级较浅的字段。"""
    queue = deque([node])
    while queue:
        current = queue.popleft()
        if isinstance(current, dict):
            for key, value in current.items():
                yield key, value
                if isinstance(value, (dict, list)):
                    queue.append(value)
        elif isinstance(current, list):
            for item in current:
                if isinstance(item, (dict, list)):
                    queue.append(item)


def _to_float(value: Any) -> float | None:
    """把候选值转成 float，无法转换时返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _find_by_keys(
    status: dict[str, Any], keys: tuple[str, ...]
) -> tuple[float | None, str | None]:
    """按字段名精确匹配（忽略大小写）查找第一个数值。"""
    for key, value in _iter_pairs_bfs(status):
        if isinstance(key, str) and key.lower() in keys:
            number = _to_float(value)
            if number is not None:
                return number, key
    return None, None


def _find_by_hint(
    status: dict[str, Any],
    required: tuple[str, ...],
    excluded: tuple[str, ...] = (),
    allowed: tuple[str, ...] = (),
) -> tuple[float | None, str | None]:
    """按字段名包含关系兜底查找数值（应对未收录的字段命名）。"""
    for key, value in _iter_pairs_bfs(status):
        if not isinstance(key, str):
            continue
        low = key.lower()
        if any(part in low for part in excluded):
            continue
        if not all(part in low for part in required):
            continue
        if allowed and not any(part in low for part in allowed):
            continue
        number = _to_float(value)
        if number is not None:
            return number, key
    return None, None


def _find_hinted_multi(
    status: dict[str, Any],
    roots: tuple[str, ...],
    excluded: tuple[str, ...] = (),
    allowed: tuple[str, ...] = (),
) -> tuple[float | None, str | None]:
    """在多个词根（fuel / oil 等）下依次按名称包含关系兜底查找。"""
    for root in roots:
        value, key = _find_by_hint(
            status, (root,), excluded=excluded, allowed=allowed
        )
        if value is not None:
            return value, key
    return None, None


def discover_fuel(status: dict[str, Any]) -> dict[str, Any]:
    """在车辆状态里自动发现油量数据。

    返回 dict:
        pct          油量百分比（0-100）
        volume       油量容积（升）
        range        燃油续航（km）
        pct_key      命中的百分比字段名
        volume_key   命中的容积字段名
        range_key    命中的续航字段名
        found        是否找到任一油量数据
        source       命中的字段名（便于排查）
    """
    result: dict[str, Any] = {
        "pct": None, "volume": None, "range": None,
        "pct_key": None, "volume_key": None, "range_key": None,
        "found": False, "source": None,
    }
    if not isinstance(status, dict) or not status:
        return result

    # 油量百分比
    pct, pct_key = _find_by_keys(status, _FUEL_PCT_KEYS)
    if pct is None:
        pct, pct_key = _find_hinted_multi(
            status, ("fuel", "oil"),
            excluded=("type", "capacity", "spec", "grade", "door"),
            allowed=("pct", "percent", "ratio"),
        )

    # 油量容积（升）
    volume, volume_key = _find_by_keys(status, _FUEL_VOLUME_KEYS)
    if volume is None:
        volume, volume_key = _find_hinted_multi(
            status, ("fuel", "oil"),
            excluded=("type", "pct", "percent", "range", "distance", "spec",
                      "door", "consumption", "price", "cost", "rate"),
            allowed=("level", "volume", "amount", "liter", "litre", "tank", "remaining"),
        )

    # 燃油续航
    range_km, range_key = _find_by_keys(status, _FUEL_RANGE_KEYS)
    if range_km is None:
        range_km, range_key = _find_hinted_multi(
            status, ("fuel", "oil"),
            excluded=("type", "pct", "percent", "level", "volume", "amount",
                      "tank", "capacity", "door"),
            allowed=("range", "distance", "empty"),
        )

    result["pct"] = pct
    result["volume"] = volume
    result["range"] = range_km
    result["pct_key"] = pct_key
    result["volume_key"] = volume_key
    result["range_key"] = range_key
    result["found"] = any(v is not None for v in (pct, volume, range_km))
    result["source"] = pct_key or volume_key or range_key
    return result


def discover_ev_range(status: dict[str, Any]) -> float | None:
    """查找纯电续航（优先精确字段，其次按名称包含）。"""
    value, _ = _find_by_keys(status, _EV_RANGE_KEYS)
    if value is not None:
        return value
    value, _ = _find_by_hint(status, ("battery", "range"))
    if value is None:
        value, _ = _find_by_hint(status, ("electric", "range"))
    return value


def _has_fuel_indicator(status: dict[str, Any], info: dict[str, Any]) -> bool:
    """判断车辆是否属于燃油/插混车型（决定是否注册油量传感器）。"""
    if isinstance(status, dict):
        for key, value in _walk_pairs(status):
            if not isinstance(key, str):
                continue
            low = key.lower()
            # fuelType 单独看取值，避免纯电车型被 configuration.fuelType 误判
            if low == "fueltype":
                text = str(value).lower()
                if any(t in text for t in ("electric", "ev", "纯电")):
                    continue
                if value not in (None, "", "0"):
                    return True
                continue
            if "fuel" in low:
                return True
            if low in ("enginestatus", "enginespeed", "engine_status") and value not in (None, "", "engine-off"):
                return True
    if isinstance(info, dict):
        for value in info.values():
            if not isinstance(value, str):
                continue
            text = value.lower()
            if any(t in text for t in ("phev", "em-i", "emi", "插混", "混动", "dm-i", "hybrid")):
                return True
    return False


def _log_fuel_diagnostics(status: dict[str, Any], fuel: dict[str, Any]) -> None:
    """输出一次油量字段诊断日志，便于定位不同车型的字段命名。"""
    if fuel["found"]:
        _LOGGER.warning(
            "[油量诊断] 已识别油量字段: 百分比=%s (%s), 容积=%s (%s), 燃油续航=%s (%s)",
            fuel["pct"], fuel["pct_key"],
            fuel["volume"], fuel["volume_key"],
            fuel["range"], fuel["range_key"],
        )
        return

    top_keys = list(status.keys()) if isinstance(status, dict) else status
    _LOGGER.warning("[油量诊断] 未找到油量字段，车辆状态顶层字段: %s", top_keys)
    sections: dict[str, Any] = {}
    if isinstance(status, dict):
        for key, value in status.items():
            if isinstance(value, dict):
                sections[key] = list(value.keys())
    _LOGGER.warning("[油量诊断] 各分区子字段: %s", sections)

    # 兜底：把整棵状态树的字段名摊平打出来，便于确认该车型真实的油量字段名
    flat_keys = [k for k, _ in _walk_pairs(status) if isinstance(k, str)]
    _LOGGER.warning(
        "[油量诊断] 状态树全部字段名(%d): %s", len(flat_keys), flat_keys[:200],
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Geely Galaxy sensors based on a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    # 车辆状态/信息
    vehicle_status = coordinator.data.get("vehicle_status", {}) if coordinator.data else {}
    vehicle_info = coordinator.data.get("vehicle_info", {}) if coordinator.data else {}

    # 是否为 XCHANGER 通道车型（L7 等）
    is_xchanger = bool(vehicle_status.get("_xchanger_extra"))

    # 是否为燃油/插混车型：决定是否注册油量相关传感器
    # 注意：distanceToEmpty 在纯电车型上表示总续航，有歧义，
    # 因此不能仅凭它能解析出「续航」就判定为燃油车，
    # 必须以油量百分比/容积或燃油特征字段（fuel*/engine*）为准。
    fuel = discover_fuel(vehicle_status)
    fuel_vehicle = (
        is_xchanger
        or fuel["pct"] is not None
        or fuel["volume"] is not None
        or _has_fuel_indicator(vehicle_status, vehicle_info)
    )
    if fuel_vehicle:
        _log_fuel_diagnostics(vehicle_status, fuel)
    else:
        _LOGGER.debug(
            "[油量诊断] 判定为非燃油车型，不注册油量传感器；状态顶层字段: %s",
            list(vehicle_status.keys()) if isinstance(vehicle_status, dict) else vehicle_status,
        )

    entities: list[GeelyBaseSensor] = [
        # 基础信息
        GeelyVehicleModelSensor(coordinator, entry),
        GeelyVinSensor(coordinator, entry),
        # 签到
        GeelySignStateSensor(coordinator, entry),
        # 电池和续航
        GeelyBatteryLevelSensor(coordinator, entry),
        GeelyRangeSensor(coordinator, entry),
        GeelyOdometerSensor(coordinator, entry),
        GeelyChargeTimeSensor(coordinator, entry),
        # 温度和环境
        GeelyInteriorTempSensor(coordinator, entry),
        GeelyExteriorTempSensor(coordinator, entry),
        GeelyPM25Sensor(coordinator, entry),
        GeelyPowerConsumptionSensor(coordinator, entry),
        # 状态
        GeelyDoorLockSensor(coordinator, entry),
        GeelyTrunkLockSensor(coordinator, entry),
        GeelyAcStatusSensor(coordinator, entry),
        # 充电相关
        GeelyChargingStatusSensor(coordinator, entry),
        GeelyChargingPowerSensor(coordinator, entry),
        GeelyChargingVoltageSensor(coordinator, entry),
        GeelyChargingCurrentSensor(coordinator, entry),
        GeelyLastChargeSocSensor(coordinator, entry),
        GeelyLastChargeEnergySensor(coordinator, entry),
    ]

    # VC 独有传感器（XCHANGER 车型不注册）
    if not is_xchanger:
        entities.extend([
            GeelySentryModeSensor(coordinator, entry),
            GeelyChargeReservationSensor(coordinator, entry),
        ])

    # 燃油/插混车型通用传感器（VC 与 XCHANGER 通道都注册）
    if fuel_vehicle:
        entities.extend([
            GeelyCombinedRangeSensor(coordinator, entry),
            GeelyFuelRangeSensor(coordinator, entry),
            GeelyFuelLevelSensor(coordinator, entry),
        ])

    # XCHANGER 车型专用传感器（L7 等 PHEV 四轮胎压）
    if is_xchanger:
        entities.extend([
            GeelyTirePressureSensor(coordinator, entry, "driver", "左前"),
            GeelyTirePressureSensor(coordinator, entry, "passenger", "右前"),
            GeelyTirePressureSensor(coordinator, entry, "driver_rear", "左后"),
            GeelyTirePressureSensor(coordinator, entry, "passenger_rear", "右后"),
        ])

    # 为每个家用充电桩创建独立设备和传感器
    home_chargers = coordinator.data.get("home_chargers", {}) if coordinator.data else {}
    for piling_code, charger_data in home_chargers.items():
        charger_name = charger_data.get("info", {}).get("name", "充电桩")
        entities.extend([
            HomeChargerStatusSensor(coordinator, entry, piling_code, charger_name),
            HomeChargerPowerSensor(coordinator, entry, piling_code, charger_name),
            HomeChargerVoltageSensor(coordinator, entry, piling_code, charger_name),
            HomeChargerCurrentSensor(coordinator, entry, piling_code, charger_name),
            HomeChargerLastRecordSensor(coordinator, entry, piling_code, charger_name),
            HomeChargerRecordsSensor(coordinator, entry, piling_code, charger_name),
        ])

    # ========== 新增：车辆/车窗/天窗/遮阳帘状态 + 胎压传感器 ==========
    entities.extend([
        GeelyVehicleStateSensor(coordinator, entry),
        GeelyWindowStatusSensor(coordinator, entry),
        GeelySunroofStatusSensor(coordinator, entry),
        GeelySunshadeStatusSensor(coordinator, entry),
        GeelyTirePressureFL(coordinator, entry),
        GeelyTirePressureFR(coordinator, entry),
        GeelyTirePressureRL(coordinator, entry),
        GeelyTirePressureRR(coordinator, entry),
    ])
    async_add_entities(entities)


class GeelyBaseSensor(CoordinatorEntity, SensorEntity):
    """Base class for Geely Galaxy sensors."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._entry = entry

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
    def vehicle_status(self) -> dict[str, Any]:
        """Get vehicle status from coordinator data."""
        if not self.coordinator.data:
            return {}
        return self.coordinator.data.get("vehicle_status", {})

    @property
    def switch_status(self) -> dict[str, Any]:
        """Get switch status from coordinator data."""
        if not self.coordinator.data:
            return {}
        return self.coordinator.data.get("switch_status", {})

    @property
    def vehicle_info(self) -> dict[str, Any]:
        """Get vehicle info from coordinator data."""
        if not self.coordinator.data:
            return {}
        return self.coordinator.data.get("vehicle_info", {})

    @property
    def last_soc(self) -> dict[str, Any]:
        """Get last SOC info from coordinator data."""
        if not self.coordinator.data:
            return {}
        return self.coordinator.data.get("last_soc", {})

    @property
    def charge_records(self) -> dict[str, Any]:
        """Get charge records from coordinator data."""
        if not self.coordinator.data:
            return {}
        return self.coordinator.data.get("charge_records", {})

    @property
    def reservation_info(self) -> dict[str, Any]:
        """Get reservation info from coordinator data."""
        if not self.coordinator.data:
            return {}
        return self.coordinator.data.get("reservation_info", {})

    @property
    def home_charger_list(self) -> list[dict[str, Any]]:
        """Get home charger list from coordinator data."""
        if not self.coordinator.data:
            return []
        return self.coordinator.data.get("home_charger_list", [])

    @property
    def home_charger_status(self) -> dict[str, Any]:
        """Get home charger status from coordinator data."""
        if not self.coordinator.data:
            return {}
        return self.coordinator.data.get("home_charger_status", {})

    @property
    def home_charger_charging_data(self) -> dict[str, Any]:
        """Get home charger charging data from coordinator data."""
        if not self.coordinator.data:
            return {}
        return self.coordinator.data.get("home_charger_charging_data", {})

    @property
    def home_charger_last_record(self) -> dict[str, Any]:
        """Get home charger last record from coordinator data."""
        if not self.coordinator.data:
            return {}
        return self.coordinator.data.get("home_charger_last_record", {})

    @property
    def home_charger_records(self) -> list[dict[str, Any]]:
        """Get home charger records from coordinator data."""
        if not self.coordinator.data:
            return []
        return self.coordinator.data.get("home_charger_records", [])

    @property
    def sign_state(self) -> dict[str, Any]:
        """Get sign state from coordinator data."""
        if not self.coordinator.data:
            return {}
        return self.coordinator.data.get("sign_state", {})


class GeelySignStateSensor(GeelyBaseSensor):
    """Sensor for daily sign-in state."""

    _attr_name = "签到状态"
    _attr_icon = "mdi:calendar-check"

    def __init__(self, coordinator: DataUpdateCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_sign_state"

    @property
    def native_value(self) -> str | None:
        """Return sign state: 已签到 / 未签到 / 未知"""
        sign_data = self.sign_state
        if not sign_data:
            return "未知"
        signed = sign_data.get("signed")
        if signed is True:
            return "已签到"
        elif signed is False:
            return "未签到"
        return "未知"


class GeelyVehicleModelSensor(GeelyBaseSensor):
    """Sensor for vehicle model."""

    _attr_name = "车型"
    _attr_icon = "mdi:car"

    def __init__(self, coordinator: DataUpdateCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_model"

    @property
    def native_value(self) -> str | None:
        """Return the vehicle model."""
        return self.vehicle_info.get("seriesNameVs")


class GeelyVinSensor(GeelyBaseSensor):
    """Sensor for VIN."""

    _attr_name = "车架号"
    _attr_icon = "mdi:identifier"

    def __init__(self, coordinator: DataUpdateCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_vin"

    @property
    def native_value(self) -> str | None:
        """Return the VIN."""
        return self.vehicle_info.get("vin")


class GeelyBatteryLevelSensor(GeelyBaseSensor):
    """Sensor for battery level."""

    _attr_name = "电池电量"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:battery"

    def __init__(self, coordinator: DataUpdateCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_battery_level"

    @property
    def native_value(self) -> int | None:
        """Return the battery level."""
        battery_status = self.vehicle_status.get("vehicleBatteryStatus", {})
        level = battery_status.get("chargeLevel")
        if level is not None:
            try:
                # 车端可能返回 "80.0" 这类浮点字符串，先转 float 再转 int
                return int(float(level))
            except (TypeError, ValueError):
                return None
        return None

    @property
    def icon(self) -> str:
        """Return the icon based on battery level."""
        level = self.native_value
        if level is None:
            return "mdi:battery-unknown"
        if level >= 90:
            return "mdi:battery"
        if level >= 70:
            return "mdi:battery-80"
        if level >= 50:
            return "mdi:battery-60"
        if level >= 30:
            return "mdi:battery-40"
        if level >= 10:
            return "mdi:battery-20"
        return "mdi:battery-alert"


class GeelyRangeSensor(GeelyBaseSensor):
    """Sensor for remaining range."""

    _attr_name = "电量续航里程"
    _attr_native_unit_of_measurement = UnitOfLength.KILOMETERS
    _attr_device_class = SensorDeviceClass.DISTANCE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:road-variant"

    def __init__(self, coordinator: DataUpdateCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_range"

    @property
    def native_value(self) -> int | None:
        """Return the remaining range."""
        basic_status = self.vehicle_status.get("basicVehicleStatus", {})
        range_value = basic_status.get("distanceToEmptyOnBatteryOnly")
        if range_value is not None:
            return int(range_value)
        return None


class GeelyOdometerSensor(GeelyBaseSensor):
    """Sensor for odometer."""

    _attr_name = "总里程"
    _attr_native_unit_of_measurement = UnitOfLength.KILOMETERS
    _attr_device_class = SensorDeviceClass.DISTANCE
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_icon = "mdi:counter"

    def __init__(self, coordinator: DataUpdateCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_odometer"

    @property
    def native_value(self) -> float | None:
        """Return the odometer reading."""
        basic_status = self.vehicle_status.get("basicVehicleStatus", {})
        odometer = basic_status.get("odometer")
        if odometer is not None:
            return round(float(odometer), 1)
        return None


class GeelyChargeTimeSensor(GeelyBaseSensor):
    """Sensor for time to fully charged."""

    _attr_name = "充满剩余时间"
    _attr_native_unit_of_measurement = "min"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_icon = "mdi:battery-clock"

    def __init__(self, coordinator: DataUpdateCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_charge_time"

    @property
    def native_value(self) -> int | None:
        """Return the time to fully charged."""
        battery_status = self.vehicle_status.get("vehicleBatteryStatus", {})
        time_value = battery_status.get("timeToFullyCharged")
        if time_value is not None:
            time_int = int(time_value)
            # 2047 表示未在充电
            if time_int == 2047:
                return None
            return time_int
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        battery_status = self.vehicle_status.get("vehicleBatteryStatus", {})
        time_value = battery_status.get("timeToFullyCharged")
        if time_value is not None and int(time_value) == 2047:
            return {"charging": False}
        return {"charging": True}


class GeelyInteriorTempSensor(GeelyBaseSensor):
    """Sensor for interior temperature."""

    _attr_name = "车内温度"
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:thermometer"

    def __init__(self, coordinator: DataUpdateCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_interior_temp"

    @property
    def native_value(self) -> float | None:
        """Return the interior temperature."""
        env_status = self.vehicle_status.get("vehicleEnvironmentStatus", {})
        temp = env_status.get("interiorTemp")
        if temp is not None:
            return float(temp)
        return None


class GeelyExteriorTempSensor(GeelyBaseSensor):
    """Sensor for exterior temperature."""

    _attr_name = "车外温度"
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:thermometer"

    def __init__(self, coordinator: DataUpdateCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_exterior_temp"

    @property
    def native_value(self) -> float | None:
        """Return the exterior temperature."""
        env_status = self.vehicle_status.get("vehicleEnvironmentStatus", {})
        temp = env_status.get("exteriorTemp")
        if temp is not None:
            return float(temp)
        return None


class GeelyPM25Sensor(GeelyBaseSensor):
    """Sensor for interior PM2.5."""

    _attr_name = "车内PM2.5"
    _attr_native_unit_of_measurement = "μg/m³"
    _attr_device_class = SensorDeviceClass.PM25
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:air-filter"

    def __init__(self, coordinator: DataUpdateCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_pm25"

    @property
    def native_value(self) -> int | None:
        """Return the PM2.5 level."""
        env_status = self.vehicle_status.get("vehicleEnvironmentStatus", {})
        pm25 = env_status.get("interiorPM25Level")
        if pm25 is not None:
            return int(pm25)
        return None


class GeelyPowerConsumptionSensor(GeelyBaseSensor):
    """Sensor for average power consumption."""

    _attr_name = "平均能耗"
    _attr_native_unit_of_measurement = "kWh/100km"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:lightning-bolt"

    def __init__(self, coordinator: DataUpdateCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_power_consumption"

    @property
    def native_value(self) -> float | None:
        """Return the average power consumption."""
        running_status = self.vehicle_status.get("vehicleRunningStatus", {})
        consumption = running_status.get("averPowerConsumption")
        if consumption is not None:
            return round(float(consumption), 1)
        return None


class GeelyDoorLockSensor(GeelyBaseSensor):
    """Sensor for door lock status."""

    _attr_name = "车锁状态"
    _attr_icon = "mdi:car-door-lock"

    def __init__(self, coordinator: DataUpdateCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_door_lock"

    @property
    def native_value(self) -> str | None:
        """Return the door lock status."""
        door_status = self.vehicle_status.get("vehicleDoorCoverStatus", {})
        lock_status = door_status.get("doorLockStatusDriver")
        is_xchanger = bool(self.vehicle_status.get("_xchanger_extra"))
        # VC: "2"=已锁, "1"=已解锁; XCHANGER: "0"=已锁, "1"=已驻车
        if lock_status in ("2", "0"):
            return "已锁定"
        elif lock_status == "1":
            return "已驻车" if is_xchanger else "已解锁"
        return None

    @property
    def icon(self) -> str:
        """Return icon based on lock status."""
        if self.native_value == "已锁定":
            return "mdi:car-door-lock"
        return "mdi:car-door"


class GeelyTrunkLockSensor(GeelyBaseSensor):
    """Sensor for trunk lock/door status (后备箱状态，仅读取，不控制).

    重要说明：
    1. 三个公开参考项目（lyj5812/geely-galaxy、kooritea/geely_galaxy_ha、
       suyunkai/geely-galaxy-assistant）均未实现银河 A7 的尾箱远程控制：
       - suyunkai/jlyh.js 里 control/door 的 position 固定为 0（仅控制全车门锁），
         没有任何尾门控制调用；全部接口列表里也不存在 /trunk /tailgate 接口；
       - kooritea 的 RTU 指令走 device-api.xchanger.cn（老车型 XCHANGER 通道），
         银河 A7 不通该域名；
       - lyj5812 原仓库未实现后备箱控制，之前误加的 position=1 实测会解锁主驾车门。
    2. 当前版本仅显示状态，不提供控制开关，避免误解锁车门造成安全隐患。
       尾门远程控制接口需要抓包获取真实请求路径和参数后才能正确实现。
    3. 字段映射（与 jlyh.js 实测一致）：trunkLockStatus '1'=已解锁, '2'=已锁定。
       该字段是"锁止"状态，不等同于尾门物理开启；为避免误报只输出锁状态。
    """

    _attr_name = "后备箱状态"
    _attr_icon = "mdi:car-back"

    def __init__(self, coordinator: DataUpdateCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_trunk_lock"

    @property
    def native_value(self) -> str | None:
        """Return the trunk lock/door status."""
        door_status = self.vehicle_status.get("vehicleDoorCoverStatus", {})
        lock = door_status.get("trunkLockStatus")
        # VC通道 trunkLockStatus 实测映射：'1'=锁定（关闭锁止）, '2'=解锁（可能已开启）
        # 同时兼容物理开启状态字段（若服务端返回）
        open_status = door_status.get("trunkOpenStatus") or door_status.get("trunkStatus")
        if open_status is not None:
            if open_status in ("1", True, "true", "open"):
                return "已开启"
            elif open_status in ("0", "2", False, "false", "closed"):
                return "已关闭"
        # 回退到锁状态判断
        if lock == "1":
            return "已锁定/关闭"
        elif lock == "2":
            return "已解锁"
        return None

    @property
    def extra_state_attributes(self) -> dict:
        """Return extra attributes with raw fields for diagnostics."""
        door_status = self.vehicle_status.get("vehicleDoorCoverStatus", {})
        return {
            "trunkLockStatus_raw": door_status.get("trunkLockStatus"),
            "trunkOpenStatus_raw": door_status.get("trunkOpenStatus"),
            "tailgateOpenStatus_raw": door_status.get("tailgateOpenStatus"),
            "raw_fields": sorted(
                [
                    k
                    for k in door_status.keys()
                    if "trunk" in k.lower() or "boot" in k.lower() or "tailgate" in k.lower()
                ]
            ),
            "vehicleDoorCoverStatus_raw": door_status,
        }

    @property
    def icon(self) -> str:
        """Return icon based on status."""
        val = self.native_value
        if val and ("开启" in val or "解锁" in val):
            return "mdi:car-back"
        return "mdi:car-side"


class GeelyAcStatusSensor(GeelyBaseSensor):
    """Sensor for AC status."""

    _attr_name = "空调状态"
    _attr_icon = "mdi:air-conditioner"

    def __init__(self, coordinator: DataUpdateCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_ac_status"

    @property
    def native_value(self) -> str | None:
        """Return the AC status."""
        climate_status = self.vehicle_status.get("vehicleClimateStatus", {})
        ac_active = climate_status.get("preClimateActive")
        if ac_active is True:
            return "开启"
        elif ac_active is False:
            return "关闭"
        return None


class GeelySentryModeSensor(GeelyBaseSensor):
    """Sensor for sentry mode status."""

    _attr_name = "哨兵模式"
    _attr_icon = "mdi:shield-car"

    def __init__(self, coordinator: DataUpdateCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_sentry_mode"

    @property
    def native_value(self) -> str | None:
        """Return the sentry mode status."""
        sentry_status = self.switch_status.get("vstdModeStatus")
        if sentry_status == "1":
            return "开启"
        elif sentry_status == "0":
            return "关闭"
        return None

    @property
    def icon(self) -> str:
        """Return icon based on sentry mode."""
        if self.native_value == "开启":
            return "mdi:shield-car"
        return "mdi:shield-off"


class GeelyChargingStatusSensor(GeelyBaseSensor):
    """Sensor for charging status."""

    _attr_name = "充电状态"
    _attr_icon = "mdi:ev-station"

    def __init__(self, coordinator: DataUpdateCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_charging_status"

    @property
    def native_value(self) -> str | None:
        """Return the charging status."""
        # 优先从家用充电桩状态获取
        charger_status = self.home_charger_status
        if charger_status:
            status = charger_status.get("status")
            if status is not None:
                status_map = {
                    0: "空闲",
                    1: "已插枪",
                    2: "充电中",
                    4: "充电完成",
                    "0": "空闲",
                    "1": "已插枪",
                    "2": "充电中",
                    "4": "充电完成",
                }
                return status_map.get(status, f"状态({status})")

        # 回退到 last_soc 数据
        soc_data = self.last_soc
        charging_status = soc_data.get("chargingStatus") or soc_data.get("chargeStatus")
        if charging_status is not None:
            status_map = {
                0: "未充电",
                1: "充电中",
                2: "充电完成",
                3: "充电暂停",
                "0": "未充电",
                "1": "充电中",
                "2": "充电完成",
                "3": "充电暂停",
            }
            return status_map.get(charging_status, f"未知({charging_status})")

        # 回退到 XCHANGER 车辆状态 (L7 等)
        extra = self.vehicle_status.get("_xchanger_extra", {})
        charge_sts = extra.get("chargeSts")
        if charge_sts is not None:
            xc_map = {
                "0": "未充电", "1": "充电中", "2": "充电完成", "3": "未连接",
            }
            return xc_map.get(str(charge_sts), f"未知({charge_sts})")

        return None

    @property
    def icon(self) -> str:
        """Return icon based on charging status."""
        if self.native_value == "充电中":
            return "mdi:battery-charging"
        elif self.native_value == "充电完成":
            return "mdi:battery-check"
        elif self.native_value == "已插枪":
            return "mdi:ev-plug-type2"
        return "mdi:ev-station"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        attrs = {}
        # 家用充电桩数据
        charger_status = self.home_charger_status
        if charger_status:
            for key in ["pilingsCode", "name", "isOnline", "isOwner", "bleName"]:
                if key in charger_status and charger_status[key] is not None:
                    attrs[key] = charger_status[key]
        # last_soc 数据
        soc_data = self.last_soc
        if soc_data:
            if "soc" in soc_data:
                attrs["soc"] = soc_data["soc"]
            if "updateTime" in soc_data:
                attrs["update_time"] = soc_data["updateTime"]
        return attrs


class GeelyChargingPowerSensor(GeelyBaseSensor):
    """Sensor for charging power."""

    _attr_name = "充电功率"
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:flash"

    def __init__(self, coordinator: DataUpdateCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_charging_power"

    @property
    def native_value(self) -> float | None:
        """Return the charging power."""
        # 优先从家用充电桩充电数据获取
        charging_data = self.home_charger_charging_data
        if charging_data:
            power = charging_data.get("power") or charging_data.get("chargingPower") or charging_data.get("realPower")
            if power is not None:
                return round(float(power), 2)

        # 也检查充电桩状态
        charger_status = self.home_charger_status
        if charger_status:
            power = charger_status.get("power") or charger_status.get("chargingPower")
            if power is not None:
                return round(float(power), 2)

        # 回退到 last_soc
        soc_data = self.last_soc
        power = soc_data.get("chargingPower") or soc_data.get("power")
        if power is not None:
            return round(float(power), 2)

        # 回退到 XCHANGER 车辆状态 (L7 等)，用 电压×电流 计算功率
        extra = self.vehicle_status.get("_xchanger_extra", {})
        xc_voltage = extra.get("chargeUAct")
        xc_current = extra.get("chargeIAct")
        if xc_voltage is not None and xc_current is not None:
            return round(float(xc_voltage) * float(xc_current) / 1000, 2)
        return None


class GeelyChargingVoltageSensor(GeelyBaseSensor):
    """Sensor for charging voltage."""

    _attr_name = "充电电压"
    _attr_native_unit_of_measurement = "V"
    _attr_device_class = SensorDeviceClass.VOLTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:lightning-bolt"

    def __init__(self, coordinator: DataUpdateCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_charging_voltage"

    @property
    def native_value(self) -> float | None:
        """Return the charging voltage."""
        # 优先从家用充电桩充电数据获取
        charging_data = self.home_charger_charging_data
        if charging_data:
            voltage = charging_data.get("voltage") or charging_data.get("chargingVoltage") or charging_data.get("realVoltage")
            if voltage is not None:
                return round(float(voltage), 1)

        # 也检查充电桩状态
        charger_status = self.home_charger_status
        if charger_status:
            voltage = charger_status.get("voltage") or charger_status.get("chargingVoltage")
            if voltage is not None:
                return round(float(voltage), 1)

        # 回退到 last_soc
        soc_data = self.last_soc
        voltage = soc_data.get("voltage") or soc_data.get("chargingVoltage")
        if voltage is not None:
            return round(float(voltage), 1)

        # 回退到 XCHANGER 车辆状态 (L7 等)
        extra = self.vehicle_status.get("_xchanger_extra", {})
        xc_voltage = extra.get("chargeUAct")
        if xc_voltage is not None:
            return round(float(xc_voltage), 1)
        return None


class GeelyChargingCurrentSensor(GeelyBaseSensor):
    """Sensor for charging current."""

    _attr_name = "充电电流"
    _attr_native_unit_of_measurement = "A"
    _attr_device_class = SensorDeviceClass.CURRENT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:current-ac"

    def __init__(self, coordinator: DataUpdateCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_charging_current"

    @property
    def native_value(self) -> float | None:
        """Return the charging current."""
        # 优先从家用充电桩充电数据获取
        charging_data = self.home_charger_charging_data
        if charging_data:
            current = charging_data.get("current") or charging_data.get("chargingCurrent") or charging_data.get("realCurrent") or charging_data.get("currentA")
            if current is not None:
                return round(float(current), 1)

        # 也检查充电桩状态
        charger_status = self.home_charger_status
        if charger_status:
            current = charger_status.get("current") or charger_status.get("chargingCurrent") or charger_status.get("currentA")
            if current is not None:
                return round(float(current), 1)

        # 回退到 last_soc
        soc_data = self.last_soc
        current = soc_data.get("current") or soc_data.get("chargingCurrent")
        if current is not None:
            return round(float(current), 1)

        # 回退到 XCHANGER 车辆状态 (L7 等)
        extra = self.vehicle_status.get("_xchanger_extra", {})
        xc_current = extra.get("chargeIAct")
        if xc_current is not None:
            return round(float(xc_current), 1)
        return None


class GeelyLastChargeSocSensor(GeelyBaseSensor):
    """Sensor for last charge SOC."""

    _attr_name = "最后充电电量"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:battery-charging-100"

    def __init__(self, coordinator: DataUpdateCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_last_charge_soc"

    @property
    def native_value(self) -> int | None:
        """Return the last charge SOC."""
        # 优先从家用充电桩最后记录获取
        last_record = self.home_charger_last_record
        if last_record:
            soc = last_record.get("endSoc") or last_record.get("soc") or last_record.get("chargeLevel")
            if soc is not None:
                return int(soc)

        # 回退到 last_soc
        soc_data = self.last_soc
        soc = soc_data.get("soc") or soc_data.get("chargeLevel")
        if soc is not None:
            return int(soc)
        return None


class GeelyChargeReservationSensor(GeelyBaseSensor):
    """Sensor for charge reservation status."""

    _attr_name = "预约充电"
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator: DataUpdateCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_charge_reservation"

    @property
    def native_value(self) -> str | None:
        """Return the charge reservation status."""
        reservation = self.reservation_info
        if not reservation:
            return None

        enabled = reservation.get("enabled") or reservation.get("reservationEnabled")
        if enabled in (True, 1, "1"):
            start_time = reservation.get("startTime", "")
            end_time = reservation.get("endTime", "")
            if start_time and end_time:
                return f"已开启 ({start_time}-{end_time})"
            return "已开启"
        elif enabled in (False, 0, "0"):
            return "未开启"
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        reservation = self.reservation_info
        if not reservation:
            return {}

        attrs = {}
        for key in ["startTime", "endTime", "targetSoc", "departuretime", "departureEnabled"]:
            if key in reservation:
                attrs[key] = reservation[key]
        return attrs


class GeelyLastChargeEnergySensor(GeelyBaseSensor):
    """Sensor for last charge energy."""

    _attr_name = "上次充电电量"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_icon = "mdi:battery-plus"

    def __init__(self, coordinator: DataUpdateCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_last_charge_energy"

    @property
    def native_value(self) -> float | None:
        """Return the last charge energy."""
        # 优先从家用充电桩最后记录获取
        last_record = self.home_charger_last_record
        if last_record:
            energy = last_record.get("chargeEnergy") or last_record.get("energy") or last_record.get("totalEnergy") or last_record.get("chargeCapacity")
            if energy is not None:
                return round(float(energy), 2)

        # 回退到车辆充电记录
        records = self.charge_records
        if not records:
            return None

        # 获取记录列表
        record_list = records.get("list") or records.get("records") or []
        if not record_list:
            return None

        # 取最近一条记录
        latest = record_list[0] if record_list else {}
        energy = latest.get("chargeEnergy") or latest.get("energy") or latest.get("totalEnergy")
        if energy is not None:
            return round(float(energy), 2)
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        attrs = {}

        # 优先从家用充电桩最后记录获取
        last_record = self.home_charger_last_record
        if last_record:
            for key in ["startTime", "endTime", "startSoc", "endSoc", "duration", "chargeType", "pilingsCode"]:
                if key in last_record and last_record[key]:
                    attrs[key] = last_record[key]
            if attrs:
                return attrs

        # 回退到车辆充电记录
        records = self.charge_records
        if not records:
            return {}

        record_list = records.get("list") or records.get("records") or []
        if not record_list:
            return {}

        latest = record_list[0] if record_list else {}
        for key in ["startTime", "endTime", "startSoc", "endSoc", "duration", "chargeType", "stationName"]:
            if key in latest and latest[key]:
                attrs[key] = latest[key]
        return attrs


class GeelyCombinedRangeSensor(GeelyBaseSensor):
    """Sensor for combined range (PHEV electric + fuel)."""

    _attr_name = "综合续航"
    _attr_native_unit_of_measurement = UnitOfLength.KILOMETERS
    _attr_device_class = SensorDeviceClass.DISTANCE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:map-marker-distance"

    def __init__(self, coordinator: DataUpdateCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_combined_range"

    @property
    def native_value(self) -> int | None:
        """Return the combined range (electric + fuel)."""
        status = self.vehicle_status
        electric = discover_ev_range(status)
        fuel_range = discover_fuel(status)["range"]

        values = [v for v in (electric, fuel_range) if v is not None]
        if not values:
            return None
        # 只有一项可用时直接返回该项，避免综合续航变成未知
        return round(sum(values))


class GeelyFuelRangeSensor(GeelyBaseSensor):
    """Sensor for fuel-only range (PHEV)."""

    _attr_name = "油量续航"
    _attr_native_unit_of_measurement = UnitOfLength.KILOMETERS
    _attr_device_class = SensorDeviceClass.DISTANCE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:gas-station"

    def __init__(self, coordinator: DataUpdateCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_fuel_range"

    @property
    def native_value(self) -> int | None:
        """Return the fuel-only range."""
        status = self.vehicle_status

        value = discover_fuel(status)["range"]
        if value is None:
            # 常见位置的兜底（VC 与 XCHANGER 转换后都放在此路径）
            basic_status = status.get("basicVehicleStatus", {})
            value = _to_float(basic_status.get("distanceToEmpty"))

        if value is None:
            return None
        return round(value)


class GeelyFuelLevelSensor(GeelyBaseSensor):
    """Sensor for fuel level (PHEV)."""

    _attr_name = "油量"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:gas-station"

    def __init__(self, coordinator: DataUpdateCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_fuel_level"

    def _fuel(self) -> dict[str, Any]:
        """Return discovered fuel data."""
        return discover_fuel(self.vehicle_status)

    @property
    def native_value(self) -> float | None:
        """优先返回油量百分比，缺失时回退到油量容积（升）。"""
        fuel = self._fuel()
        if fuel["pct"] is not None:
            return round(fuel["pct"])
        if fuel["volume"] is not None:
            return round(fuel["volume"], 1)
        return None

    @property
    def native_unit_of_measurement(self) -> str:
        """油量百分比显示 %，回退到容积时显示 L。"""
        fuel = self._fuel()
        if fuel["pct"] is None and fuel["volume"] is not None:
            return "L"
        return PERCENTAGE

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra attributes including fuel liters."""
        fuel = self._fuel()
        attrs: dict[str, Any] = {}
        if fuel["pct"] is not None:
            attrs["fuel_percent"] = round(fuel["pct"], 1)
        if fuel["volume"] is not None:
            attrs["fuel_liters"] = round(fuel["volume"], 1)
        if fuel["range"] is not None:
            attrs["fuel_range_km"] = round(fuel["range"])
        if fuel["source"]:
            attrs["source_field"] = fuel["source"]
        return attrs


class GeelyTirePressureSensor(GeelyBaseSensor):
    """Sensor for tire pressure."""

    _attr_native_unit_of_measurement = UnitOfPressure.KPA
    _attr_device_class = SensorDeviceClass.PRESSURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:tire"

    _POSITION_KEY_MAP = {
        "driver": "tyreStatusDriver",
        "passenger": "tyreStatusPassenger",
        "driver_rear": "tyreStatusDriverRear",
        "passenger_rear": "tyreStatusPassengerRear",
    }

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        entry: ConfigEntry,
        position: str,
        label: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self._position = position
        self._attr_name = f"胎压({label})"
        self._attr_unique_id = f"{entry.entry_id}_tire_pressure_{position}"

    @property
    def native_value(self) -> float | None:
        """Return the tire pressure in kPa."""
        extra = self.vehicle_status.get("_xchanger_extra", {})
        key = self._POSITION_KEY_MAP.get(self._position)
        if key:
            value = extra.get(key)
            if value is not None:
                return round(float(value), 1)
        return None


class HomeChargerBaseSensor(GeelyBaseSensor):
    """Base class for per-charger sensors."""

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        entry: ConfigEntry,
        piling_code: str,
        charger_name: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self._piling_code = piling_code
        self._charger_name = charger_name

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device info for this charger."""
        return {
            "identifiers": {(DOMAIN, f"charger_{self._piling_code}")},
            "name": self._charger_name,
            "manufacturer": "吉利汽车",
            "model": "家用充电桩",
        }

    @property
    def charger_data(self) -> dict[str, Any]:
        """Get this charger's data from coordinator."""
        if not self.coordinator.data:
            return {}
        return self.coordinator.data.get("home_chargers", {}).get(self._piling_code, {})


class HomeChargerStatusSensor(HomeChargerBaseSensor):
    """Sensor for home charger status."""

    _attr_icon = "mdi:ev-station"

    def __init__(
        self, coordinator: DataUpdateCoordinator, entry: ConfigEntry,
        piling_code: str, charger_name: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry, piling_code, charger_name)
        self._attr_name = "状态"
        self._attr_unique_id = f"{entry.entry_id}_charger_{piling_code}_status"

    @property
    def native_value(self) -> str | None:
        """Return the charger status."""
        status = self.charger_data.get("status", {})
        is_online = status.get("isOnline")
        if is_online == 0:
            return "离线"
        equip_status = status.get("status")
        status_map = {
            0: "空闲", "0": "空闲",
            1: "已插枪", "1": "已插枪",
            2: "充电中", "2": "充电中",
            4: "充电完成", "4": "充电完成",
        }
        return status_map.get(equip_status, f"状态({equip_status})")

    @property
    def icon(self) -> str:
        """Return icon based on status."""
        val = self.native_value
        if val == "离线":
            return "mdi:ev-station-off"
        if val == "充电中":
            return "mdi:battery-charging"
        return "mdi:ev-station"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra attributes."""
        status = self.charger_data.get("status", {})
        info = self.charger_data.get("info", {})
        attrs = {}
        for key in [
            "pilingsCode", "isOnline", "isOwner", "bleName", "bleMac",
            "hardwareVersion", "softwareVersion", "quickCharge",
        ]:
            val = status.get(key)
            if val is None:
                val = info.get(key)
            if val is not None:
                attrs[key] = val
        return attrs


class HomeChargerPowerSensor(HomeChargerBaseSensor):
    """Sensor for home charger real-time charging power."""

    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:flash"

    def __init__(
        self, coordinator: DataUpdateCoordinator, entry: ConfigEntry,
        piling_code: str, charger_name: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry, piling_code, charger_name)
        self._attr_name = "充电功率"
        self._attr_unique_id = f"{entry.entry_id}_charger_{piling_code}_power"

    @property
    def native_value(self) -> float | None:
        """Return the charging power."""
        charging_data = self.charger_data.get("charging_data", {})
        if charging_data:
            power = charging_data.get("power") or charging_data.get("chargingPower") or charging_data.get("realPower")
            if power is not None:
                try:
                    return round(float(power), 2)
                except (ValueError, TypeError):
                    pass
        # 尝试从状态中获取
        status = self.charger_data.get("status", {})
        power = status.get("power") or status.get("chargingPower")
        if power is not None:
            try:
                return round(float(power), 2)
            except (ValueError, TypeError):
                pass
        return None


class HomeChargerVoltageSensor(HomeChargerBaseSensor):
    """Sensor for home charger real-time charging voltage."""

    _attr_native_unit_of_measurement = "V"
    _attr_device_class = SensorDeviceClass.VOLTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:lightning-bolt"

    def __init__(
        self, coordinator: DataUpdateCoordinator, entry: ConfigEntry,
        piling_code: str, charger_name: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry, piling_code, charger_name)
        self._attr_name = "充电电压"
        self._attr_unique_id = f"{entry.entry_id}_charger_{piling_code}_voltage"

    @property
    def native_value(self) -> float | None:
        """Return the charging voltage."""
        charging_data = self.charger_data.get("charging_data", {})
        if charging_data:
            voltage = charging_data.get("voltage") or charging_data.get("chargingVoltage") or charging_data.get("realVoltage")
            if voltage is not None:
                try:
                    return round(float(voltage), 1)
                except (ValueError, TypeError):
                    pass
        status = self.charger_data.get("status", {})
        voltage = status.get("voltage") or status.get("chargingVoltage")
        if voltage is not None:
            try:
                return round(float(voltage), 1)
            except (ValueError, TypeError):
                pass
        return None


class HomeChargerCurrentSensor(HomeChargerBaseSensor):
    """Sensor for home charger real-time charging current."""

    _attr_native_unit_of_measurement = "A"
    _attr_device_class = SensorDeviceClass.CURRENT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:current-ac"

    def __init__(
        self, coordinator: DataUpdateCoordinator, entry: ConfigEntry,
        piling_code: str, charger_name: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry, piling_code, charger_name)
        self._attr_name = "充电电流"
        self._attr_unique_id = f"{entry.entry_id}_charger_{piling_code}_current"

    @property
    def native_value(self) -> float | None:
        """Return the charging current."""
        charging_data = self.charger_data.get("charging_data", {})
        if charging_data:
            current = charging_data.get("current") or charging_data.get("chargingCurrent") or charging_data.get("realCurrent") or charging_data.get("currentA")
            if current is not None:
                try:
                    return round(float(current), 1)
                except (ValueError, TypeError):
                    pass
        status = self.charger_data.get("status", {})
        current = status.get("current") or status.get("chargingCurrent") or status.get("currentA")
        if current is not None:
            try:
                return round(float(current), 1)
            except (ValueError, TypeError):
                pass
        return None


class HomeChargerLastRecordSensor(HomeChargerBaseSensor):
    """Sensor for home charger last charge record."""

    _attr_native_unit_of_measurement = "kWh"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_icon = "mdi:battery-charging-100"

    def __init__(
        self, coordinator: DataUpdateCoordinator, entry: ConfigEntry,
        piling_code: str, charger_name: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry, piling_code, charger_name)
        self._attr_name = "最近充电"
        self._attr_unique_id = f"{entry.entry_id}_charger_{piling_code}_last_record"

    @property
    def native_value(self) -> float | None:
        """Return the energy of the last charge record."""
        last_record = self.charger_data.get("last_record", {})
        if not last_record:
            return None
        energy = last_record.get("degree") or last_record.get("totalDegree")
        if energy is not None:
            try:
                return round(float(energy), 2)
            except (ValueError, TypeError):
                pass
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra attributes with charge details."""
        last_record = self.charger_data.get("last_record", {})
        if not last_record:
            return {}
        attrs = {}
        field_map = {
            "startTime": "开始时间",
            "endTime": "结束时间",
            "degree": "充电电量(kWh)",
            "duration": "充电时长(分钟)",
            "chargeTypeDesc": "充电类型",
            "chargerName": "充电者",
        }
        for key, label in field_map.items():
            if key in last_record and last_record[key] is not None:
                attrs[label] = last_record[key]
        return attrs


class HomeChargerRecordsSensor(HomeChargerBaseSensor):
    """Sensor for home charger records."""

    _attr_icon = "mdi:clipboard-list"

    def __init__(
        self, coordinator: DataUpdateCoordinator, entry: ConfigEntry,
        piling_code: str, charger_name: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry, piling_code, charger_name)
        self._attr_name = "充电记录"
        self._attr_unique_id = f"{entry.entry_id}_charger_{piling_code}_records"

    @property
    def native_value(self) -> int:
        """Return the number of charge records on current page."""
        return len(self.charger_data.get("records", []))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes with paginated records."""
        records = self.charger_data.get("records", [])
        current_page = self.charger_data.get("records_page", 1)
        records_total = self.charger_data.get("records_total", 0)

        if not records:
            return {
                "records": [], "current_page": current_page,
                "total_records": records_total,
                "total_count": 0,
            }

        # 格式化记录列表
        formatted_records = []
        for i, record in enumerate(records):
            formatted_record = {"序号": (current_page - 1) * 10 + i + 1}
            field_map = {
                "startTime": "开始时间",
                "endTime": "结束时间",
                "degree": "充电电量(kWh)",
                "chargeTypeDesc": "充电类型",
                "chargerName": "充电者",
            }
            for key, label in field_map.items():
                if key in record and record[key] is not None:
                    formatted_record[label] = record[key]
            formatted_records.append(formatted_record)

        # 当前页统计
        total_energy = 0
        for record in records:
            energy = record.get("degree") or 0
            try:
                total_energy += float(energy)
            except (ValueError, TypeError):
                pass

        return {
            "total_count": len(records),
            "total_energy_kwh": round(total_energy, 2),
            "current_page": current_page,
            "total_records": records_total,
            "records": formatted_records,
        }


# ============================================================================
# 新增：车辆状态、车窗/天窗/遮阳帘状态、胎压传感器
# ============================================================================

def _find_value_recursive(data: Any, key_names: tuple[str, ...], case_insensitive: bool = True) -> Any:
    """递归查找字段，支持多候选名"""
    if data is None:
        return None
    if isinstance(data, dict):
        for k, v in data.items():
            k_str = str(k)
            k_cmp = k_str.lower() if case_insensitive else k_str
            for target in key_names:
                t_cmp = target.lower() if case_insensitive else target
                if k_cmp == t_cmp:
                    if v is not None:
                        return v
            # 递归子节点
            res = _find_value_recursive(v, key_names, case_insensitive)
            if res is not None:
                return res
    elif isinstance(data, (list, tuple)):
        for item in data:
            res = _find_value_recursive(item, key_names, case_insensitive)
            if res is not None:
                return res
    return None


def _safe_float(val: Any) -> float | None:
    """安全转浮点数，兼容字符串带引号"""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        s = val.strip().strip('"').strip("'")
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _safe_int(val: Any) -> int | None:
    f = _safe_float(val)
    if f is None:
        return None
    return int(round(f))


def _get_status_value(data: Any, key_names: tuple[str, ...]) -> str | None:
    """获取状态值并转字符串（用于判断1/2/3）"""
    val = _find_value_recursive(data, key_names)
    if val is None:
        return None
    return str(val).strip()


class GeelyVehicleStateSensor(CoordinatorEntity, SensorEntity):
    """车辆状态：已熄火/驻车/空挡/行驶中"""

    _attr_icon = "mdi:car-electric"
    _attr_name = "车辆状态"

    def __init__(self, coordinator: DataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_vehicle_state"
        try:
            self._device_name = coordinator.data.get("device_name", "吉利银河") if coordinator.data else "吉利银河"
            self._vin = coordinator.data.get("vin", "") if coordinator.data else ""
        except Exception:
            self._device_name = "吉利银河"
            self._vin = ""

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._vin or self.unique_id)},
            "name": self._device_name,
            "manufacturer": "吉利银河",
        }

    @property
    def native_value(self) -> str:
        data = self.coordinator.data if self.coordinator.data else {}
        vehicle_status = data.get("vehicle_status", {})

        ign = _get_status_value(vehicle_status, ("ignitionOn", "engineOn", "engineStatus", "ignitionStatus", "powerStatus"))
        gear = _get_status_value(vehicle_status, ("gearValue", "gearPosition", "gear", "currentGear", "shiftPosition"))
        speed = _safe_float(_find_value_recursive(vehicle_status, ("speed", "vehicleSpeed", "currentSpeed", "drivingSpeed")))
        if speed is None:
            speed = _safe_float(_find_value_recursive(data, ("speed", "vehicleSpeed")))
        epb = _get_status_value(vehicle_status, ("epbStatus", "parkBrakeStatus", "handbrakeStatus", "handBrakeStatus"))

        attrs = {}
        if speed is not None:
            attrs["车速(km/h)"] = round(speed, 1)
        if gear is not None:
            attrs["挡位"] = gear
        if ign is not None:
            attrs["点火状态"] = ign
        if epb is not None:
            attrs["手刹"] = epb
        self._attrs = attrs

        is_ignition_on = ign in ("1", "ON", "on", "true", "True", True, 1, "start", "START")
        is_moving = speed is not None and speed > 1.0

        if not is_ignition_on:
            return "已熄火"
        if is_moving:
            return "行驶中"
        if gear in ("P", "p", "PARK", "park", "1", 1):
            return "驻车(P挡)"
        if gear in ("N", "n", "NEUTRAL", "neutral", "3", 3):
            return "空挡(N挡)"
        return "已挂挡"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        _ = self.native_value
        return getattr(self, "_attrs", {})


class GeelyWindowStatusSensor(CoordinatorEntity, SensorEntity):
    """车窗状态：全关/微开/全开/部分打开，四窗独立状态"""

    _attr_icon = "mdi:car-window"
    _attr_name = "车窗状态"

    WINDOW_POSITIONS = [
        ("主驾车窗", ("driverWindowStatus", "windowStatusDriver", "driverWindow", "frontLeftWindow", "flWindow", "leftFrontWindowStatus", "windowStatus1")),
        ("副驾车窗", ("passengerWindowStatus", "windowStatusPassenger", "passengerWindow", "frontRightWindow", "frWindow", "rightFrontWindowStatus", "windowStatus2")),
        ("左后车窗", ("rearLeftWindowStatus", "windowStatusRearLeft", "rearLeftWindow", "rlWindow", "leftRearWindowStatus", "windowStatus3")),
        ("右后车窗", ("windowStatusRearRight", "rearRightWindowStatus", "rearRightWindow", "rrWindow", "rightRearWindowStatus", "windowStatus4")),
    ]

    STATUS_MAP = {
        "1": "全开", "open": "全开", "OPEN": "全开", "opened": "全开",
        "2": "全关", "close": "全关", "CLOSE": "全关", "closed": "全关", "0": "全关",
        "3": "微开", "ajar": "微开", "partial": "微开", "half": "微开",
    }

    def __init__(self, coordinator: DataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_window_status"
        try:
            self._device_name = coordinator.data.get("device_name", "吉利银河") if coordinator.data else "吉利银河"
            self._vin = coordinator.data.get("vin", "") if coordinator.data else ""
        except Exception:
            self._device_name = "吉利银河"
            self._vin = ""

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._vin or self.unique_id)},
            "name": self._device_name,
            "manufacturer": "吉利银河",
        }

    def _get_window_state(self, data: Any, keys: tuple[str, ...]) -> str:
        val = _get_status_value(data, keys)
        if val is None:
            return "未知"
        return self.STATUS_MAP.get(val, f"未知({val})")

    @property
    def native_value(self) -> str:
        data = self.coordinator.data if self.coordinator.data else {}
        vehicle_status = data.get("vehicle_status", {})
        per_window = {}
        opened_count = 0
        ajar_count = 0
        unknown_count = 0
        total = 0
        for wname, wkeys in self.WINDOW_POSITIONS:
            state = self._get_window_state(vehicle_status, wkeys)
            per_window[wname] = state
            total += 1
            if state == "全开":
                opened_count += 1
            elif state == "微开":
                ajar_count += 1
            elif state.startswith("未知"):
                unknown_count += 1
        self._attrs = per_window
        if opened_count == 0 and ajar_count == 0 and unknown_count == total:
            return "未知"
        if opened_count == total:
            return "全开"
        if opened_count == 0 and ajar_count == 0:
            return "全关"
        if opened_count == 0 and ajar_count == total:
            return "微开"
        return "部分打开"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        _ = self.native_value
        return getattr(self, "_attrs", {})


class GeelySunroofStatusSensor(CoordinatorEntity, SensorEntity):
    """天窗状态：全关/微开/全开"""

    _attr_icon = "mdi:car-select"
    _attr_name = "天窗状态"
    STATUS_MAP = {
        "1": "全开", "open": "全开", "OPEN": "全开", "opened": "全开",
        "2": "全关", "close": "全关", "CLOSE": "全关", "closed": "全关", "0": "全关",
        "3": "微开", "ajar": "微开", "partial": "微开", "tilt": "微开",
    }

    def __init__(self, coordinator: DataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_sunroof_status"
        try:
            self._device_name = coordinator.data.get("device_name", "吉利银河") if coordinator.data else "吉利银河"
            self._vin = coordinator.data.get("vin", "") if coordinator.data else ""
        except Exception:
            self._device_name = "吉利银河"
            self._vin = ""

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._vin or self.unique_id)},
            "name": self._device_name,
            "manufacturer": "吉利银河",
        }

    @property
    def native_value(self) -> str:
        data = self.coordinator.data if self.coordinator.data else {}
        vs = data.get("vehicle_status", {})
        val = _get_status_value(vs, ("sunroofStatus", "skylightStatus", "roofStatus", "sunroofOpenStatus", "skylightOpenStatus"))
        if val is None:
            return "未知"
        return self.STATUS_MAP.get(val, f"未知({val})")


class GeelySunshadeStatusSensor(CoordinatorEntity, SensorEntity):
    """遮阳帘状态：全关/半开/全开"""

    _attr_icon = "mdi:blinds"
    _attr_name = "遮阳帘状态"
    STATUS_MAP = {
        "1": "全开", "open": "全开", "OPEN": "全开", "opened": "全开", "fullopen": "全开",
        "2": "全关", "close": "全关", "CLOSE": "全关", "closed": "全关", "0": "全关",
        "3": "半开", "half": "半开", "ajar": "半开", "partial": "半开", "middle": "半开",
    }

    def __init__(self, coordinator: DataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_sunshade_status"
        try:
            self._device_name = coordinator.data.get("device_name", "吉利银河") if coordinator.data else "吉利银河"
            self._vin = coordinator.data.get("vin", "") if coordinator.data else ""
        except Exception:
            self._device_name = "吉利银河"
            self._vin = ""

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._vin or self.unique_id)},
            "name": self._device_name,
            "manufacturer": "吉利银河",
        }

    @property
    def native_value(self) -> str:
        data = self.coordinator.data if self.coordinator.data else {}
        vs = data.get("vehicle_status", {})
        val = _get_status_value(vs, (
            "sunshadeStatus", "curtainStatus", "skylightCurtainStatus",
            "sunCurtainStatus", "sunroofCurtainStatus", "shadeStatus",
        ))
        if val is None:
            return "未知"
        return self.STATUS_MAP.get(val, f"未知({val})")


# ---- 胎压（智能单位转换 + 模糊匹配兜底）----
# 银河 A7（VC 通道）实测字段命名规则（位于 vehicleTyreStatus 节点下）：
#   driver = 左，passenger = 右；不带 Rear = 前轮，带 Rear = 后轮
#     vehicleTyreStatus.tyreStatusDriver        -> 左前
#     vehicleTyreStatus.tyreStatusPassenger     -> 右前
#     vehicleTyreStatus.tyreStatusDriverRear    -> 左后
#     vehicleTyreStatus.tyreStatusPassengerRear -> 右后
# 单位已是 kPa（如 270.481 ≈ 2.70 bar），无需换算。
_TIRE_PRESSURE_FIELDS = {
    "左前胎压": (
        "tyreStatusDriver", "tireStatusDriver",
        "frontLeftTirePressure", "leftFrontTirePressure", "tirePressureFrontLeft",
        "tirePressureLeftFront", "flTirePressure", "flPressure", "tirePressureFl",
        "frontLeftTpms", "tpmsFrontLeft", "tyrePressureFrontLeft",
        "tyrePressureLeftFront",
    ),
    "右前胎压": (
        "tyreStatusPassenger", "tireStatusPassenger",
        "frontRightTirePressure", "rightFrontTirePressure", "tirePressureFrontRight",
        "tirePressureRightFront", "frTirePressure", "frPressure", "tirePressureFr",
        "frontRightTpms", "tpmsFrontRight", "tyrePressureFrontRight",
        "tyrePressureRightFront",
    ),
    "左后胎压": (
        "tyreStatusDriverRear", "tireStatusDriverRear",
        "rearLeftTirePressure", "leftRearTirePressure", "tirePressureRearLeft",
        "tirePressureLeftRear", "rlTirePressure", "rlPressure", "tirePressureRl",
        "rearLeftTpms", "tpmsRearLeft", "tyrePressureRearLeft",
        "tyrePressureLeftRear",
    ),
    "右后胎压": (
        "tyreStatusPassengerRear", "tireStatusPassengerRear",
        "rearRightTirePressure", "rightRearTirePressure", "tirePressureRearRight",
        "tirePressureRightRear", "rrTirePressure", "rrPressure", "tirePressureRr",
        "rearRightTpms", "tpmsRearRight", "tyrePressureRearRight",
        "tyrePressureRightRear",
    ),
}

# 轮胎温度字段（同上命名规则，单位 ℃）
_TIRE_TEMP_FIELDS = {
    "左前胎压": ("tyreTempDriver", "tireTempDriver", "tyreTemperatureDriver"),
    "右前胎压": ("tyreTempPassenger", "tireTempPassenger", "tyreTemperaturePassenger"),
    "左后胎压": ("tyreTempDriverRear", "tireTempDriverRear", "tyreTemperatureDriverRear"),
    "右后胎压": (
        "tyreTempPassengerRear", "tireTempPassengerRear", "tyreTemperaturePassengerRear",
    ),
}


def _collect_tire_fields(data: Any) -> dict[str, dict[str, Any]]:
    """兜底：全树收集所有tire/tyre/tpms字段"""
    found = {}

    def _walk(obj: Any, path: str = ""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                k_lower = str(k).lower()
                new_path = f"{path}.{k}" if path else str(k)
                if any(kw in k_lower for kw in ("tire", "tyre", "tpms")) and v is not None:
                    try:
                        fv = float(v)
                        found[new_path] = fv
                    except (ValueError, TypeError):
                        pass
                _walk(v, new_path)
        elif isinstance(obj, (list, tuple)):
            for i, item in enumerate(obj):
                _walk(item, f"{path}[{i}]")
    _walk(data)
    return found


# 模糊匹配关键词： (必须命中其一, 必须排除) —— 仅在精确字段名都没命中时兜底
# 注意 driver=左 / passenger=右，且 "tyreStatusDriver" 是
# "tyreStatusDriverRear" 的前缀，所以前轮必须排除含 rear 的路径。
_POSITION_KEYWORDS = {
    "左前胎压": (
        ("tyrestatusdriver", "tirestatusdriver", "frontleft", "leftfront", "fl"),
        ("rear", "back"),
    ),
    "右前胎压": (
        ("tyrestatuspassenger", "tirestatuspassenger", "frontright", "rightfront", "fr"),
        ("rear", "back"),
    ),
    "左后胎压": (
        ("tyrestatusdriverrear", "tirestatusdriverrear", "rearleft", "leftrear", "rl"),
        (),
    ),
    "右后胎压": (
        (
            "tyrestatuspassengerrear", "tirestatuspassengerrear",
            "rearright", "rightrear", "rr",
        ),
        (),
    ),
}


def _normalize_tire_kpa(raw_val: float) -> float | None:
    """把各种单位的胎压值转换成kPa"""
    if raw_val is None:
        return None
    try:
        v = float(raw_val)
    except (ValueError, TypeError):
        return None
    if v <= 0:
        return None
    # bar * 100 = kPa
    if v < 10:
        return round(v * 100, 1)
    # bar*10 存储（如2.5bar=25）
    if v < 100:
        return round(v * 10, 1)
    # 已经是kPa
    if 100 <= v <= 600:
        return round(v, 1)
    return None


class GeelyTirePressureSensorNew(CoordinatorEntity, SensorEntity):
    """胎压传感器基类"""
    _attr_native_unit_of_measurement = "kPa"
    _attr_device_class = SensorDeviceClass.PRESSURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:tire"

    def __init__(self, coordinator: DataUpdateCoordinator, entry: ConfigEntry,
                 position: str, keys: tuple[str, ...]) -> None:
        super().__init__(coordinator)
        self._position = position
        self._keys = keys
        self._attr_name = position
        self._attr_unique_id = f"{entry.entry_id}_tire_{position}"
        try:
            self._device_name = coordinator.data.get("device_name", "吉利银河") if coordinator.data else "吉利银河"
            self._vin = coordinator.data.get("vin", "") if coordinator.data else ""
        except Exception:
            self._device_name = "吉利银河"
            self._vin = ""
        self._raw_field = None
        self._raw_value = None

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._vin or self.unique_id)},
            "name": self._device_name,
            "manufacturer": "吉利银河",
        }

    def _containers(self) -> list[Any]:
        """按优先级返回待搜索的数据容器"""
        data = self.coordinator.data if self.coordinator.data else {}
        out: list[Any] = []
        vs = data.get("vehicle_status") if isinstance(data, dict) else None
        if isinstance(vs, dict):
            out.append(vs)
        out.append(data)
        return out

    def _find_tire_value(self) -> tuple[float | None, str | None, float | None]:
        # 1. 精确字段名查找（A7 走这一步：tyreStatusDriver / Passenger / *Rear）
        for container in self._containers():
            for k in self._keys:
                v = _find_value_recursive(container, (k,))
                if v is None:
                    continue
                fv = _safe_float(v)
                if fv is None:
                    continue
                kpa = _normalize_tire_kpa(fv)
                if kpa is not None:
                    return kpa, k, fv

        # 2. 兜底：全树收集 tire/tyre/tpms 字段后按位置关键词匹配
        include_kws, exclude_kws = _POSITION_KEYWORDS.get(self._position, ((), ()))
        for container in self._containers():
            all_fields = _collect_tire_fields(container)
            for path, val in all_fields.items():
                path_lower = path.lower()
                # 温度/预警字段不是胎压，排除，避免误判（如温度30被当成300kPa）
                if any(kw in path_lower for kw in ("temp", "warn", "pre")):
                    continue
                if any(kw in path_lower for kw in exclude_kws):
                    continue
                if include_kws and not any(kw in path_lower for kw in include_kws):
                    continue
                kpa = _normalize_tire_kpa(val)
                if kpa is not None:
                    return kpa, path, val
        return None, None, None

    def _find_tire_temp(self) -> float | None:
        """查找该轮位的轮胎温度（℃）"""
        for container in self._containers():
            for k in _TIRE_TEMP_FIELDS.get(self._position, ()):
                fv = _safe_float(_find_value_recursive(container, (k,)))
                if fv is not None and -50 <= fv <= 150:
                    return round(fv, 1)
        return None

    @property
    def native_value(self) -> float | None:
        kpa, field, raw = self._find_tire_value()
        self._raw_field = field
        self._raw_value = raw
        return kpa

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = {}
        if self._raw_field:
            attrs["命中字段"] = self._raw_field
        if self._raw_value is not None:
            attrs["原始值"] = self._raw_value
        temp = self._find_tire_temp()
        if temp is not None:
            attrs["轮胎温度"] = temp
        # 仅左前胎压输出所有胎压字段，方便诊断
        if self._position == "左前胎压":
            for container in self._containers():
                all_tpms = _collect_tire_fields(container)
                if all_tpms:
                    attrs["全部胎压字段"] = dict(all_tpms)
                    break
        return attrs


class GeelyTirePressureFL(GeelyTirePressureSensorNew):
    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "左前胎压", _TIRE_PRESSURE_FIELDS["左前胎压"])


class GeelyTirePressureFR(GeelyTirePressureSensorNew):
    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "右前胎压", _TIRE_PRESSURE_FIELDS["右前胎压"])


class GeelyTirePressureRL(GeelyTirePressureSensorNew):
    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "左后胎压", _TIRE_PRESSURE_FIELDS["左后胎压"])


class GeelyTirePressureRR(GeelyTirePressureSensorNew):
    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "右后胎压", _TIRE_PRESSURE_FIELDS["右后胎压"])
