# Geely Galaxy 吉利银河 Home Assistant 集成（银河A7适配版 v9）

本版本基于 lyj5812/geely-galaxy 原版，针对银河 A7 插混做了以下增强与修复。

## v9 更新（本次修复「实体散落到多个设备 + 天窗/车窗状态读数错误」）

**问题现象**：

1. 集成里除了车型设备「银河A7」，还多出几个名为「吉利银河」的设备（车辆状态、车窗状态、天窗状态、遮阳帘状态），
   以及一个**未命名设备**（车辆位置）；
2. 天窗、车窗状态传感器读数不对（银河A7 走 VC 通道）。

**根因（已在代码中确认）**：

| 问题 | 根因 |
| --- | --- |
| 多出「吉利银河」设备 | `sensor.py` 里 `GeelyWindowStatusSensor` / `GeelySunroofStatusSensor` / `GeelySunshadeStatusSensor` / `GeelyVehicleStateSensor` 被**定义了两次**，文件末尾那版（`CoordinatorEntity` 版）覆盖了前面的 `GeelyBaseSensor` 版；它的设备标识写成 `coordinator.data.get("vin")`，但协调器数据里**没有顶层 `vin`**，于是退化成 `(DOMAIN, 该实体的 unique_id)` —— **每个实体各自变成一个名为「吉利银河」的设备** |
| 未命名设备 | `device_tracker.py` 的 `_attr_device_info` 只写了 `identifiers`、**没写 `name`**，HA 只能登记成「未命名设备」 |
| 天窗/遮阳帘读数错 | 两个传感器读的是 `vehicleDoorCoverStatus.sunroofStatus / sunshadeStatus`，而银河A7（VC 通道）实际把天窗/遮阳帘放在 **`vehicleWindowStatus`** 块：`sunroofPos` / `sunroofOpenStatus` / `curtainPos` / `curtainOpenStatus` |
| 重复注册 | 车窗/天窗/遮阳帘在 `async_setup_entry` 里被 `entities.extend` 了**两次**，同一个 `unique_id` 会冲突 |

**修复方式**（只动 `sensor.py`、`device_tracker.py`，**开关逻辑一律未改**）：

1. 删掉文件末尾重复定义的那一整段（含 `_find_value_recursive` 等只被它用到的辅助函数），
   并删掉 `async_setup_entry` 里的重复注册块 —— 现在每个实体只定义、只注册一次；
2. 车窗/天窗/遮阳帘三个状态传感器统一继承 `GeelyBaseSensor`，因此**自动与车型设备 `(DOMAIN, vin)` 合并**；
3. 天窗、遮阳帘改为优先读 **`vehicleWindowStatus`**（`sunroofPos` / `curtainPos` 开度百分比判状态），
   `sunroofOpenStatus` / `curtainOpenStatus` 作次选，`vehicleDoorCoverStatus.sunroofStatus / sunshadeStatus` 仅作老车型兜底；
4. 车窗状态改为在 `vehicleWindowStatus`（其次 `vehicleDoorCoverStatus`、XCHANGER 的 `additionalVehicleStatus.*`）里按
   多候选字段名查找四门车窗；**车端如果不上报车窗开合，就显示「未知」，不再臆造数值**，
   并把原始块放进实体属性 `_raw_vehicleWindowStatus` 便于核对；
5. `device_tracker` 的 `device_info` 改成与传感器/开关/按钮**完全一致**（`identifiers={(DOMAIN, vin)}`、`name=车型`），
   车辆位置并入车型设备；`unique_id` 保持旧值不变，升级后不会产生孤儿实体。

**⚠️ 需要你实机复核的一点（唯一不确定项）**：

银河A7 的 VC 状态下，**天窗/遮阳帘确定在 `vehicleWindowStatus`**（已对齐另一维护分支 Eternity-h/geely-galaxy 的写法）；
但**四门车窗是否上报开合状态、字段叫什么，取决于车型**。请打开「车窗状态」实体看它的属性：

- 若属性里出现了四个车窗（主驾/副驾/左后/右后）→ 说明字段名已命中，正常；
- 若只有 `_raw_vehicleWindowStatus` 一项、状态显示「未知」→ 说明你的车端确实不上报车窗开合，
  把 `_raw_vehicleWindowStatus` 的原始内容贴给我，我按真实字段名补进候选列表即可（改动只在 `sensor.py` 一处）。

## v8 更新（修复「关闭极速降温后座椅通风仍在转」）

**问题现象**：点「关闭极速降温」后，空调关了，但**座椅通风仍然在转**。

**根因（已在代码中确认）**：

极速降温走的是一条**独立接口** `/vc/app/v1/vehicle/control/temperature`，这条指令一次性带了两个通道：

| 通道 | 报文里的字段 | 极速降温开启时 |
| --- | --- | --- |
| 预空调（制冷） | `paaAc` / `paaAcTemp` | `true` / `15.5` |
| **座椅通风** | `paaVentilation` / `paaVentilationValue` / `paaVlt` / `paaVltDuration` | `[1,2]` / `3` / `true` / `60` |

而 v7 的「关闭极速降温」是直接去调 `/vc/app/v1/vehicle/control/climate`（`conditioner=1, type=2`）**关空调**——这条报文里根本没有座椅通风字段，所以它只能关空调，**关不掉座椅通风**；座椅通风会一直转到 `paaVltDuration=60` 分钟到期才自动停。

**修复方式**：

1. 「关闭极速降温」现在先调 `/control/temperature`，把座椅通风字段清零（`paaVentilation=[1,2]`、`paaVentilationValue=0`、`paaVlt=false`、`paaVltDuration=0`、同时 `paaAc=false` 关预空调），**再**调「关空调」兜底；
2. 同理修复「关闭极速升温」→ 关闭后同时把**座椅加热**清零（`paaHeat=[1,2]`、`paaHeatValue=0`）；
3. 新增 4 个独立按钮，可不影响空调单独控制座椅：**座椅通风开 / 座椅通风关 / 座椅加热开 / 座椅加热关**。以后只想关座椅通风，点「座椅通风关」即可。

**⚠️ 需要你抓包复核的一点（唯一的不确定项）**：

「极速降温关闭」之前没有可参考的真实报文（青龙脚本 jylh.js 只有开启、没有关闭，之前的"关空调"是推断出来的，结果就是本次这个 bug）。上面"把档位设为 0 = 关闭"是**按开启报文反推**的最合理写法，请你实机验证一次：

- 若点「关闭极速降温 / 座椅通风关」后座椅通风停止 → 说明反推正确，收工；
- 若车机报 `B00005 参数校验失败`（说明后端不认「档位 0」这个取值），请到官方 APP 里手动关闭一次座椅通风并抓包，把 `/vc/app/v1/vehicle/control/*` 的 **URL + method + 请求体**发我，我把 `control_temperature_off` / `control_seat_ventilation` 的关闭参数按真实报文替换即可（改动只在 api.py 一处）。

## v7 更新（修复空调 / 极速升降温）

- ❌ **极速升/降温之前失效的根本原因**：之前错误地走了 `/control/climate` 接口（conditioner=3/4, temp="LO"/"HI"）——凭空猜的参数，真实接口根本不是这样。
- ✅ **极速升/降温走独立接口**：与青龙脚本 jlyh.js 完全一致，改为 `/vc/app/v1/vehicle/control/temperature`：
  - 请求体带完整的 `paa*` 字段（paaAc、paaAcTemp、paaHeat、paaHeatValue、paaVentilation、paaVentilationValue、paaVlt、paaVltDuration 等）；
  - 极速降温：`type=3`，`temp=15.5`（数字！），`paaVentilation=[1,2]`，`paaVentilationValue=3`，`paaVlt=true`；
  - 极速升温：`type=3`，`temp=28.5`（数字！），`paaHeat=[1,2]`，`paaHeatValue=3`，`paaVlt=false`；
  - duration=90（分钟自动结束）；
  - ~~关闭极速 → 直接调用"关空调"~~ → **此写法有缺陷，v8 已改为同时关闭座椅通风/加热**。
- ✅ **普通空调温度默认值**：与青龙脚本保持一致，默认 25°C，不再用 `int(round())` 截断，保持字符串形式。
- ✅ **除霜不受影响**：仍然走 `/control/climate`，conditioner=2，temp="24"（与原脚本一致）。

## 其他已修复的功能（v5-v6 已加入）

- 控制接口参数错误导致全部车控报 `B00005 参数校验失败`：全部按 jlyh.js 修正（哨兵、车锁、空调、除霜、寻车、车窗、净化）；
- 新增天窗开关、遮阳帘开关（走 `/control/window`，position=2/3）；
- 新增车窗全开按钮（position=[1], type=1）；
- 新增油量/油量续航/综合续航传感器（插混车型）；
- 新增 device_tracker「车辆位置」，支持 GCJ-02→WGS84 坐标纠偏；
- 修复电量 `"80.0"` 转 int 崩溃；
- 修复验证码视图路由重复注册报错；
- 后备箱状态映射修正（'1'=解锁、'2'=锁定），**暂不提供远程开启按钮**——真实尾门控制接口的请求四要素（URL/method/body/headers）还不全，待补全后可接入。

## 支持的实体

### 开关 (switch)
- 哨兵模式
- 天窗
- 遮阳帘

### 按钮 (button)
- 解锁、锁车
- 寻车（鸣笛闪灯）
- 空调开、空调关
- 除霜开、除霜关
- 空气净化开、空气净化关
- 车窗全开、车窗微开、关闭车窗
- **极速降温**、**关闭极速降温**（关闭时会连带关闭座椅通风）
- **极速升温**、**关闭极速升温**（关闭时会连带关闭座椅加热）
- **座椅通风开 / 座椅通风关**、**座椅加热开 / 座椅加热关**（独立控制，不影响空调）
- 每日签到

### 传感器 (sensor)
- 总里程、剩余续航（电）、车内温度、车外温度、PM2.5、平均能耗
- 剩余电量、电池电量（%）
- **油量（%）、油量续航（km）、综合续航（km）**（插混车型）
- 车门锁状态
- **后备箱状态**（已锁定 / 已解锁）
- **车辆状态**（已熄火 / 驻车 / 空挡 / 行驶中）
- **车窗状态、天窗状态、遮阳帘状态**（银河A7 走 VC 通道，读 `vehicleWindowStatus`；开度放在属性里）
- 空调状态、充电状态、驻车图片等

### 定位 (device_tracker)
- 车辆位置（GCJ-02→WGS84 纠偏，可直接在 HA 地图显示、区域触发自动化）

## 部署方法

1. 备份 `/config/custom_components/geely_galaxy/`；
2. 用本目录下全部文件覆盖原目录；
3. 删除 `/config/custom_components/geely_galaxy/__pycache__/`；
4. **完全重启 Home Assistant**（不能只重载集成，因包含 device_tracker 平台变更）。

本次（v9）只改了 `sensor.py`、`device_tracker.py`、`README.md`（`api.py` / `button.py` / `switch.py` / `__init__.py` 均无变化），如已装 v8，覆盖后重启即可，不需要重新配置。

## 车机端必要设置

- **位置共享**：设置 → 系统 → 隐私与权限 → 车辆位置信息共享 → 开（授权 360 天）。
