# Geely Galaxy 吉利银河 Home Assistant 集成（银河A7实测校准版 v16）

本版本基于 lyj5812/geely-galaxy 原版，针对银河 A7 VC 通道做了实车真值校准。

## v16 更新（修复：winPos=0 全关到位时四窗显示「未知」的 bug）

**根因**：v15 里有两个代码级 bug 叠加，导致「winPos=0 全关到位、winStatus 不下发」时四窗都显示未知：
1. 启发式递归扫描中，**分支顺序写反**了——先跑「分窗状态字段」匹配，再跑「winPos 位置字段」匹配。但 `winPosDriver / winPosPassenger` 这类字段名里本身就含 `driver/passenger` 方位关键字，会被前一个分支截走，把 `'0'` 送进状态判读（而状态判读里 `0` 是"未上报/无效"返回 None），continue 之后就永远走不到 winPos 分支。
2. 精确匹配块里，命中 winPos 字段并解读出状态后，**没有把 `has_any_per_window` 置 True**（只有 winStatus 分支才会置），导致后续「对未命中窗用 raw_pos_state 补全全关」的逻辑根本不会跑。

**v16 修复点**：
- 启发式递归里**先判 winPos*/windowPos*（位置量），再判普通 status 字段**，彻底避免字段名关键字被截胡；
- 精确匹配阶段命中 winPos 且能判读出状态时，**直接写入 result、并置 has_any_per_window=True**，确保补全逻辑生效；
- 6 组典型场景（全关 winStatus 缺失 / 四窗微开 / 全关下发 winStatus=2 / 单窗全开 / 单窗微开不串位 / winPos 单独判开度）全部单元测试通过；
- **天窗、遮阳帘、所有开关/按钮/定位/充电桩、unique_id 一律未动。**

部署：覆盖 `/config/custom_components/geely_galaxy/` 后删除 `__pycache__/` 并重启 HA。已装 v9-v15 不需要重新配置集成，也不需要删孤儿设备。

## v15 更新（最终校准：winStatus/winPos 与实车行为完全对齐）

根据你两次实车返回的真实数据（winPos=8 时是微开/未关严，winPos=0 才是真全关到位），完成最终校准：

| winStatus | winPos | 真实状态 |
|---|---|---|
| `'1'` | `'8'` | 微开/未关严（车窗在关闭位但未完全贴合密封条） |
| `'2'` | `'0'` | 全关到位（车窗完全闭合） |
| `'3'` | `'100'` | 全开到位 |

**v15 修复点**：
- 纠正 `_window_status_label` 码位映射：`'1'=微开/未关严`、`'2'=全关`、`'3'=全开`，与银河A7实车完全一致；
- 纠正 `_winpos_label` 语义：`winPos='8'` 不再误判为全关，改为解读为"8(未开/未关严)" → 状态=微开；`winPos='0'` 才是"0(全关到位)" → 状态=全关；
- winPos 作为兜底状态正常参与判读（winStatus 缺失时），不会再出现"全关到位显示未知"；
- 所有注释与属性说明同步更新，不再误导后续维护；
- **天窗、遮阳帘、所有开关/按钮/定位/充电桩、unique_id 一律未动。**

部署：覆盖 `/config/custom_components/geely_galaxy/` 后删除 `__pycache__/` 并重启 HA。如已装 v9-v14，不需要重新配置集成，也不需要删孤儿设备。

## v14 更新（winPos 位置量友好解读：0=全关到位 / 8=未开默认关闭）

根据实车反馈，银河 A7 VC 通道的 `winPos*` 字段含义已经明确：

| winPos 值 | 含义 | 状态判读 |
|---|---|---|
| `'0'` | 全关到位（车窗完全闭合） | 全关 |
| `'8'` | 未开 / 默认关闭（车窗在关闭位，未收到开启指令） | 全关 |
| 1–7、9–99 | 开度百分比（中间位置） | 微开/部分打开 |
| `'100'` | 全开到位 | 全开 |

你之前看到"位置量 raw 主驾:'8'"——这正是全关状态下的正常值。`'8'` 不是"开了 8%"，而是车端的"关闭/未开"枚举码；**全关时会有两种码（0 与 8）**，都属于关闭。

**v14 修复点**：

- 属性 `位置量_raw` 更名为 `位置量`，值从裸的原始值改为解读后的友好字符串，例如：
  - 全关：`主驾: 8(未开/默认关闭)，副驾: 8(未开/默认关闭)，左后: 0(全关到位)，右后: 8(未开/默认关闭)`
  - 开窗：`主驾: 15(开度15%)，副驾: 100(开度100%)`
- winStatus* 仍是主判读依据，winPos 解读仅作为状态属性展示；但当 winStatus* 缺失时，会用 winPos 的判读结果兜底，避免出现"字段明明在但显示未知"的情况；
- 整体状态判读（全关/微开/全开/部分打开）的合并规则不变，四窗最终状态依旧由 winStatus* 主导。
- **天窗、遮阳帘、所有开关/按钮/定位/充电桩、以及 unique_id 一律未动。**

部署：覆盖 `/config/custom_components/geely_galaxy/` 后删除 `__pycache__/` 并重启 HA。如已装 v9/v10/v11/v12/v13，**不需要**重新配置集成，也不需要删孤儿设备。

## v13 更新（车窗状态最终修复：纠正 VC 通道状态码语义 + 分离位置量）

拿到银河 A7 实车数据后，定位到前几版显示错误的**真正根因**：状态码语义搞反了。

**实车数据（车停稳、四窗全关 + 天窗/遮阳帘全关时）**：

```
vehicleWindowStatus:
  winPosDriver: '8'           winStatusDriver: '1'
  winPosPassenger: '8'        winStatusPassenger: '1'
  winPosDriverRear: '8'       winStatusDriverRear: '1'
  winPosPassengerRear: '8'    winStatusPassengerRear: '1'
  sunroofPos: '0'    sunroofOpenStatus: '1'
  curtainPos: '0'    curtainOpenStatus: '1'
```

**根因分析**：

1. **四门车窗在 VC 通道**（`/vc/app/v1/vehicle/control/status`）**关闭时字段是存在的**，不是"不下发"。四个窗的 `winStatus*` 字段全都会返回值（全关时是 `'1'`）；
2. **状态码语义被搞反了**：VC 通道里 `1=关闭`、`2=全开`、`3=微开`。前几版沿用老 L7 时代的映射把 `'1'` 判成"全开"，所以四个窗全显示"全开"；
3. **`winPos*` 是车端位置量**（实测值 `'8'`），**不是** 0-100 开度百分比，不能直接用百分比判"全开/全关/部分打开"，否则会被误判；
4. 对照青龙 `jlyh.js`（同样走 `galaxy-vc.geely.com` 控制窗口）和 `Eternity-h/geely-galaxy`（星舰7分支），确认天窗/遮阳帘的 `sunroofPos/curtainPos` 是 0-100 开度百分比（这条之前判断正确），而四门车窗的 `winPos*` 与 `winStatus*` 是另一套编码。

**v13 修复点**：

- 状态码映射按 VC 通道语义重写：`1=全关`、`2=全开`、`3=微开`；英文/中文显式值（`open/close/ajar`、`打开/关闭/微开`）跨通道仍通用；
- `winPos*` 从状态判读字段中移除，加入排除关键字，避免再把位置量当成状态；改为收集到属性 `位置量_raw` 中供你核对含义；
- 四门车窗精确字段：`winStatusDriver / winStatusPassenger / winStatusDriverRear / winStatusPassengerRear`，四个字段都会命中；
- 补全窗逻辑调整：只有"至少一个分窗字段命中"时才对未命中的窗做补全（套整体字段值），不再因 `vehicleWindowStatus` 块存在就强行补"全关"，避免误判；
- 天窗/遮阳帘仍以 `sunroofPos/curtainPos` 开度为权威（这个判断从 v9 起就正确），`sunroofOpenStatus/curtainOpenStatus='1'` 的兜底判读同步按 VC 语义判"全关"；
- v12 新增的「车辆原始数据」诊断传感器保留。
- **天窗、遮阳帘、所有开关/按钮/定位/充电桩 一律未动。**

部署：覆盖 `/config/custom_components/geely_galaxy/` 后删除 `__pycache__/` 并重启 HA。如已装 v9/v10/v11/v12，**不需要**重新配置集成，也不需要删孤儿设备。

## v12 更新（车窗状态显示仍不对：加诊断传感器 + 扩大字段候选）

**v12 修复点**：
- 新增「车辆原始数据」诊断传感器，把车端返回的完整 vehicle_status JSON 原样放到属性（按顶层块拆开放出，另附 \_all_keys 点路径列表，可 Ctrl+F 搜字段名）；
- 车窗状态传感器的属性新增 \_debug_all_window_fields：递归扫描整份 JSON，把所有含 window/win/glass/车窗关键词的字段（不管方位是否匹配）悉数列出——真实字段名一定在里面；
- 方位关键字扩充：增加 flwin/frwin/rlwin/rrwin、winfl/winfr/winrl/winrr、拼音 zj/fj/zh/yh 等更多组合；
- 顶层块额外暴露 vehicleClimateStatus / vehicleBatteryStatus / vehicleEngineStatus / additionalVehicleStatus / \_xchanger_extra，方便比对。
- **天窗、遮阳帘、所有开关/按钮/定位/充电桩 一律未动。**

## v11 更新（车窗状态显示不准：递归字段扫描 + 排障诊断）

**v10 遗留问题**：银河 A7 四门车窗字段**不是**固定位于顶层
`vehicleWindowStatus.windowStatusDriver` 这种命名里，而是可能藏在任意嵌套层级
或用非标准命名（如 `windowLightOpen`、`windowFullOpen`、`win_driver=ajar`、
`windowStatusFrontLeft=1` 等），且关闭时不下发。v10 仅在已知块里按候选字段名查找，
很容易漏读或错判（例如「右后微开」被错判为「副驾微开」）。

**v11 修复点**：

1. **递归扫描整份车辆状态 JSON**：字段名不再硬编码到特定块，任意层级都能命中。
2. **方位关键字「最长优先」匹配**：`windowStatusPassengerRear` 优先命中 `passengerrear`
   → 右后，不再被 `passenger` 抢走显示成「副驾微开」。
3. **补全 VC 整体枚举字段**：`windowLightOpen=1`、`windowFullOpen=1`、
   `windowClose=1` 分别对应微开/全开/全关（与青龙脚本控制命令同名）。
4. **VC 通道判定更稳**：只要 `vehicleWindowStatus` 里能读到 `sunroofPos`/`curtainPos`
   （说明这是 VC 通道报文、天窗已可上报），就认定车窗系统在工作，**未下发字段
   的窗自动判「全关」**；仅在确实走 XCHANGER 回退且无任何车窗字段时才显示「未知」。
5. **排障属性**：在「车窗状态」实体的属性里新增 `_命中字段`，列出所有匹配到的
   字段完整路径和原始值；继续保留 `_raw_vehicleWindowStatus` /
   `_raw_vehicleDoorCoverStatus`，方便对照真实字段名一次性对齐。
6. 天窗/遮阳帘同步加了递归兜底查找，避免 VC 通道字段层级变化时读数异常。

**部署提示**：本次只改 `sensor.py` 和 `README.md`，覆盖 `custom_components/geely_galaxy/`
下同名文件 → 删除 `__pycache__/` → 重启 Home Assistant 即可，无需重新配置集成，
也**不需要**像 v9 那样手动删孤儿设备。

**排障用法**：如果装完后车窗状态仍有异常，打开「车窗状态」实体 → 属性，
把里面 `_命中字段` 和 `_raw_vehicleWindowStatus` 的内容发给作者即可定位。

## v10 更新（本次修复「车窗已关闭时状态不显示」）

**问题现象**（实车反馈）：天窗、遮阳帘读数正常；但**车窗状态在车窗关闭时不显示**——
车端上报的车窗字段取值形如「微开 / 全开」，**关闭时该字段压根不下发**，
于是四个车窗一个都读不到，实体显示「未知」。

**根因**：`v9` 里车窗传感器的取值判读只认数字码（`1/2/3`）与少量英文，
且**字段缺失一律当作「未知」**；对银河A7 这种「关闭即不发字段」的车端报文，
就永远拿不到「关闭」这个状态。

**修复方式**（只动 `sensor.py` 一处，**开关逻辑一律未改**）：

1. 取值判读 `_window_status_label()` 扩展为同时兼容**数字码 / 英文 / 中文**：
   全开族 `1 / open / 打开 / 全开 / 全开车窗`，微开族 `3 / ajar / 微开 / 半开 / 微开车窗`，
   关闭族 `0 / 2 / close / 关闭 / 全关 / 已关闭`；
2. 新增 `_window_field_label()`：字段**存在但为空串**时也判为「全关」（部分通道用空串表示关闭）；
3. 车窗块优先取 **`vehicleWindowStatus`**（银河A7 的 VC 车窗块），
   其次 `vehicleDoorCoverStatus`、`vehicleClimateStatus`，XCHANGER 车型继续兜底到
   `additionalVehicleStatus.*`（含 `winStatus*`）；四个车窗的候选字段名统一扩充为
   `windowStatus* / windowPos* / winStatus* / window*/win*` 等多套命名；
4. **关键**：只要 `vehicleWindowStatus` 块存在（说明车端在报车窗系统），
   其中**未出现的窗即判为「全关」**——这正好对上银河A7「关闭时不发字段」的行为；
   其它通道（doorCover / XCHANGER）仍坚持「找得到才判读」，绝不臆造数值；
5. 额外支持「整体车窗字段」（`windowStatus / windowsStatus / windowOpenStatus /
   windowState / winStatus` 等），命中时四窗同值；
6. 实体属性里继续原样暴露 `_raw_vehicleWindowStatus`（并新增 `_raw_vehicleDoorCoverStatus`），
   方便随时核对你车端的真实字段名与取值。

**已验证**（模拟 VC 报文，全部通过）：

| 场景 | 截屏前的车窗状态 | 主驾/副驾/左后/右后 |
| --- | --- | --- |
| 主驾=微开、副驾=全开，后两窗未上报 | 部分打开 | 微开 / 全开 / **全关** / **全关** |
| `vehicleWindowStatus` 只剩天窗/遮阳帘（四窗全关） | **全关** | 全关 / 全关 / 全关 / 全关 |
| 整体字段 `windowStatus='微开'` | 微开 | 微开 / 微开 / 微开 / 微开 |
| 数字码：主驾=1，其余未上报 | 部分打开 | 全开 / 全关 / 全关 / 全关 |
| 老车型：只有 `doorLockStatusDriver` | 未知（不臆造） | — |
| XCHANGER：`climateStatus.winStatusDriver='3'` | 微开 | 微开 / — |

## v9 更新（修复「实体散落到多个设备 + 天窗/车窗状态读数错误」）

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

**⚠️ 需要你实机复核的一点（已在 v10 闭环）**：

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
