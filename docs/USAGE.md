# Asset Bridge 使用文档（面向美术 / 技术美术）

> 适用版本：Blender 5.0.1、Unreal Engine 5.8.3、Maya 2023、Substance 3D Painter（本机实测）
> 项目根目录：`E:\CirnoWorkSpace\AssetBridge`
> 中转服务地址：`http://127.0.0.1:27631`　数据根：`E:\CirnoWorkSpace\AssetBridge\runtime`

---

## 1. 总览

**Asset Bridge 是「一键互发 DCC 资产」的本地中转工具**：在任一软件里选中模型 / 贴图 / 骨架，点一下就能发到另一个软件，对方自动收下并导入。Blender、Unreal、Maya、Substance Painter 之间互相收发，支持**增量更新同名资产**、**单位换算**、**材质槽 / UV / 骨架一起过**。

### 架构示意

```
  Blender          Unreal Engine        Maya         Substance Painter
    │                │                  │                  │
    │  选中对象      │                  │                  │
    │  点「发送」    │  内容浏览器选中  │  send_selection   │  点「发贴图」
    │      │         │  命令行发送       │      │           │      │
    └──────┼─────────┼──────────────────┼──────────┼──────┘
           │  HTTP / JSON（仅本机 127.0.0.1:27631）
           ▼
   ┌───────────────────────────────────────────────┐
   │              本地中转服务 bridge_server          │
   │  · 会话注册 / 心跳 / 在线状态                    │
   │  · 传输记录（事实来源，带 revision 计数）        │
   │  · 收件箱路由 + 认领去重（防重复处理）           │
   └───────────────────────┬─────────────────────────┘
                           │
                           ▼
   ┌───────────────────────────────────────────────┐
   │  文件区（runtime/payloads/<transfer_id>/）      │
   │  SM_Chair.fbx / T_*.png …                      │
   └───────────────────────────────────────────────┘
                           ▲
                           │  四个软件各自后台轮询自己的收件箱，收到就自动导入
```

### 支持的能力

| 能力 | 说明 |
|---|---|
| 模型收发 | 静态网格（SM_）/ 骨骼网格（SK_）在四软件间互发 |
| 贴图回传 | Substance Painter 画好的 baseColor / normal / ORM 等发回 UE（kind=textures） |
| 骨架收发 | FBX 自带骨骼层级，自动随模型过到 UE，不丢朝向 |
| 增量更新 | 重复发送同名资产 = 覆盖更新（revision 递增），**不会**生成 `SM_Chair_1` |
| 单位换算 | 米 / 厘米、Z-up / Y-up 自动处理，集中在 `shared/profiles.json` |
| 材质槽 / UV | 材质槽、UV 集、顶点色随 FBX 一起走，UE 侧自动建材质 |

> ⚠️ **铁律：中转服务必须先启动，四个软件都连它。** 任一软件没反应，第一步永远是先确认服务在跑（见第 5 节排查）。

---

## 2. 安装与启动

> 适配器文件**已经拷到各软件对应的目录里了**（见安装日志 `logs/install_20260924_040621.log`）。
> 你只需做下面每个软件标注的「需手动操作」步骤，把适配器**启用 / 加载**起来。

### 2.1 中转服务（最先做，必须）

**推荐方式：托盘守护程序（零窗口常驻，`🟢` / `🔴` 图标）**

双击桌面上的：

```
C:\Users\12069\Desktop\AssetBridge-Tray.vbs
```

（或项目内 `E:\CirnoWorkSpace\AssetBridge\AssetBridge-Tray.bat`；`.vbs` 完全不闪窗口，`.bat` 会闪一下 cmd。）

右下角托盘会出现一个图标，**颜色 = 服务状态**：

| 图标 | 含义 |
|---|---|
| 🟢 绿圆 + ▶ | 服务**运行中** |
| 🟠 琥珀圆 + ❗ | 服务**掉线**，守护会在 10 秒内自动拉起 |
| ⚪ 灰圆 + ⏸ | 服务运行中，但**守护已暂停**（不会自动拉起） |
| 🔴 红圆 + ■ | 服务**已停止**（守护也暂停） |

> 图标全部由 `tools\gen_icons.py` 生成，清单与自定义方法见 `docs\ICONS.md`。

- **双击图标** → 状态小窗（状态 / 端口 / PID / 守护开关 / 最近日志 + 启动·停止·重启·设置·隐藏到托盘）。
  点右上角关闭 = **缩回托盘，不退出**。
- **右键图标** → 菜单：

```
状态：运行中  :27631  pid=xxxxx      ← 实时状态（不可点）
────────────────────────
启动服务 / 停止服务 / 重启服务
暂停守护          ← 关掉自动拉起（再点变「恢复守护」）
────────────────────────
打开托盘日志 / 打开服务日志 / 打开项目文件夹
────────────────────────
设置...            ← 改「服务端口」「自检间隔(秒)」「中转文件路径」（可打开文件夹 / 清空）
────────────────────────
退出并停止服务
退出（服务保持运行）
```

> 🛡️ **它自带看门狗**：每 N 秒（默认 10 秒）自检一次 `127.0.0.1:27631`，服务掉了自动拉起。
> 所以**不需要**再开那个会常驻黑窗口的 `bridge_watchdog.ps1` / `run_watchdog.bat`（保留着只当"前台调试"用）。
> 这样能避免「中转服务进程意外退出 → 各 DCC 软件卡死」这类单点故障（见第 5.8 节）。

> ℹ️ 托盘由 `tray\assetbridge_tray_launch.vbs` 以「窗口样式 0」启动 → **全程没有任何控制台窗口**；
> 服务本身由 `pythonw.exe` + `tray\run_server.py` 启动 → 也**没有窗口**，且与托盘进程解耦
> （**退出托盘不会连带杀掉服务**，只有点「退出并停止服务」/「停止服务」才会停）。
> 日志：托盘 `logs\tray.log`，服务控制台输出 `logs\server_console.log`，配置 `tray\tray_config.json`。

> ⚠️ **单实例保护**：托盘同一时间只能有一个。若双击后"没反应"，通常说明托盘已在运行（右下角找图标）。
> 想重启托盘：先右键图标 → 退出，再双击启动器。

**备用方式 A：黑窗口常驻（无托盘时）**

```
E:\CirnoWorkSpace\AssetBridge\start_bridge_server.bat
```

启动后是一个常驻黑窗口，别关。端口被占会**拒绝启动**（见第 5 节）。

**备用方式 B：命令行直启（调试用）**

```bash
python E:\CirnoWorkSpace\AssetBridge\server\server.py --root E:/CirnoWorkSpace/AssetBridge/runtime
```

> 🔴 **铁律（务必先读）**：**中转服务没起，四个 DCC 软件全部会变卡！**
> 旧版适配器在服务不可达时会在主线程高频同步请求、且无退避/熔断，结果就是：
> Blender 列表找不到插件、Maya 卡在启动界面、UE 打开关卡极慢、SP 疯狂刷
> `HTTP 0` 日志。新版已全部改为「后台线程 + 指数退避 + 熔断 + 注册重试」，
> 但**服务仍必须先起**。任一软件没反应，第一步永远是先确认服务在跑。

命令行等价写法（不用 bat 时）：

```bash
python E:\CirnoWorkSpace\AssetBridge\server\server.py --root E:/CirnoWorkSpace/AssetBridge/runtime
```

启动成功会打印：

```
 asset-bridge 中转服务已启动
   监听   : http://127.0.0.1:27631/api/v1
   数据根 : E:\CirnoWorkSpace\AssetBridge\runtime
   负载区 : E:\CirnoWorkSpace\AssetBridge\runtime\payloads
   Ctrl+C 退出
```

### 2.2 Blender（需手动启用插件）

> ⚠️ **重要路径更正**：Blender 5.0 的用户级插件目录是
> `C:\Users\12069\AppData\Roaming\Blender Foundation\Blender\5.0\scripts\addons\asset_bridge_blender`
> （即 `%APPDATA%\Blender Foundation\Blender\5.0\scripts\addons\`）。
> 旧版文档写的 `D:\DCC\Blender5.0.1\5.0\scripts\addons\` **不在 5.0 的搜索路径里**，
> 装在那里插件在列表里根本找不到。安装器已自动清理旧错误路径与误装的单个
> `__init__.py`，并把整个包复制到上面的正确目录。

适配器已就位：`C:\Users\12069\AppData\Roaming\Blender Foundation\Blender\5.0\scripts\addons\asset_bridge_blender`

> ⚠️ **需手动操作**：Blender 的插件机制**无法绕过**，必须手动勾选启用一次。
> 1. 打开 Blender → `Edit` → `Preferences`（编辑 → 偏好设置）
> 2. 左侧选 `Add-ons`（插件）
> 3. 右上搜索框输入 **`Asset Bridge`**
> 4. 勾选 **Asset Bridge (Blender)**（分类 Import-Export）
> 5. 面板默认地址就是 `http://127.0.0.1:27631`，无需改

启用后，3D 视图右侧工具栏（按 `N` 打开）会出现 **Asset Bridge** 标签页。

### 2.3 Maya（需重启 Maya）

- 适配器已就位：`C:\Users\12069\Documents\maya\2023\zh_CN\scripts\asset_bridge_maya`
- 启动代码已追加进：`C:\Users\12069\Documents\maya\scripts\userSetup.py`

> ⚠️ **需手动操作**：**重启 Maya** 让 `userSetup.py` 生效。无需手动 `exec`，重启后适配器自动 `start()` 并注册会话、挂上后台轮询。

验证是否加载：Maya 启动后看 Script Editor 的 Output 里有没有 `[bridge] Maya 适配器已启动`。

### 2.4 Substance Painter（需重启 SP）

适配器已就位：`C:\Users\12069\Documents\Adobe\Adobe Substance 3D Painter\plugins\asset-bridge`

> ⚠️ **需手动操作**：**完全退出并重启 Substance Painter**。SP **没有插件热重载**，改了文件不重启不会生效。

重启后工具栏会出现桥接按钮（见第 3.4 节）。SP 启动日志在 SP 的 `Log` 面板里看 `[bridge]` 前缀。

> 💡 **注册自重试**：新版 SP 适配器在 `start_bridge_server.bat` 启动后若服务还没起来，会按
> 指数退避**自动重试注册**（连续失败 ≥5 次会熔断并提示「请先启动 start_bridge_server.bat」，
> 之后每 30 秒探测恢复）。所以服务恢复后 SP 会自动重新上线，**无需手动重启 SP**。
> 但**改动 `main.qml` 源代码后仍需完全重启 SP** 才会生效（QML 无热重载）。

### 2.5 Unreal Engine（打开工程即自动加载）

- 适配器已就位：`E:\CirnoWorkSpace\AssetBridge\unreal\project\AssetBridgeTest\Content\Python\asset_bridge_unreal`
- 桥接脚本已就位：`E:\CirnoWorkSpace\AssetBridge\unreal\project\AssetBridgeTest\Content\Python\init_unreal.py`
- `DefaultEngine.ini` 已追加 `[/Script/PythonScriptPlugin.PythonScriptPluginSettings]` 段自动启用

> ⚠️ **无需 GUI 勾选**：用这个工程文件打开即可自动加载：
> ```
> E:\CirnoWorkSpace\AssetBridge\unreal\project\AssetBridgeTest\AssetBridgeTest.uproject
> ```
> 打开 / 重启 `AssetBridgeTest` 工程，适配器自动 `start()`（前提是中转服务已起，否则会打一条 `[bridge] 连接中转服务失败` 警告，但工程照常能开，服务起来后下一轮自动连上）。

UE 侧发送是**命令行触发**（没有 GUI 按钮），见第 3.3 节。

### 启动顺序小结

```
1) 双击 start_bridge_server.bat   ← 最先
2) 打开 Blender 并手动勾选启用插件
3) 重启 Maya
4) 重启 Substance Painter
5) 打开 AssetBridgeTest.uproject
```
四个软件都连同一个 `127.0.0.1:27631`，谁先开后开都行，**只要服务先起**。

---

## 3. 日常使用

### 3.1 Blender（发送 / 接收）

**入口**：3D 视图 → 按 `N` 打开右侧边栏 → **Asset Bridge** 标签页（面板内分「发送 / 接收 / 中转服务」三块）。

**发送**：
1. 在 3D 视图里**选中**要发的网格 / 骨架（多选可发多个）。
2. 在「发送」块里：
   - **目标**：下拉选 `Unreal Engine` / `Maya` / `Substance Painter` / `Blender`
   - **资产名**：留空则自动用选中物体名（会过滤成合法名）
   - **模式**：`更新同名资产`（覆盖，推荐）/ `始终新建`
   - 勾选 `前缀`（自动加 `SM_`/`SK_`）、`带材质槽`、`带贴图` 按需
3. 点 **「发送选中对象」** 按钮。
4. 成功会在底部状态栏提示：`已发送 SM_Chair -> unreal（第 1 版，SM_Chair.fbx）`。

**接收**：
- 默认**自动接收**已勾选（`自动接收` 开关）——别的软件发过来，Blender 后台每 0.5 秒轮询一次，收到就自动导入，并弹一个 `Asset Bridge 已导入 N 项资产` 提示。
- 想手动拉一次：点 **「立即接收」** 按钮。
- 想看底层文件：点接收块右侧的**文件夹图标**（打开负载目录）。

**连服务**：「中转服务」块里 `地址` 默认 `http://127.0.0.1:27631`；点 **「测试中转服务连接」** 可立即验证。显示 `已连接。在线端：unreal(online), maya(online) …` 即正常。

> 第一次用建议：点一下「测试中转服务连接」，确认能连上且能看到其它已开的软件在线。

### 3.2 Maya（发送 / 接收）

Maya 适配器**没有 GUI 面板**，全部用 Script Editor 里的 Python 命令驱动（已随 `userSetup.py` 自动加载）。

**发送**（先选中物体，再在 Script Editor 的 Python 标签执行）：

```python
import asset_bridge_maya as abm
# 发到 Unreal（默认目标）；要发 Blender 改成 target_app="blender"，发 SP 改 "substance_painter"
abm.send_selection(target_app="unreal")
```

可选带备注：

```python
abm.send_selection(target_app="unreal", note="从 Maya 发的椅子")
```

> 目标软件就是命令里的 `target_app="..."` 参数（取值：`unreal` / `maya` / `substance_painter` / `blender`）。
> 发送成功会在 Script Editor 提示：`[bridge] 已发送 Chair -> unreal (rev 1)`。

**接收**：**全自动**。适配器挂了 `idle` 脚本任务（scriptJob），Maya 空闲时自动轮询「发给 Maya」的收件箱并导入到当前场景，无需任何操作。提示形如 `[bridge] 收到 SM_Chair (rev 1)`。

**自检 / 重启适配器**（适配器没反应时）：

```python
import asset_bridge_maya as abm
abm.start()          # 重新注册会话 + 挂轮询 + 开 commandPort
```

> 详见第 5.3 节「Maya commandPort 卡死」自救。

### 3.3 Unreal Engine（发送 / 接收）

**接收（全自动）**：打开 `AssetBridgeTest` 工程后，后台线程每 0.5 秒轮询「发给 unreal」的收件箱，在**游戏线程**自动导入，并把静态网格**直接摆进当前关卡原点**。导入路径默认 `/Game/Bridge`，贴图在 `/Game/Bridge/Textures`。Output Log 里看 `[bridge] ✓ SM_Chair (rev 1) -> 导入网格 ... ，N 个材质槽，摆放 1 个`。

> UE 侧**没有「立即接收」按钮**——全靠后台自动轮询，只要工程开着就在收。

**发送**（从内容浏览器选中资产，在 Output Log 底部的 Python 命令行执行）：

```python
import asset_bridge_unreal as abu
# 把内容浏览器里选中的静态网格发回 Blender（默认目标就是 blender）
abu.send_selected_assets("blender")
# 想发到 Maya：abu.send_selected_assets("maya")
```

> 目标软件 = `send_selected_assets("blender" | "maya" | "substance_painter")` 的第一个参数。
> 发送成功 Output Log 提示：`[bridge] 已发送 1 个资产 -> blender (rev 1)`。

**确认导入结果**：在内容浏览器里看 `/Game/Bridge` 下是否出现了同名资产（覆盖更新不会加 `_1`）。

### 3.4 Substance Painter（发送 / 接收）

**入口**：SP 工具栏上的桥接按钮（由 `toolbar.qml` 注入），一组两个按钮：
- **收模型**（pull）—— 从收件箱拉取别的软件发来的模型
- **发贴图**（push）—— 把当前项目画好的贴图回传给目标软件

**接收模型**：
- 方式 A（自动）：SP 默认每 1 秒轮询收件箱，收到模型会用它**直接开一个新项目**（SP 没有「原地刷新网格」的 API，所以会先自动保存当前项目，再关掉、用新网格新建）。
- 方式 B（手动）：点工具栏 **「收模型」** 按钮。
- 成功后 SP 日志：`[bridge] 已打开 SM_Cube（第 1 版）`。

**回传贴图**：
- 点工具栏 **「发贴图」** 按钮。
- 默认发回：`Unreal` / 工程 `AssetBridgeTest` / 路径 `/Game/Bridge/Textures`（这些默认值写在 `main.qml` 里）。
- 导出用当前工程自带的导出预设（没有则兜底 `PBR MetalRough`），按激活贴图集分辨率导出。
- 成功后 UE 侧会自动收下贴图并建成 `Texture2D`（baseColor=颜色、normal/ORM/Rough 等按通道自动设压缩与 sRGB）。

> 回传前请确保在 SP 里**已经烘焙 / 绘制**了贴图，否则会提示「没有导出任何贴图」。
> ⚠️ 想让 SP 的贴图被**远程一键拉回**（而不是人去点按钮），由 UE / Blender 侧脚本发一条 `kind=textures_request` 给 SP 即可，SP 收到会自动烘焙 + 导出 + 回传。

---

## 4. 单位换算与校准（实测结论，别乱改）

**一句话结论：所有换算的 `global_scale` / `scale_factor` 都是 `1.0`，旧的 `0.01` 是错的。**

本机实测（2026-09-24，Blender 5.0.1 + UE 5.8.3）：

| 场景 | 实测结果 |
|---|---|
| Blender 1m 立方体 → UE | **100 × 100 × 100 cm** ✅（正确，米→厘米由 UE 的 Convert Scene Unit 处理） |
| UE 2×1×0.5m 长方体 → Blender | 仍是 **2 × 1 × 0.5 m** ✅ |
| Blender → Maya（1m 立方体） | 导入后为 **100cm**，往返无损 ✅ |
| UE → Blender（旧 `global_scale=0.01`） | 缩小 100 倍 ❌（错误，已修正为 `1.0`） |

各路线缩放系数（都在 `shared/profiles.json` 里）：

| 路线 | 缩放 |
|---|---|
| `blender>unreal`（导出） | `global_scale = 1.0` |
| `unreal>blender`（导入） | `global_scale = 1.0`（**旧 0.01 错**） |
| `blender>maya` | `global_scale = 1.0` |
| `maya>unreal` / `unreal>maya` | `scale_factor = 1.0` |
| `blender>substance_painter` | `global_scale = 1.0` |

换轴：Blender / UE 都是 Z-up，无需换轴；Maya / SP 是 Y-up，Z↔Y 交给导入端处理。

### 改换算档案的正确姿势

换算档案 `shared/profiles.json` 是**唯一事实源**。各适配器各自带一份 `profiles.json` 副本，改了**共享档案后必须同步**：

```bash
# 1) 改 E:\CirnoWorkSpace\AssetBridge\shared\profiles.json
# 2) 同步到各适配器副本
python E:\CirnoWorkSpace\AssetBridge\tests\profiles_sync.py --sync
# 3) 顺手做一次一致性自检（4 份 × 18 个关键字段）
python E:\CirnoWorkSpace\AssetBridge\tests\profiles_sync.py
```

> ⚠️ 如果只改了某个适配器本地的 `profiles.json` 而没改 `shared/`，下次 `--sync` 会被覆盖。一律改 `shared/profiles.json` 再 `--sync`。

---

## 5. 故障排查

### 5.1 中转服务起不来（端口被占）

- **现象**：双击 `start_bridge_server.bat` 后窗口打印 `端口 127.0.0.1:27631 已被占用，已有实例在跑`。
- **原因**：Windows 上两个进程可能绑同一端口，服务为防「连到旧实例、数据落错地方」而**拒绝启动**。
- **解决**：关掉之前那个 `bridge_server` 黑窗口（或任务管理器结束 `python server/server.py` 进程），再重新双击 bat。

### 5.2 连本机报 502

- **现象**：客户端提示 `502 upstream connect failed` 或连不上。
- **原因**：公司网络下 `HTTP_PROXY` 会把 `127.0.0.1` 也劫走。所有客户端**已强制绕过代理**（`ProxyHandler({})`），但若仍 502，说明环境变量/代理软件在作怪。
- **解决**：确认没有全局 HTTP 代理强制拦截 localhost；必要时在系统/终端里 `set HTTP_PROXY=` 清空后再启动服务与各软件。

### 5.3 Maya commandPort 卡死

- **现象**：Maya 里 Asset Bridge 没反应，Script Editor 命令发过去不回显、不执行。
- **原因**：Maya 的 commandPort 收到「半句被截断的代码」会卡在「等续行」状态；或旧端口残留占用 7001。注意 **GUI 解释器与 commandPort 解释器是分开的**，GUI 里正常不代表 commandPort 活着。
- **解决**（在 Script Editor 的 Python 标签执行「关端口 + 重 exec」两行自救）：
  ```python
  import maya.cmds as cmds
  cmds.commandPort(name="127.0.0.1:7001", close=True)   # 先关旧端口（含可能卡死的）
  cmds.commandPort(name="127.0.0.1:7001", sourceType="python", echoOutput=True)  # 再开干净端口
  ```
  然后重新加载适配器（GUI 与 commandPort 各自一次）：
  ```python
  import asset_bridge_maya as abm
  abm.start()
  ```

### 5.4 Substance Painter 改了不生效

- **现象**：改了插件逻辑 / QML，SP 里行为没变。
- **原因**：SP **没有热重载**，插件只在启动时加载一次。
- **解决**：完全退出 Substance Painter 再重启。改 QML 前可先跑语法自检：`python E:\CirnoWorkSpace\AssetBridge\tests\sp_qml_lint.py`。

### 5.5 单位不对（模型放大 / 缩小 100 倍）

- **现象**：跨软件资产尺寸差 100 倍。
- **原因**：换算系数错（旧 `0.01` 等）或没同步。
- **解决**：核对 `shared/profiles.json` 里对应路线的 `global_scale` / `scale_factor` 应为 `1.0`；改完跑 `python tests/profiles_sync.py --sync`。校准方法：导出一个已知 1m 立方体，在目标软件量一下再回写。

### 5.6 某软件「没反应」（收不到 / 发不出）

按这个顺序查：
1. **中转服务在跑吗？** 看 `start_bridge_server.bat` 窗口是否还开着，或浏览器开 `http://127.0.0.1:27631/api/v1/health` 是否返回 `{"ok": true, ...}`。
2. **适配器启用 / 加载了吗？**
   - Blender：偏好设置里 `Asset Bridge (Blender)` 勾上了吗？
   - Maya：Script Editor 有 `[bridge] Maya 适配器已启动` 吗？没有就 `abm.start()`。
   - SP：重启过 SP 吗？工具栏有桥接按钮吗？
   - UE：用的是 `AssetBridgeTest.uproject` 打开的吗？Output Log 有 `[bridge] Unreal 适配器已启动` 吗？
3. **在线状态**：Blender 面板点「测试中转服务连接」，看对方软件是否 `online`。
4. **目标选对了吗？** Blender 的「目标」下拉 / Maya 的 `target_app` / UE 的 `send_selected_assets(...)` 参数是否指向正确的接收方。

### 5.7 跨机使用

- **现象 / 限制**：服务**只监听 `127.0.0.1`** 且协议**无鉴权**，不能直接 `--host 0.0.0.0` 暴露到公网。
- **解决**：走 SSH 隧道把远端端口映射到本机：
  ```bash
  # 在要连远端服务的机器上执行（把远端的 27631 映射到本机 27631）
  ssh -N -L 27631:127.0.0.1:27631 user@remote-host
  ```
  之后各软件地址照填 `http://127.0.0.1:27631` 即可。

### 5.8 「中转服务一退出，所有软件都卡」事故（本次已修）

- **现象**：中转服务进程意外退出后——Blender 插件列表里找不到 Asset Bridge；
  Maya 一直卡在启动界面；UE 打开测试关卡非常卡（延迟）；SP 变卡且日志疯狂刷
  `[bridge] GET /inbox?... 失败: Error: HTTP 0`。
- **根因**：四个适配器在服务不可达时**没有退避、没有熔断、且在主线程做同步阻塞
  网络 IO**，把宿主软件全部拖死；其中 SP 还「注册失败永不重试」，服务恢复后也不上线。
- **已修复**（2026-09-24）：
  - 所有网络 IO 移到**后台 daemon 线程**，主线程只做 DCC API 调用；
  - **指数退避** `min(1000·2^(n-1), 30000)ms` + **熔断**（连续失败 ≥5 次暂停并提示）
    + **失败日志限频**（最多每 30s 一条）；
  - SP / Maya / UE 在拿到 session 前**不发 claim**、注册**按退避自动重试**；
  - 新增 `bridge_watchdog.ps1` 守护（见 2.1），`start_bridge_server.bat --watch` 进入守护模式。
- **用户侧处置**：
  1. 先确认服务在跑（`start_bridge_server.bat` 窗口在 / 浏览器开 `http://127.0.0.1:27631/api/v1/health`）。
  2. 不在就双击启动；若怕再退出，用 `--watch` 守护模式。
  3. 服务起来后 Maya / UE 通常**立即自愈**；SP 会自动重试注册上线（改了 `main.qml` 才需重启 SP）；
     Blender 若之前因装错目录而找不到，按 2.2 重新在正确目录启用即可。

### 5.9 ⚠️ 用户自备残留：`userSetup.py` 里的 `vaccine` / `leukocyte`（**只提示，不代改**）

- **现象**：`C:\Users\12069\Documents\maya\scripts\userSetup.py` **第 1–8 行**有：
  ```python
  import vaccine
  vaccine.phage()
  # ...
  leukocyte.occupation()
  ```
  而 `vaccine.py` 是个**空模块**，因此每次 Maya 启动执行到这几行都会抛 `AttributeError`。
- **为什么要注意**：
  1. `userSetup.py` 是 **Maya 启动即执行**的脚本。如果开头几行就抛异常，**后面追加的
     Asset Bridge 自启动代码（`asset_bridge_maya.start()`）可能不会被执行** → 表现就是
     「Maya 里 Asset Bridge 没反应」。此时可在 Maya 的 Script Editor 里手动执行
     `import asset_bridge_maya as abm; abm.start()` 兜底启动。
  2. 这类 `vaccine.phage()` / `leukocyte.occupation()` 是**典型的 Maya 病毒（malware）残留特征**；
     机器上还留有 `Documents/maya/2018/zh_CN/prefs/__pycache__/vaccine.cpython-39.pyc` 等痕迹，
     说明这台机器历史上中过 Maya 脚本病毒。
- **我们的立场**：这几行属于**用户自己的文件内容**，本工具**不擅自修改或删除**。
  仅在此提示，请用户自行判断后清理（例如注释掉/删除该段，并扫描清除相关的
  `vaccine.py` / `vaccine*.pyc` / `leukocyte*` 文件）。清理前建议自行备份。

### 5.10 托盘「停止服务」报「拒绝访问」（已修）

- **现象**：托盘日志出现
  `停止服务失败: 无法停止进程"python (50692)": 拒绝访问。`，紧接着
  `启动跳过：已在运行` —— 服务既停不掉、也换不成新进程。
- **根因**：那个服务进程是**别的会话/终端**启动的（例如命令行直启、或由别的
  后台任务拉起）。Windows 下 `Stop-Process` 跨会话拿不到 `PROCESS_TERMINATE`
  权限（未提权且无 `SeDebugPrivilege`）→ 被拒绝；而端口还被占着，
  `Start-Server` 又正确地拒绝重复启动。
- **已修复**（2026-09-25）：服务新增 **`POST /api/v1/shutdown`** 优雅关闭接口，
  托盘「停止服务」改为三步：
  1. 先 `POST /api/v1/shutdown`（**绕代理**，不需要任何进程权限，跨会话也有效）；
  2. 等它自己退出（最多 5 秒）；
  3. 仍活着才兜底 `Stop-Process`（同一会话由托盘拉起的服务一定能杀掉）。
- **兜底**：万一仍停不掉，命令行执行 `taskkill /F /PID <pid>`（pid 见托盘状态窗
  或 `netstat -ano | findstr :27631`）；托盘看门狗会在 10 秒内把服务重新拉起。
- **提示**：让托盘**自己**启动的服务是 `pythonw.exe`（`tray\run_server.py`），
  与托盘进程解耦但同会话，因此「停止 / 重启」一定有效。

---

## 6. 进阶 / 维护

### 6.1 新增一个软件适配器

加一个新软件 = 加一个适配器，**其它代码不用动**（这是架构的核心收益）。套路：
1. 在仓库下新建 `你的软件/plugin/asset_bridge_xxx/`，实现：
   - 注册会话（`POST /api/v1/sessions`）
   - 发送：导出 → `POST /transfers` → 落文件（`PUT /transfers/{id}/files/{name}` 或同机直写 payload 目录）→ `POST /transfers/{id}/publish`
   - 接收：后台轮询 `GET /api/v1/inbox?app=xxx` → `POST /transfers/{id}/claim` → 导入 → `POST /transfers/{id}/finish`
2. 在 `shared/profiles.json` 的 `routes` 里加 `<源>xxx` / `xxx>目标` 两条换算规则。
3. 客户端统一走**无代理** HTTP（避免 502），主线程才碰 DCC API，网络 IO 放定时器 / 子线程 / idle 回调。
4. 跑自检脚本确认协议层通过。

### 6.2 同步换算档案

```bash
python E:\CirnoWorkSpace\AssetBridge\tests\profiles_sync.py --sync   # 共享档案 → 各适配器副本
python E:\CirnoWorkSpace\AssetBridge\tests\profiles_sync.py         # 一致性自检（4 份 × 18 字段）
```

### 6.3 测试 / 自检脚本清单（`tests/`）

| 脚本 | 用途 |
|---|---|
| `smoke_test.py` | 协议层冒烟测试（16 项） |
| `profiles_sync.py` | 换算档案同步 + 一致性自检（带 `--sync`） |
| `maya_stub_harness.py` | Maya 适配器逻辑测试（假 maya 模块 + 真服务，26 项） |
| `maya_commandport_verify.py` | Maya 实机 commandPort 验证（需先在 Maya 里启动适配器） |
| `sp_qml_lint.py` | SP 插件 JS / QML 语法自检（重启 SP 前先跑） |
| `sp_send_model.py` | 从 Blender 发模型给 SP（SP 自动开项目） |
| `sp_to_ue_chain.py` | SP 烘焙 + 回传贴图 → UE 收入（一条命令跑完） |
| `qa_*.py` | 全链路 QA 用例（Blender / UE / 贴图闭环等） |

跑协议层与档案自检：

```bash
python E:\CirnoWorkSpace\AssetBridge\tests\smoke_test.py
python E:\CirnoWorkSpace\AssetBridge\tests\profiles_sync.py
```

---

## 附：各软件操作入口速查

| 软件 | 发送入口 | 接收方式 | 启用动作 |
|---|---|---|---|
| Blender | 侧栏(N) → Asset Bridge →「发送选中对象」 | 自动（或「立即接收」） | 偏好设置勾选启用 ⚠️ |
| Maya | Script Editor: `abm.send_selection(target_app=...)` | 自动（idle 轮询） | 重启 Maya ⚠️ |
| Unreal | Output Log: `abu.send_selected_assets("blender")` | 自动（后台线程 + 摆进关卡） | 打开 AssetBridgeTest 工程即加载 |
| Substance Painter | 工具栏「发贴图」 | 自动（或工具栏「收模型」） | 重启 SP ⚠️ |
