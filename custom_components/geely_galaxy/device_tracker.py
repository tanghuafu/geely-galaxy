"""支持车辆定位追踪的 device_tracker 平台。

车辆定位信息来自接口返回的位置字段，支持两种结构：
1. VC 通道（galaxy-vc）：vehicleLocationStatus = { longitude, latitude, altitude, ... }
2. XCHANGER 通道：basicVehicleStatus.position = { latitude, longitude, altitude, marsCoordinates, ... }

经纬度原始单位为「毫秒（秒 × 1000）」，需除以 3_600_000 得到标准度数（WGS-84）。

中国大陆返回的坐标是 GCJ-02（火星坐标系，相对于真实 WGS-84 有 100~700 米偏移），
若返回中 marsCoordinates=true（或字段缺失，中国大陆车辆默认），会自动转换为 WGS-84，
使车在 HA 地图上显示的位置与实际相符。

参考 geely-galaxy-assistant/jlyh.js 坐标换算，以及 kooritea/geely_galaxy_ha 的 GCJ-02→WGS-84 实现。
"""
from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, Any

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

if TYPE_CHECKING:
    from . import GeelyDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

# 经纬度单位转换：原始值为「毫秒（秒 × 1000）」，除以 3_600_000 得到标准度数
LOCATION_DIVISOR = 3_600_000

# ------------------------- 定位字段查找 -------------------------
# 定位块候选路径（按优先级）：VC 通道的扁平块 / XCHANGER 通道的嵌套块
LOCATION_BLOCK_PATHS = (
    ("vehicleLocationStatus",),
    ("vehicleLocation",),
    ("locationStatus",),
    ("basicVehicleStatus", "position"),
)
LATITUDE_KEYS = ("latitude", "lat")
LONGITUDE_KEYS = ("longitude", "lon", "lng")
ALTITUDE_KEYS = ("altitude", "alt")
HEADING_KEYS = ("heading", "direction", "course")
SPEED_KEYS = ("speed", "vehicleSpeed")
MARS_KEYS = ("marsCoordinates", "mars_coordinates", "gcj02", "isGcj02")


def _walk(obj: Any, path: tuple[str, ...]) -> Any:
    """按路径逐层获取嵌套字段。"""
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        # 大小写不敏感
        if key in cur:
            cur = cur[key]
        else:
            lower = {k.lower(): v for k, v in cur.items()}
            if key.lower() in lower:
                cur = lower[key.lower()]
            else:
                return None
    return cur


def _find_field(obj: Any, keys: tuple[str, ...]) -> Any:
    """在 dict 中按候选键名查找字段；找不到返回 None（大小写不敏感）。"""
    if not isinstance(obj, dict):
        return None
    for k in keys:
        if k in obj and obj[k] not in (None, ""):
            return obj[k]
    lower = {kk.lower(): vv for kk, vv in obj.items()}
    for k in keys:
        if k.lower() in lower and lower[k.lower()] not in (None, ""):
            return lower[k.lower()]
    return None


def _to_float(v: Any, divisor: float = 1.0) -> float | None:
    """把原始值转 float；非数字返回 None。支持"毫秒"原始值。"""
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f / divisor if divisor != 1 else f


# ------------------------- GCJ-02 → WGS-84 坐标转换 -------------------------
# 中国地图使用 GCJ-02（火星坐标系），Home Assistant 地图使用 WGS-84。
# 不转换会导致在 HA 地图上出现 100~700 米偏移。
_GCJ_A = 6378245.0  # 长半轴
_GCJ_EE = 0.006693421622965943  # 偏心率平方


def _out_of_china(lat: float, lng: float) -> bool:
    """是否在中国境外（境外无需偏移转换）。"""
    return not (72.004 <= lng <= 137.8347 and 0.8293 <= lat <= 55.8271)


def _transform_lat(x: float, y: float) -> float:
    ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * math.pi) + 320.0 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lng(x: float, y: float) -> float:
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def gcj02_to_wgs84(lat: float, lng: float) -> tuple[float, float]:
    """GCJ-02（火星）坐标 → WGS-84。"""
    if _out_of_china(lat, lng):
        return lat, lng
    dlat = _transform_lat(lng - 105.0, lat - 35.0)
    dlng = _transform_lng(lng - 105.0, lat - 35.0)
    rad_lat = lat / 180.0 * math.pi
    magic = math.sin(rad_lat)
    magic = 1 - _GCJ_EE * magic * magic
    sqrt_magic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_GCJ_A * (1 - _GCJ_EE)) / (magic * sqrt_magic) * math.pi)
    dlng = (dlng * 180.0) / (_GCJ_A / sqrt_magic * math.cos(rad_lat) * math.pi)
    return round(lat - dlat, 6), round(lng - dlng, 6)


def _extract_location(vehicle_status: Any) -> dict[str, Any] | None:
    """从整段 status 里提取定位字典（含坐标系统）。返回值含 latitude/longitude(度) 以及可能的 altitude/heading/speed/is_gcj02。"""
    if not isinstance(vehicle_status, dict):
        return None

    # 1) 从候选路径找定位块
    loc: dict | None = None
    for path in LOCATION_BLOCK_PATHS:
        candidate = _walk(vehicle_status, path)
        if isinstance(candidate, dict):
            loc = candidate
            break

    # 2) 兜底：在顶层直接找经纬度
    if loc is None:
        top_lat = _find_field(vehicle_status, LATITUDE_KEYS)
        top_lon = _find_field(vehicle_status, LONGITUDE_KEYS)
        if top_lat is None and top_lon is None:
            return None
        loc = vehicle_status

    lat_raw = _find_field(loc, LATITUDE_KEYS)
    lon_raw = _find_field(loc, LONGITUDE_KEYS)
    lat = _to_float(lat_raw, LOCATION_DIVISOR)
    lon = _to_float(lon_raw, LOCATION_DIVISOR)
    if lat is None or lon is None:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        _LOGGER.debug("车辆定位经纬度异常，跳过: lat=%s lon=%s", lat, lon)
        return None

    # 3) 判断是否是 GCJ-02（火星）坐标
    mars_raw = _find_field(loc, MARS_KEYS)
    if mars_raw is None:
        # 字段缺失：中国大陆车辆默认按 GCJ-02 处理（安全默认值，避免 100~700 米偏移）
        is_gcj02 = not _out_of_china(lat, lon)
    else:
        if isinstance(mars_raw, bool):
            is_gcj02 = mars_raw
        else:
            is_gcj02 = str(mars_raw).strip().lower() in ("true", "1", "yes")

    # 4) GCJ-02 → WGS-84
    if is_gcj02:
        lat, lon = gcj02_to_wgs84(lat, lon)

    result: dict[str, Any] = {"latitude": lat, "longitude": lon, "is_gcj02": is_gcj02}
    alt = _to_float(_find_field(loc, ALTITUDE_KEYS))
    if alt is not None:
        result["altitude"] = alt
    heading = _to_float(_find_field(loc, HEADING_KEYS))
    if heading is not None:
        result["heading"] = heading
    speed = _to_float(_find_field(loc, SPEED_KEYS))
    if speed is not None:
        result["speed"] = speed
    return result


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Geely Galaxy device tracker."""
    coordinator: GeelyDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    api = hass.data[DOMAIN][entry.entry_id]["api"]
    async_add_entities([GeelyVehicleTracker(coordinator, api)], True)


class GeelyVehicleTracker(CoordinatorEntity, TrackerEntity):
    """代表车辆当前位置的 device_tracker。"""

    _attr_has_entity_name = True
    _attr_translation_key = "vehicle_location"
    _attr_name = "车辆位置"
    _attr_icon = "mdi:map-marker"
    _attr_source_type = SourceType.GPS

    def __init__(self, coordinator: GeelyDataUpdateCoordinator, api) -> None:
        """Initialize the tracker."""
        super().__init__(coordinator)
        self._api = api
        device_sn = getattr(api, "device_sn", None) or "geely_galaxy"
        self._attr_unique_id = f"{device_sn}_vehicle_location"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device_sn)},
        }

    @property
    def _vehicle_status(self) -> dict[str, Any] | None:
        data = self.coordinator.data
        if not isinstance(data, dict):
            return None
        vs = data.get("vehicle_status")
        return vs if isinstance(vs, dict) else None

    @property
    def _location(self) -> dict[str, Any] | None:
        return _extract_location(self._vehicle_status)

    @property
    def latitude(self) -> float | None:
        loc = self._location
        return loc["latitude"] if loc else None

    @property
    def longitude(self) -> float | None:
        loc = self._location
        return loc["longitude"] if loc else None

    @property
    def location_accuracy(self) -> float:
        return 50.0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {}
        loc = self._location
        if loc:
            if "altitude" in loc:
                attrs["altitude"] = round(loc["altitude"], 1)
            if "heading" in loc:
                attrs["heading"] = round(loc["heading"], 1)
            if "speed" in loc:
                attrs["speed"] = round(loc["speed"], 1)
            attrs["coordinate_system"] = "GCJ-02 → WGS-84" if loc.get("is_gcj02") else "WGS-84"
        # 原始值（用于排障）
        vs = self._vehicle_status
        if isinstance(vs, dict):
            for path in LOCATION_BLOCK_PATHS:
                raw = _walk(vs, path)
                if isinstance(raw, dict):
                    attrs["raw_location_path"] = ".".join(path)
                    attrs["raw_location"] = raw
                    break
        return attrs

    @property
    def available(self) -> bool:
        return super().available and self._location is not None
