# Geely Galaxy 吉利银河 Home Assistant 集成（银河A7适配版 v8）

本版本基于 lyj5812/geely-galaxy 原版，针对银河 A7 插混做了以下增强与修复。

## v8 更新（本次修复「关闭极速降温后座椅通风仍在转」）

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
- 空调状态、充电状态、驻车图片等

### 定位 (device_tracker)
- 车辆位置（GCJ-02→WGS84 纠偏，可直接在 HA 地图显示、区域触发自动化）

## 部署方法

1. 备份 `/config/custom_components/geely_galaxy/`；
2. 用本目录下全部文件覆盖原目录；
3. 删除 `/config/custom_components/geely_galaxy/__pycache__/`；
4. **完全重启 Home Assistant**（不能只重载集成，因包含 device_tracker 平台变更）。

本次只改了 `api.py`、`button.py`、`README.md`（`__init__.py` 无变化），如已装 v7，覆盖后重启即可，不需要重新配置。

## 车机端必要设置

- **位置共享**：设置 → 系统 → 隐私与权限 → 车辆位置信息共享 → 开（授权 360 天）。
