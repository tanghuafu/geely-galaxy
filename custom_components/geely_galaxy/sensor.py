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
        # 新增状态（车辆/车窗/天窗/遮阳帘）
        GeelyVehicleDrivingSensor(coordinator, entry),
        GeelyWindowStatusSensor(coordinator, entry),
        GeelySunroofStatusSensor(coordinator, entry),
        GeelySunshadeStatusSensor(coordinator, entry),
        # 诊断：原始车辆状态（方便排查字段名问题）
        GeelyRawStatusSensor(coordinator, entry),
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

    # 四轮胎压（XCHANGER + VC 通道通用，自动识别字段）
    entities.extend([
        GeelyTirePressureSensor(coordinator, entry, "driver", "左前"),
        GeelyTirePressureSensor(coordinator, entry, "passenger", "右前"),
        GeelyTirePressureSensor(coordinator, entry, "driver_rear", "左后"),
        GeelyTirePressureSensor(coordinator, entry, "passenger_rear", "右后"),
    ])

    # 说明：车窗/天窗/遮阳帘状态传感器已在前面「新增状态」一段注册，
    # 这里不再重复注册（原来的重复注册会让 HA 报 unique_id 冲突，
    # 且会把实体推到另一个设备里）。

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
        # 1. XCHANGER channel (old models like L7)
        extra = self.vehicle_status.get("_xchanger_extra", {})
        key = self._POSITION_KEY_MAP.get(self._position)
        if key:
            value = extra.get(key)
            if value is not None:
                try:
                    return round(float(value), 1)
                except (ValueError, TypeError):
                    pass
        # 2. VC channel (Galaxy A7/E8)
        tyre_status = self.vehicle_status.get("vehicleTyreStatus", {})
        if tyre_status and key in tyre_status:
            value = tyre_status.get(key)
            if value is not None:
                try:
                    fv = float(value)
                    # Auto unit conversion
                    if fv < 10:      # bar -> kPa
                        fv = fv * 100
                    elif fv < 100:   # 0.1 bar unit
                        fv = fv * 10
                    return round(fv, 1)
                except (ValueError, TypeError):
                    pass
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = {}
        tyre_status = self.vehicle_status.get("vehicleTyreStatus", {})
        if key := self._POSITION_KEY_MAP.get(self._position):
            temp_key = key.replace("tyreStatus", "tyreTemp")
            if tyre_status and temp_key in tyre_status:
                try:
                    attrs["轮胎温度"] = round(float(tyre_status.get(temp_key)), 1)
                except (ValueError, TypeError):
                    pass
        return attrs


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


# ========== 新增状态传感器（v14 追加）：车辆状态 / 车窗状态 / 天窗状态 / 遮阳帘状态 ==========


class GeelyVehicleDrivingSensor(GeelyBaseSensor):
    """车辆行驶/驻车状态传感器。综合判断点火、挡位、车速。"""

    _attr_name = "车辆状态"
    _attr_icon = "mdi:car"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_vehicle_driving"

    @staticmethod
    def _to_float(v):
        if v is None: return None
        try: return float(v)
        except (TypeError, ValueError): return None

    @staticmethod
    def _to_str(v):
        return str(v) if v is not None else None

    def _deep_get(self, obj, *keys):
        for k in keys:
            if not isinstance(obj, dict): return None
            obj = obj.get(k)
        return obj

    @property
    def native_value(self) -> str | None:
        vs = self.vehicle_status
        # 尝试多路径取值
        ignition = self._to_str(self._deep_get(vs, "vehicleRunningStatus", "ignitionStatus"))
        if ignition is None:
            ignition = self._to_str(self._deep_get(vs, "additionalVehicleStatus", "engineStatus", "ignitionStatus"))
        gear = self._to_str(self._deep_get(vs, "vehicleRunningStatus", "transmissionStatus"))
        if gear is None:
            gear = self._to_str(self._deep_get(vs, "additionalVehicleStatus", "drivingSafetyStatus", "gear"))
        speed = self._to_float(self._deep_get(vs, "vehicleRunningStatus", "speed"))
        if speed is None:
            speed = self._to_float(self._deep_get(vs, "additionalVehicleStatus", "drivingSafetyStatus", "speed"))
        if speed is None:
            speed = self._to_float(vs.get("speed"))

        if ignition in ("0", "OFF", "off", None) and (speed is None or speed <= 0.5):
            return "已熄火"
        if speed is not None and speed > 1.0:
            return "行驶中"
        # 挡位判断
        if gear in ("P", "PARK", "park"):
            return "驻车(P挡)"
        if gear in ("N", "NEUTRAL"):
            return "空挡(N挡)"
        if gear in ("D", "DRIVE", "R", "REVERSE"):
            if speed is None or speed <= 0.5:
                return f"挂{gear}挡(未动)"
            return "行驶中"
        # 点火但无挡位/速度信息，保守显示怠速
        if ignition in ("1", "ON", "on", "START"):
            return "点火待命"
        return None

    @property
    def icon(self) -> str:
        v = self.native_value
        if v == "行驶中": return "mdi:car-sports"
        if v == "已熄火": return "mdi:car-off"
        if v and "P挡" in v: return "mdi:parking"
        return "mdi:car"

    @property
    def extra_state_attributes(self) -> dict:
        vs = self.vehicle_status
        return {
            "车速": self._to_float(self._deep_get(vs, "vehicleRunningStatus", "speed")),
            "挡位": self._to_str(self._deep_get(vs, "vehicleRunningStatus", "transmissionStatus")),
            "点火状态": self._to_str(self._deep_get(vs, "vehicleRunningStatus", "ignitionStatus")),
        }


def _window_scalar(value: Any) -> str | None:
    """把车窗相关取值规整成可判读的字符串；非标量（dict/list 等）返回 None。"""
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    return str(value).strip()


def _window_status_label(value_str: str | None) -> str | None:
    """把单窗/单部件状态码翻译为中文。

    吉利 VC 通道（银河A7/E5 等新车型走 /vc/app/v1/...）四门车窗状态码约定（实测于银河A7）：
        1 / ajar / 微开 / 未关严 / lightopen  -> 微开（winPos=8，车窗未完全闭合）
        2 / close / closed / 关闭 / 全关 / 已关闭 / off / false -> 全关（winPos=0）
        3 / open / 打开 / 全开 / on / true                      -> 全开
        0 作为"未上报/无效"，不做判读（返回 None）。

    注意：这里的码位含义是按银河A7 实车（winStatus/winPos 配对数据）校准的，
    与早期老 L7 通道（曾用 1=打开/2=关闭）不同；
    天窗/遮阳帘优先读 *Pos（开度百分比）而非 openStatus，此处映射仅作兜底。
    """
    if value_str is None:
        return None
    v = str(value_str).strip()
    if v == "":
        return None
    low = v.lower()
    # 英文/中文显式语义（跨通道通用）
    if low in ("open", "opened", "打开", "全开", "fullopen", "full_open", "on", "true") or "全开" in v:
        return "全开"
    if low in ("ajar", "微开", "半开", "lightopen", "light_open", "half", "未关严", "未开") or "微开" in v or "半开" in v or "未关严" in v:
        return "微开"
    if low in ("close", "closed", "关闭", "全关", "已关闭", "off", "false") or "全关" in v or "已关闭" in v:
        return "全关"
    # 数字码（VC 通道银河A7 实测：1=微开/未关严 / 2=全关 / 3=全开）
    if low == "1":
        return "微开"
    if low == "2":
        return "全关"
    if low == "3":
        return "全开"
    if low == "0":
        return None
    return f"未知({value_str})"


def _window_field_label(value: Any) -> str | None:
    """字段确已存在时的判读。

    银河A7（VC 通道）车窗关闭时不下发该字段；部分通道则下发空串，
    两种「已经给到但表示关闭」的情况统一按「全关」处理。
    """
    s = _window_scalar(value)
    if s is None:
        return None
    if s == "":
        return "全关"
    return _window_status_label(s)


def _pos_to_label(pos: Any) -> str | None:
    """按开度百分比判断状态：0=全关，≈100=全开，其余=部分打开。"""
    if pos is None:
        return None
    try:
        p = float(pos)
    except (TypeError, ValueError):
        return None
    if p <= 0.5:
        return "全关"
    if p >= 99.5:
        return "全开"
    return "部分打开"


def _winpos_label(value: Any) -> tuple[str | None, str | None]:
    """解读银河A7 VC 通道 winPos* 位置码。

    银河A7 实测字段约定（与 winStatus* 配套）：
        '0'  -> 全关到位（车窗完全关闭、贴合密封条）
        '8'  -> 未开/未关严（车窗位于关闭位但未完全闭合；常对应 winStatus='1'，微开态）
        1-7、9-99 -> 开度百分比（中间位置 = 微开/部分打开）
        '100' -> 全开到位

    返回：(展示字符串, 状态判读或 None)
        展示字符串形如 "8(未开/未关严)"、"15(开度15%)"；
        状态判读仅在能确定开/关时返回 "全关"/"微开"/"全开"，
        用于 winStatus 缺失时的兜底。
    """
    if value is None:
        return None, None
    s = str(value).strip()
    if s == "" or s.lower() in ("none", "null"):
        return None, None
    if s == "0":
        return "0(全关到位)", "全关"
    if s == "8":
        return "8(未开/未关严)", "微开"
    try:
        num = int(s)
    except (TypeError, ValueError):
        # 非数字：原样返回，不做判读
        return f"{s}(原始值)", None
    if 0 < num < 95:
        return f"{s}(开度{num}%)", "微开"
    if num >= 95:
        return f"{s}(开度{num}%)", "全开"
    # 兜底
    return f"{s}(原始值)", None


# ====== 关键字启发式：从任意嵌套层级里定位车窗/天窗/遮阳帘字段 ======

# 方位关键字：用于在字段名里识别「这是某一个窗」
_POS_KEYWORDS: dict[str, tuple[str, ...]] = {
    # 主驾（左前）：官方常见 driver/leftFront，以及各种缩写组合
    "主驾": ("driver", "leftfront", "frontleft", "flwin", "lfwin",
             "driverfront", "frontdriver", "dri", "fl", "lf",
             "winfl", "winlf", "winfrontleft", "winleftfront",
             "driverwindow", "leftfrontwindow",
             "主驾", "左前", "zj", "zjc", "zuoqian"),
    # 副驾（右前）
    "副驾": ("passenger", "rightfront", "frontright", "frwin", "rfwin",
             "passengerfront", "frontpassenger", "copilot", "pas", "fr", "rf",
             "winfr", "winrf", "winfrontright", "winrightfront",
             "passengerwindow", "rightfrontwindow",
             "副驾", "右前", "fj", "fjs", "youqian"),
    # 左后
    "左后": ("driverrear", "reardriver", "driverback", "leftrear", "rearleft",
             "rlwin", "lrwin", "rl", "lr", "drear", "drirare",
             "winrl", "winlr", "winrearleft", "winleftrear",
             "driverrearwindow", "leftrearwindow",
             "左后", "zh", "zuohou"),
    # 右后
    "右后": ("passengerrear", "rearpassenger", "passengerback", "rightrear",
             "rearright", "rrwin", "rr", "prear", "pasrear",
             "winrr", "winrearright", "winrightrear",
             "passengerrearwindow", "rightrearwindow",
             "右后", "yh", "youhou"),
}
# 全局关键字：命中这些就视为「整体车窗状态」
_OVERALL_HINTS = ("windowstatus", "windowsstatus", "windowopenstatus",
                  "windowstate", "windowopenstate", "winstatus",
                  "winopenstatus", "allwindow", "allwindowstatus",
                  "windowlightopen", "windowfullopen", "windowclose",
                  "windowsts", "windowst", "winsts", "winst")
# 排除词：命中这些就不是「窗状态」（如 sunroof/curtain/门锁/车窗升降中/位置量）
_EXCLUDE_HINTS = ("sunroof", "curtain", "sunshade", "skylight", "doorlock",
                  "lockstatus", "windowlock", "windowup", "windowdown",
                  "windowmove", "winpospct", "windowpospct",
                  "dooropen", "windowanti", "winanti",
                  # winPos* 是位置量（银河A7 实测值为 8，不是开度百分比），
                  # 不能直接当"全开/全关/微开"状态判读，单独收集到属性里。
                  "winpos", "windowpos")
# 只要字段名里含这些词，就归到「车窗候选」（不限方位，统统收集给用户 debug）
_WINDOW_HINTS = ("window", "win", "glass", "chechuang", "车窗")


def _walk(obj: Any, prefix: str = ""):
    """递归遍历 dict/list，产出 (dotted_path, scalar_value)。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, dict):
                yield from _walk(v, p)
            elif isinstance(v, list):
                for i, item in enumerate(v):
                    yield from _walk(item, f"{p}[{i}]")
            else:
                yield p, v
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            yield from _walk(item, f"{prefix}[{i}]")


def _match_position(field: str) -> str | None:
    """根据字段名判断属于哪个车窗；返回 None 表示不是分窗字段。

    匹配策略：优先匹配最长的关键字（如 passengerrear 比 passenger 长），
    避免 "windowStatusPassengerRear" 被副驾的 passenger 抢先命中。
    并且排除 sunroof/curtain/doorLock 等非车窗字段。
    """
    low = field.lower().replace("_", "").replace("-", "")
    # 先排除天窗/遮阳帘/门锁/锁车等
    for ex in _EXCLUDE_HINTS:
        if ex in low:
            return None
    # 必须含「窗」语义
    if "window" not in low and "win" not in low and "窗" not in low:
        return None
    # 收集所有命中的 (关键字, 方位) ，按关键字长度从长到短排，最长的胜
    hits: list[tuple[int, str]] = []
    for pos_name in ("主驾", "副驾", "左后", "右后"):
        for kw in _POS_KEYWORDS[pos_name]:
            if kw in low:
                hits.append((len(kw), pos_name))
    if not hits:
        return None
    hits.sort(key=lambda x: -x[0])
    return hits[0][1]


def _is_overall(field: str) -> bool:
    low = field.lower().replace("_", "").replace("-", "")
    for ex in _EXCLUDE_HINTS:
        if ex in low:
            return False
    return any(h in low for h in _OVERALL_HINTS)


def _iter_window_blocks(vehicle_status: Any):
    """保留：天窗/遮阳帘仍在这几个块里读。"""
    vs = vehicle_status if isinstance(vehicle_status, dict) else {}
    for name in ("vehicleWindowStatus", "vehicleDoorCoverStatus", "vehicleClimateStatus"):
        blk = vs.get(name)
        if isinstance(blk, dict):
            yield name, blk


class GeelyWindowStatusSensor(GeelyBaseSensor):
    """车窗整体状态（四个车窗在属性中分别显示）。

    v11 改进：不再把 ``vehicleWindowStatus`` 块存在与否作为「是否上报车窗」
    的依据——因为 VC 通道该块**始终存在**（天窗/遮阳帘就在里面），但四门
    车窗字段可能位于任意层级、甚至用不同的命名。改为**递归遍历整份车辆
    状态 JSON**，按字段名启发式定位「分窗字段」与「整体字段」，命中值即
    解析；并把所有命中字段的原始路径和取值原样暴露到属性里方便排查。

    车窗状态判读：
      - 任意一个分窗字段命中非全关值（微开/全开/未知）=> 其它未命中的窗
        视为「全关」（银河A7 / VC 通道关闭时不下发对应字段）。
      - 一个分窗字段都没命中，但命中了整体字段 => 整体字段套用到四窗。
      - 两类字段都没命中 => 显示「未知」，避免臆造。
    """

    _attr_name = "车窗状态"
    _attr_icon = "mdi:car-window"

    _WINDOWS = ("主驾", "副驾", "左后", "右后")

    # 分窗状态字段（winStatus* 等布尔/枚举型字段，主判读依据）。
    # winPos* 是车端返回的"位置量"：银河A7 VC 通道约定：
    #   '0'=全关到位, '8'=未开(默认关闭), 其余数字=开度%。
    # winPos 单独解读为友好描述放进属性，并在 winStatus 缺失时兜底判状态。
    _WINDOW_KEYS = {
        "主驾": (
            "windowStatusDriver", "winStatusDriver", "driverWindowStatus",
            "windowDriverStatus", "driverWindowOpenStatus",
            "frontLeftWindowStatus", "leftFrontWindowStatus",
            "windowStatusFrontLeft", "windowStatusLeftFront",
        ),
        "副驾": (
            "windowStatusPassenger", "winStatusPassenger", "passengerWindowStatus",
            "windowPassengerStatus", "passengerWindowOpenStatus",
            "frontRightWindowStatus", "rightFrontWindowStatus",
            "windowStatusFrontRight", "windowStatusRightFront",
        ),
        "左后": (
            "windowStatusDriverRear", "winStatusDriverRear", "driverRearWindowStatus",
            "windowDriverRearStatus", "driverRearWindowOpenStatus",
            "rearLeftWindowStatus", "leftRearWindowStatus",
            "windowStatusRearLeft", "windowStatusLeftRear",
        ),
        "右后": (
            "windowStatusPassengerRear", "winStatusPassengerRear", "passengerRearWindowStatus",
            "windowPassengerRearStatus", "passengerRearWindowOpenStatus",
            "rearRightWindowStatus", "rightRearWindowStatus",
            "windowStatusRearRight", "windowStatusRightRear",
        ),
    }

    # 分窗位置字段（原值记录，不直接判状态）—— VC 通道用 winPos*
    _WINDOW_POS_KEYS = {
        "主驾": ("windowPosDriver", "winPosDriver", "driverWindowPos",
                 "frontLeftWindowPos", "leftFrontWindowPos",
                 "windowPosFrontLeft", "windowPosLeftFront"),
        "副驾": ("windowPosPassenger", "winPosPassenger", "passengerWindowPos",
                 "frontRightWindowPos", "rightFrontWindowPos",
                 "windowPosFrontRight", "windowPosRightFront"),
        "左后": ("windowPosDriverRear", "winPosDriverRear", "driverRearWindowPos",
                 "rearLeftWindowPos", "leftRearWindowPos",
                 "windowPosRearLeft", "windowPosLeftRear"),
        "右后": ("windowPosPassengerRear", "winPosPassengerRear", "passengerRearWindowPos",
                 "rearRightWindowPos", "rightRearWindowPos",
                 "windowPosRearRight", "windowPosRightRear"),
    }

    # 整体字段的精确匹配（含 VC 通道常见枚举字段 windowLightOpen/windowFullOpen/windowClose）
    _OVERALL_KEYS = (
        "windowStatus", "windowsStatus", "windowOpenStatus", "windowState",
        "windowOpenState", "winStatus", "winOpenStatus",
        "windowLightOpen", "windowFullOpen", "windowClose",
    )

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_window_status"

    def _scan(self) -> tuple[dict[str, str], dict[str, str], dict[str, str], str | None, bool]:
        """扫描 vehicle_status，返回 (per_window_states, raw_status_map, raw_pos_map, overall_label, has_any_per_window)。

        - per_window_states: {主驾/副驾/左后/右后 -> 中文状态}
        - raw_status_map: {字段完整路径 -> 原始状态值}（状态枚举字段）
        - raw_pos_map: {方位 -> 位置码解读字符串}（winPos*，如 "8(未开/默认关闭)"）
        - raw_pos_state: {方位 -> 由 winPos 判读出的状态}（winStatus 缺失时作兜底）
        - overall_label: 整体字段解析得到的标签（可能为 None）
        """
        result: dict[str, str] = {}
        raw_status: dict[str, str] = {}
        raw_pos: dict[str, str] = {}
        raw_pos_state: dict[str, str] = {}
        overall_label: str | None = None
        has_any_per_window = False
        blocks = list(_iter_window_blocks(self.vehicle_status))

        # --- 1) 精确匹配：遍历已知块里的候选字段 ---
        for _bname, block in blocks:
            for w, keys in self._WINDOW_KEYS.items():
                if w in result:
                    continue
                for k in keys:
                    if k in block:
                        label = _window_field_label(block[k])
                        raw_status[k] = str(block[k])
                        if label is not None:
                            result[w] = label
                            has_any_per_window = True
                        break
            for w, keys in self._WINDOW_POS_KEYS.items():
                if w in raw_pos:
                    continue
                for k in keys:
                    if k in block:
                        disp, state_from_pos = _winpos_label(block[k])
                        if disp:
                            raw_pos[w] = disp
                            if state_from_pos:
                                raw_pos_state[w] = state_from_pos
                                # 精确匹配命中 winPos 且能判读出状态时，
                                # 只要该方位还没有 winStatus 判读结果，就直接用上。
                                # 这避免了「winPos=0 全关到位，但 winStatus 不下发」
                                # 时 result 为空、后续补全逻辑根本不跑的问题
                                # （之前 has_any_per_window 只在 winStatus 分支被置 True）。
                                if w not in result:
                                    result[w] = state_from_pos
                                    has_any_per_window = True
                        break
            if overall_label is None:
                for k in self._OVERALL_KEYS:
                    if k in block:
                        raw_status[k] = str(block[k])
                        overall_label = _window_field_label(block[k])
                        break

        # --- 2) 启发式递归：覆盖未知字段名/未知层级 ---
        for path, value in _walk(self.vehicle_status):
            leaf = path.split(".")[-1].split("[")[0]
            low_leaf = leaf.lower().replace("_", "").replace("-", "")

            # 注意：必须先判断 winPos*/windowPos*（位置量）再判断普通 status 字段。
            # 因为 winPosDriver / winPosPassenger 这类字段名本身就包含 driver/passenger
            # 关键字，若先跑 _match_position 会被当成 winStatus 字段截走，
            # 调用 _window_field_label('0') 返回 None，result 不会被填入，
            # 并且 continue 后就跳过了下面的 winPos 分支——
            # 这就是「winPos=0 全关到位却四个窗都显示未知」的直接原因。
            if low_leaf.startswith(("winpos", "windowpos")) and "sunroof" not in low_leaf and "curtain" not in low_leaf:
                # 从字段名里解析方位（最长关键字优先）
                hits: list[tuple[int, str]] = []
                for pname in self._WINDOWS:
                    if pname in raw_pos:
                        continue
                    for kw in _POS_KEYWORDS[pname]:
                        if kw in low_leaf:
                            hits.append((len(kw), pname))
                if hits:
                    hits.sort(key=lambda x: -x[0])
                    pname = hits[0][1]
                    disp, state_from_pos = _winpos_label(value)
                    if disp:
                        raw_pos[pname] = disp
                        if state_from_pos and pname not in result:
                            # winStatus 未命中时，用 winPos 兜底
                            result[pname] = state_from_pos
                            has_any_per_window = True
                            raw_pos_state[pname] = state_from_pos
                continue

            # winStatus* / windowStatus* 等分窗状态字段（非位置量）
            pos_name = _match_position(leaf)
            if pos_name is not None:
                raw_status[path] = str(value)
                label = _window_field_label(value)
                if label is not None and pos_name not in result:
                    result[pos_name] = label
                    has_any_per_window = True
                continue

            if overall_label is None and _is_overall(leaf):
                raw_status[path] = str(value)
                overall_label = _window_field_label(value)

        return result, raw_status, raw_pos, raw_pos_state, overall_label, has_any_per_window

    def _each_window(self) -> tuple[dict[str, str], dict[str, str], dict[str, str], str | None]:
        scan_result = self._scan()
        per_window, raw_status, raw_pos, raw_pos_state, overall, has_per = scan_result
        result = dict(per_window)

        # 判断「车端正在上报车窗系统」的依据（VC 通道）：
        #   1) 命中了至少一个分窗状态字段（winStatus* 或 winPos* 兜底），或
        #   2) 命中了整体字段。
        #
        # 实测（银河A7）：
        #   - 四窗微开/未关严时：winStatus*='1'、winPos*='8'，四个字段都会下发
        #   - 四窗全关到位时：winStatus*='2'（或字段切换）、winPos*='0'
        #   - 单个窗开启：对应窗 winStatus 变为 3（全开）或 1（微开）、winPos 为具体开度
        # 因此精确字段命中即判读；winStatus 与 winPos 共同参与判读。
        vs = self.vehicle_status if isinstance(self.vehicle_status, dict) else {}

        if has_per:
            # 命中分窗字段：未命中的窗若有 winPos 兜底状态则用之；
            # 若整体字段有值则套整体；否则保留（车端没报）
            for w in self._WINDOWS:
                if w not in result:
                    if w in raw_pos_state:
                        result[w] = raw_pos_state[w]
                    elif overall:
                        result[w] = overall
        elif overall:
            for w in self._WINDOWS:
                result[w] = overall

        return result, raw_status, raw_pos, overall

    @property
    def native_value(self) -> str | None:
        ws, _rs, _rp, _ov = self._each_window()
        if not ws:
            return "未知"
        states = set(ws.values())
        if states == {"全关"}:
            return "全关"
        if states == {"全开"}:
            return "全开"
        if states == {"微开"}:
            return "微开"
        # 混合状态：只要有全开就"部分打开"
        if "全开" in states and len(states) > 1:
            return "部分打开"
        if "微开" in states:
            return "微开"
        # 若存在未知(...)取值，保留「部分打开」并在属性里展示原始值
        return "部分打开"

    @property
    def icon(self) -> str:
        v = self.native_value
        if v == "全关":
            return "mdi:car-window-closed"
        if v == "全开":
            return "mdi:car-window-down"
        if v == "微开":
            return "mdi:car-window-arrows"
        return "mdi:car-window"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        per_window, raw_status, raw_pos, overall = self._each_window()
        attrs: dict[str, Any] = {}
        # 四窗最终判读
        for w in self._WINDOWS:
            attrs[w] = per_window.get(w, "未知")
        # 各窗位置量（winPos* 解读后：0=全关到位, 8=未开/未关严, 数字=开度%）
        if raw_pos:
            pos_attrs = {}
            for w in self._WINDOWS:
                if w in raw_pos:
                    pos_attrs[w] = raw_pos[w]
            if pos_attrs:
                attrs["位置量"] = pos_attrs
        # 整体字段（如有）
        if overall:
            attrs["整体状态_raw"] = overall
        # 排障：所有命中的状态字段路径 + 原始值
        if raw_status:
            attrs["_命中状态字段"] = raw_status
        # 排障：原样暴露常见顶层块
        vs = self.vehicle_status
        for name in ("vehicleWindowStatus", "vehicleDoorCoverStatus",
                     "vehicleRunningStatus", "vehicleClimateStatus",
                     "vehicleBatteryStatus", "vehicleEngineStatus",
                     "additionalVehicleStatus", "_xchanger_extra"):
            blk = vs.get(name) if isinstance(vs, dict) else None
            if isinstance(blk, dict) and blk:
                attrs[f"_raw_{name}"] = blk
        # 排障：递归扫整份 JSON，把所有含 window/win/glass/车窗 关键词
        # 的字段全部列出（不管方位是否匹配），真实字段名就在这里。
        debug_win: dict[str, Any] = {}
        if isinstance(vs, dict):
            for path, value in _walk(vs):
                leaf = path.split(".")[-1].lower().replace("_", "").replace("-", "")
                if any(h in leaf for h in _WINDOW_HINTS):
                    if not any(ex in leaf for ex in _EXCLUDE_HINTS):
                        debug_win[path] = value
        if debug_win:
            attrs["_debug_all_window_fields"] = debug_win
        return attrs


class GeelySunroofStatusSensor(GeelyBaseSensor):
    """天窗状态（全关 / 部分打开 / 全开）。

    银河A7（VC 通道）读数来自 ``vehicleWindowStatus.sunroofPos``（开度 %），
    其次看 ``vehicleWindowStatus.sunroofOpenStatus``；
    ``vehicleDoorCoverStatus.sunroofStatus`` 仅作老车型兜底。
    """

    _attr_name = "天窗状态"
    _attr_icon = "mdi:roof"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_sunroof_status"

    def _find_by_keys(self, keys: tuple[str, ...]) -> tuple[Any, Any, str | None]:
        """在 vehicle_status 整棵树里找第一个含指定字段名的位置，返回 (pos, open_status_raw, 所在路径)。"""
        pos_val: Any = None
        open_val: Any = None
        pos_path: str | None = None
        open_path: str | None = None
        pos_key = keys[0]
        open_key = keys[1]
        for path, v in _walk(self.vehicle_status):
            leaf = path.split(".")[-1].split("[")[0]
            if leaf == pos_key and pos_val is None:
                pos_val = v
                pos_path = path
            if leaf == open_key and open_val is None:
                open_val = v
                open_path = path
            if pos_val is not None and open_val is not None:
                break
        return pos_val, open_val, pos_path or open_path

    def _raw_pair(self) -> tuple[Any, Any]:
        """返回 (开度, 开关状态原始值)。"""
        vs = self.vehicle_status
        # 先精确找 vehicleWindowStatus
        vws = vs.get("vehicleWindowStatus") if isinstance(vs, dict) else None
        if isinstance(vws, dict):
            pos = vws.get("sunroofPos")
            raw = vws.get("sunroofOpenStatus")
            if pos is not None or raw is not None:
                return pos, raw
        dc = vs.get("vehicleDoorCoverStatus") if isinstance(vs, dict) else None
        if isinstance(dc, dict):
            for k in ("sunroofStatus", "sunroofOpenStatus", "sunroofOpen"):
                if dc.get(k) is not None:
                    return None, dc.get(k)
        # 兜底：递归搜 sunroofPos / sunroofOpenStatus
        p, o, _p = self._find_by_keys(("sunroofPos", "sunroofOpenStatus"))
        if p is not None or o is not None:
            return p, o
        return None, None

    @property
    def native_value(self) -> str | None:
        pos, raw = self._raw_pair()
        label = _pos_to_label(pos)
        if label is not None:
            return label
        return _window_status_label(raw)

    @property
    def icon(self) -> str:
        v = self.native_value
        if v == "全关":
            return "mdi:roof"
        if v == "全开":
            return "mdi:car-windshield-outline"
        return "mdi:window-maximize"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        pos, raw = self._raw_pair()
        return {"开度": pos, "open_status_raw": raw}


class GeelySunshadeStatusSensor(GeelyBaseSensor):
    """遮阳帘状态（全关 / 部分打开 / 全开）。

    银河A7（VC 通道）读数来自 ``vehicleWindowStatus.curtainPos``（开度 %），
    其次看 ``vehicleWindowStatus.curtainOpenStatus``；
    ``vehicleDoorCoverStatus.sunshadeStatus`` 仅作老车型兜底。
    """

    _attr_name = "遮阳帘状态"
    _attr_icon = "mdi:blinds"

    _FALLBACK_KEYS = (
        "sunshadeStatus", "skylightCurtainStatus", "sunCurtainStatus", "curtainStatus",
    )

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_sunshade_status"

    def _raw_pair(self) -> tuple[Any, Any]:
        """返回 (开度, 开关状态原始值)。"""
        vs = self.vehicle_status
        vws = vs.get("vehicleWindowStatus") if isinstance(vs, dict) else None
        if isinstance(vws, dict):
            pos = vws.get("curtainPos")
            raw = vws.get("curtainOpenStatus")
            if pos is not None or raw is not None:
                return pos, raw
        dc = vs.get("vehicleDoorCoverStatus") if isinstance(vs, dict) else None
        if isinstance(dc, dict):
            for k in self._FALLBACK_KEYS:
                if dc.get(k) is not None:
                    return None, dc.get(k)
        # 兜底：递归搜
        p, o, _p = self._find_by_keys(("curtainPos", "curtainOpenStatus"))
        if p is not None or o is not None:
            return p, o
        return None, None

    @property
    def native_value(self) -> str | None:
        pos, raw = self._raw_pair()
        label = _pos_to_label(pos)
        if label is not None:
            return label
        return _window_status_label(raw)

    @property
    def icon(self) -> str:
        v = self.native_value
        if v == "全关":
            return "mdi:blinds"
        if v == "全开":
            return "mdi:blinds-open"
        return "mdi:blinds-horizontal"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        pos, raw = self._raw_pair()
        return {"开度": pos, "open_status_raw": raw}



import json as _json


class GeelyRawStatusSensor(GeelyBaseSensor):
    """诊断用：把车端返回的完整 vehicle_status JSON 暴露到实体属性。

    实体 state 固定为 "raw"，所有真实字段都在属性里，按顶层块拆开；
    另外附一个 _all_keys（点路径 -> 原始值）列表，方便 Ctrl+F 搜字段名。
    这个传感器仅作排障用，不需要可长期禁用或隐藏。
    """

    _attr_name = "车辆原始数据"
    _attr_icon = "mdi:code-braces"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_raw_status"

    @property
    def native_value(self) -> str:
        vs = self.vehicle_status
        if not isinstance(vs, dict) or not vs:
            return "无数据"
        keys = [k for k in vs.keys() if isinstance(vs[k], (dict, list))]
        return f"{len(keys)} 个顶级块"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        vs = self.vehicle_status if isinstance(self.vehicle_status, dict) else {}
        attrs: dict[str, Any] = {}
        # 顶层块原样列出
        for k, v in vs.items():
            if k.startswith("_"):
                continue
            try:
                # 防止不可序列化对象，直接 repr
                _json.dumps(v, ensure_ascii=False, default=str)
                attrs[k] = v
            except Exception:
                attrs[k] = repr(v)
        # 列出所有点路径 -> 值（便于搜索）
        all_leaves: dict[str, Any] = {}
        for path, value in _walk(vs):
            if not path.startswith("_"):
                all_leaves[path] = value
        attrs["_all_keys_count"] = len(all_leaves)
        # 只放前 400 项避免属性过大
        items = list(all_leaves.items())[:400]
        attrs["_all_keys"] = dict(items)
        if len(all_leaves) > 400:
            attrs["_all_keys_truncated"] = True
        return attrs
