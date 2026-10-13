# Asset Bridge 架构设计

> 目标：让 Blender / Unreal / Maya / Substance Painter 之间**一键互发模型**，  
> 并扩展支持 **Marmoset Toolbag 5**（烘焙 / 渲染）与 **Photoshop / SAI2**（贴图编辑：  
> 其它软件把贴图发过去，**保存后自动回传**），带上材质槽、UV、骨架，保留变换与单位，  
> 重复发送时更新同名资产而不是堆 `SM_Chair_1`。

---

## 1. 总体结构

```
┌──────────┐  ┌──────────┐  ┌──────────┐  ┌───────────────────┐
│ Blender  │  │   UE5    │  │   Maya   │  │ Substance Painter │
│  (bpy)   │  │ (unreal) │  │ (cmds)   │  │  (JS / QML)       │
└────┬─────┘  └────┬─────┘  └────┬─────┘  └─────────┬─────────┘
     │             │             │                  │
┌────┴─────┐  ┌────┴───────┐     │                  │
│ Toolbag  │  │ PS / SAI2  │     │                  │
│  (mset)  │  │ (守护进程)  │     │                  │
└────┬─────┘  └────┬───────┘     │                  │
     └─────────────┴──────┬──────┴──────────────────┘
                          │  HTTP / JSON（仅本机 127.0.0.1:27631）
                          ▼
              ┌───────────────────────────┐
              │      bridge_server        │
              │  · 会话注册 / 心跳         │
              │  · transfer 记录（事实源） │
              │  · inbox 路由 + 认领去重   │
              │  · payload 文件区          │
              └─────────────┬─────────────┘
                            │
              E:\CirnoWorkSpace\AssetBridge\runtime\payloads\<transfer_id>\
                            ├── SM_Chair.fbx
                            └── （贴图等）
```

> **编辑器守护进程**（`tools/editor_daemon.py`）：Photoshop / SAI2 没有可编程 API，  
> 因此由托盘拉起一个独立守护进程——轮询 inbox → 认领 → 调用 exe 打开贴图 → 监听文件  
> 保存稳定 → 建 `kind=textures` 回传 transfer（并合并原 options 的 `return_of` / `source_file` 等）  
> → 触发发送端在原资产上原地重载。守护与中转服务解耦：服务挂了不影响它，它挂了也不影响各 DCC。

**为什么是「本地中转服务 + 文件」，而不是点对点直连：**

| 方案                               | 问题                                       |
| -------------------------------- | ---------------------------------------- |
| A 直接调 B 的 API（如 Blender 直接驱动 UE） | 要求 B 一定开着；A 里要写死 B 的版本适配；加一个软件要改 N 个插件   |
| 纯共享目录 + 约定文件名                    | 没有通知机制，只能手动刷新；没有版本/状态，做不了增量更新            |
| **本地服务中转（本方案）**                  | 各软件只跟服务说话，彼此不知道对方存在；加一个新软件 = 加一个适配器，其它不动 |

服务**只处理元数据和小文件**，大文件走磁盘 / PUT 上传。这样即使插件崩了，资产文件还在，可以手动补导入。

---

## 2. 传输协议

全部 REST + JSON，无第三方依赖（DCC 内嵌 Python 里不一定有 `requests`）。

| 方法   | 路径                                    | 用途                      |
| ---- | ------------------------------------- | ----------------------- |
| GET  | `/api/v1/health`                      | 健康检查                    |
| GET  | `/api/v1/config`                      | 拿 payload 目录（同机直写要用）    |
| POST | `/api/v1/sessions`                    | 注册一个 DCC 会话             |
| POST | `/api/v1/sessions/{id}/heartbeat`     | 心跳，超时标记 offline         |
| GET  | `/api/v1/sessions`                    | 看当前谁在线                  |
| POST | `/api/v1/transfers`                   | 建一条传输记录（草稿态）            |
| PUT  | `/api/v1/transfers/{id}/files/{name}` | 上传负载文件                  |
| POST | `/api/v1/transfers/{id}/publish`      | 发布 → 进入目标软件收件箱          |
| GET  | `/api/v1/inbox?app=unreal`            | 收件箱（按 app / project 隔离） |
| POST | `/api/v1/transfers/{id}/claim`        | 认领（**409 防重复处理**）       |
| POST | `/api/v1/transfers/{id}/finish`       | 回执（done / error）        |
| POST | `/api/v1/transfers/{id}/retry`        | 失败重投                    |
| GET  | `/api/v1/transfers`                   | 历史记录                    |

### transfer 记录

```jsonc
{
  "id": "a1b2c3...",            // 发送方生成的 UUID
  "asset_name": "SM_Chair",     // 只允许 [A-Za-z0-9_.-]
  "kind": "static_mesh",        // static_mesh | skeletal_mesh | animation | textures
  "format": "fbx",
  "from": { "app": "blender", "version": "4.2.0" },
  "to":   { "app": "unreal", "project": "Demo", "content_path": "/Game/Bridge" },
  "unit": { "scene_unit": "m", "up_axis": "Z", "forward_axis": "-Y", "unit_scale": 1.0 },
  "options": {
    "mode": "update",           // update=覆盖同名 / create=新建
    "import_materials": true, "import_textures": true,
    "combine_meshes": false, "import_animations": false
  },
  "objects": [ { "name": "Chair", "type": "MESH", "materials": ["M_Wood"],
                 "tris": 1240, "uv_layers": ["UVMap"] } ],
  "revision": 3,                // 同名资产第几次发送 —— 增量更新的依据
  "status": "draft|queued|claimed|done|error"
}
```

**状态机**：`draft → queued → claimed → done / error`，`retry` 可以把 error 拉回 queued。  
`claim` 返回 409 表示已被别的实例领走 —— 这是多台机器同时收同一个 inbox 时唯一的去重手段。

---

## 3. 四个关键能力怎么落地

### 3.1 一键发送 + 自动导入

- **发送方**：选中对象 → 按档案导出 FBX → 建记录 → 落文件 → publish。
- **接收方**：常驻轮询 inbox（Blender `bpy.app.timers` / UE Slate tick / Maya `scriptJob idle` / SP `onTick`），  
  收到就 claim + 导入，UE 侧还会把静态网格**直接摆进当前关卡**。

### 3.2 增量更新同名资产

- 服务端按 `(asset_name, to.app, to.project)` 累加 `revision`。
- UE 侧 `destination_name = asset_name` + `replace_existing=True` → **覆盖**同名资产，  
  保留场景里已摆好的 Actor 和蓝图引用（这正是纯手动导入最容易丢的东西）。
- `mode=create` 时 `destination_name` 留空，交回 UE 自己生成唯一名。

### 3.3 保留变换 / 轴心 / 单位

换算集中在 `shared/profiles.json`，不在代码里散落魔数：

| 软件                | Up 轴 | 前向 | 单位 |
| ----------------- | ---- | -- | -- |
| Blender           | Z    | -Y | m  |
| Unreal            | Z    | X  | cm |
| Maya              | Y    | Z  | cm |
| Substance Painter | Y    | Z  | cm |

- **Blender → UE**：两端都 Z-up 无需换轴；m→cm 交给 UE 的 `Convert Scene Unit`，  
  所以导出端 `global_scale = 1.0`（在导出端乱乘 100 是社区里最常见的错误）。
- **UE → Blender**：`import global_scale = 1.0`。Blender 5.0 的 FBX 导入器已内建 cm→m 换算，  
  旧值 `0.01` 是基于老版本 Blender 的错误假设（会把模型缩小 100 倍），已按实机校准修正。
- **Maya ↔ UE**：同为 cm 不缩放，`scale_factor = 1.0`，Y-up↔Z-up 交给 UE 的 `Convert Scene`。
- **Blender → SP**：SP 是 Y-up/cm，但 Blender 5.0 导出的 FBX 有效单位已是 cm，  
  所以 `global_scale = 1.0`（实测用 100 会放大 100 倍），只需显式把 Z-up 转成 Y-up。

> ⚠️ FBX 的单位/轴向语义在 Blender 各版本间有过调整（4.x 移除了部分轴向参数）。  
> 上面是**实机校准后的值**，也是 `shared/profiles.json` 里的唯一事实源；  
> 第一次接入新版本请先做一次校准：导出一个已知 1m 立方体，在目标软件里量一下，  
> 把结果写回 `profiles.json`。Blender 侧用 `io_fbx._call()`  
> 做 RNA 反射过滤，参数不存在时自动忽略而不是报错。

### 3.4 材质槽 / UV / 骨架

FBX 天然携带材质槽、UV 集、顶点色、骨骼层级，不需要自己序列化。  
插件额外在 `objects[]` 里带一份元数据（材质名、UV 层名、面数、世界变换），  
目标是让接收端**能校验**（"发过去 3 个材质槽，收到了几个"），而不是靠肉眼。

导出参数上做了几个针对性设置：

- `add_leaf_bones = false` —— 不然 UE 里每根骨骼末端多一根无用的末端骨。
- `path_mode = COPY` + `embed_textures = true` —— 贴图跟着走，避免 UE 侧丢贴图。
- `mesh_smooth_type = FACE` —— 保住硬边/平滑组，避免 UE 里重算法线。

---

## 4. 线程模型（各软件不一样，但思路一致）

**主线程才碰 DCC API，网络 IO 要么在主线程的空闲回调里做，要么在子线程做然后回主线程执行。**

| 软件      | 机制                                                                | 说明                            |
| ------- | ----------------------------------------------------------------- | ----------------------------- |
| Blender | `bpy.app.timers`                                                  | 子线程碰 `bpy` 会崩，所以全在 timer 里同步做 |
| UE      | 后台 `threading.Thread` 轮询 + `register_slate_post_tick_callback` 执行 | 资产操作必须在游戏线程                   |
| Maya    | `cmds.scriptJob(event=["idle", ...])`                             | 主线程空闲时执行，天然安全                 |
| SP      | `PainterPlugin.onTick`（`tickIntervalMS`）                          | QML 主线程                       |

另外：**所有客户端都强制 `ProxyHandler({})` 绕过代理**。公司网络里 `HTTP_PROXY` 会连  
`127.0.0.1` 也劫走，表现是 `502 upstream connect failed`，排查起来非常费时间。

---

## 5. 各适配器安装

### 一键安装（推荐：托盘安装器）

`tray/AssetBridge-Tray.vbs` 启动托盘 → 右键 **安装插件...**：

1. **探测**：对七个软件分别跑多源探测——PATH / `Get-Command` / 环境变量（`MAYA_LOCATION`、`DCC_ROOTS`）/
   App Paths 注册表 / 卸载注册表 / Autodesk 注册表 / 文件关联 / 快捷方式 / 各盘 DCC 目录限深扫描。
2. **版本门槛**：Blender ≥ 5.0、Maya ≥ 2023、SP ≥ 10.0.1（build 3757）、UE ≥ 5.8、Toolbag ≥ 5.0；
   Photoshop / SAI2 不限版本（只校验 exe 存在，不装插件，路径同步进 `tools/editor_daemon.json`）。
3. **部署**：复制到各软件的插件目录（UE 为引擎级插件），先备份旧版到 `backups/install_*`，绝不就地改名。
4. **状态判断**：安装完成后写 `.assetbridge.json` 指纹卡片，据此显示 未安装 / 已安装（最新）/ 可重装。
5. **手动指定兜底**：探测不到的软件可「指定」路径；**指定一次即持久化**到
   `tray/tray_config.json` 的 `manual_paths`，之后每次检测都会与自动探测结果合并
   （任一侧可用即安装；两边都可用时 targets 取并集，例如检测到 5.0 + 手动指定 5.2 会都装上）。
6. **卸载**：移到 `backups/uninstall_*`，可回滚。

### 服务（必须先跑）

托盘会自动守护中转服务；手动启动：

```bash
python server/server.py --root runtime --port 27631
```

### Blender

`plugins/blender/asset_bridge_blender` 整目录打包成 zip →  
`Edit > Preferences > Add-ons > Install...`。侧栏 `Asset Bridge` 面板里填服务地址即可。

### Unreal

引擎级插件：`plugins/unreal/AssetBridge` 整个拷到 `<UE>/Engine/Plugins/`，  
对该版本所有工程通用，无需改工程配置（Python 启动脚本已通过 `DefaultEngine.ini` 自动启用）。

### Maya

`plugins/maya/asset_bridge_maya` 放进 `Documents/maya/<版本>/zh_CN/scripts/`，  
并按 `plugins/maya/userSetup.py.example` 把启动代码**追加**到用户自己的 `userSetup.py` 末尾  
（不要新建同名文件——Maya 只执行 `MAYA_SCRIPT_PATH` 里找到的第一个 `userSetup.py`，会顶掉用户自己的）。

### Substance Painter

`plugins/substance_painter/asset-bridge` 整个文件夹拷到  
`Documents/Adobe/Adobe Substance 3D Painter/plugins/`。  
⚠️ SP **不做插件热重载**：改了 `main.qml` 必须重启 SP 才生效。

### Marmoset Toolbag 5

`plugins/toolbag/asset_bridge` 整个文件夹拷到 `<Toolbag 安装目录>\data\plugin\asset_bridge`  
（Toolbag 会自动扫描 `data/plugin/<文件夹>/__main__.py` 并加载）。插件通过 `mset.callbacks.onPeriodicUpdate`
轮询 inbox：收到模型时按发送端标注的 **bake / render** 模式分流——

- **烘焙模式**：`baker.addGroup(name)` 建烘焙组，`baker.importModel(path)` 让快速加载器自动归类高/低模，
  跑 `mset.baking.bake()`，完成后把烘焙贴图作为 `kind=textures` 回传给 **SP**（暂只支持发 SP）。
- **渲染模式**：只 `mset.importModel / importMaterial / frameObject` 导入模型与材质，不烘焙。

⚠️ Toolbag 的 `bake()` 是异步的，完成判定用「输出目录出现新文件且 mtime 稳定」法（轮询确认）。

### Photoshop / SAI2（编辑器守护）

这两个软件**不装插件**——它们没有可编程 API，无法像其它适配器那样主动收发。流程改为：

1. 发送端（Blender / Maya / UE）把贴图发往 `photoshop` / `sai2`；
2. 托盘拉起的**编辑器守护进程**（`tools/editor_daemon.py`）认领后，用 `subprocess.Popen([exe, 文件])`
   打开该贴图文件；
3. 守护进程监听文件 mtime/大小 + 同目录新图片（覆盖另存为）变化，稳定后建 `kind=textures` 回传 transfer，
   并把原 transfer 的 `options`（`return_of` / `source_file` / `editor` 等）合并回去；
4. 发送端在对应资产上**原地重载**贴图（引用不丢）；
5. 用户未保存直接关闭 → 宽限后按 `cancelled` 收尾。

安装器里 Photoshop / SAI2 的「安装」按钮只是把 exe 路径同步进 `tools/editor_daemon.json`（标 `无需安装插件`）。

### ZBrush 2026.1+（Python 适配器 + 守护进程握手）

ZBrush 2026.1 起有官方 Python SDK（`zbrush.commands`；2025.x 只有 ZScript，不支持）。
关键约束：**内嵌 VM 的 GIL 在主线程，zbc 只能在主线程调用**——适配器因此采用
「**主线程 serve 循环**」（迭代间 `zbc.update(1, True)` pump UI，停止按钮可随时打断），
无常驻后台线程。

1. **安装**：插件拷到**用户资产目录** `%APPDATA%\Maxon\Zbrush<版本>_<哈希>\ZStartup\ZPlugs64\asset_bridge_zbrush`
   （2026.1 起安装目录只读）；安装器写 `ab_config.json` 并往 `ZData\Python\init.py`
   注入自动加载钩子（marker 包裹、幂等；注入失败可 `ZScript>Python` 面板手动 Load Script）。
2. **接收**（其他软件 → ZBrush）：守护进程 claim 后写 `<payload>\zbrush_inbox\<tid>.json`
   任务文件 → serve 循环主线程 `set_next_filename + Tool:Import` 免对话框导入 →
   写 `<tid>.done.json` 回执，任务文件消费后即删。ZBrush 未运行时守护进程用
   `Popen([ZBrush.exe, "-script", one_shot_import.py, ...])` 一次性导入兜底；
   运行中但桥接未启动 → 落盘 + 托盘气泡（路径一键复制）+ `Tool:Load` 手动加载。
3. **发送**（ZBrush → 其他软件）：`发送当前模型` / `发送回最近发送者` 按钮 →
   `set_next_filename + Tool:Export` 免对话框导出 → HTTP 建传输/上传/publish；
   热文件夹 `<payload>\zbrush_outbox\` 兜底（守护进程自动回传）。
4. **单位**：OBJ 不带单位，ZBrush 导出按导入时记录的 Scale/Offset 还原原始数值，
   `global_scale=1.0` 透传（1m 立方往返实测）。ZBrush **无保存事件钩子**，发送为手动触发语义。

⚠️ 同目录单实例：ZBrush 已运行再拉起会被拒——所以守护进程先用
`<payload>\zb_bridge.heartbeat`（freshness ≤12s）判断常驻桥接是否活着，再决定握手还是兜底。

### 托盘自身的设置

右键托盘 → **设置**：服务端口、自检间隔、中转文件路径。中转路径旁可「打开文件夹」或
**「清空」**——清空会删除该目录下全部互传文件（传输历史记录保留，不影响已安装插件），
删除前二次确认，完成后报告删除项数与释放空间，删不掉的项进 `logs/tray.log`。

---

## 6. 当前进度 & 待办

| 模块          | 状态                                     |
| ----------- | -------------------------------------- |
| 中转服务        | ✅ 完成，16 项冒烟测试全通过                       |
| Blender 适配器 | ✅ 完成（发送 / 接收 / 面板 / 定时器 / 发贴图到编辑器 / 保存重载） |
| UE 适配器      | ✅ 完成（自动导入 / 摆放 / 回发 / 发贴图到编辑器 / 原地重载），引擎级插件安装 |
| Maya 适配器    | ✅ 完成（发送 / 接收 / 发贴图到编辑器 / 重载），userSetup 自启 |
| SP 适配器      | ✅ 完成（接收模型 / 回传贴图 / 接收 `kind=textures` / 回传目标含 PS·SAI2） |
| Toolbag 适配器  | ✅ 完成（bake / render 两模式；烘焙回传 SP）            |
| ZBrush 适配器   | ✅ 完成（2026.1+ Python 主线程 serve 循环；inbox 握手接收 / 按钮+热文件夹发送 / 1m 立方单位实测） |
| 编辑器守护进程    | ✅ 完成（PS/SAI2 打开贴图 → 监听保存 → 自动回传 + 重载；ZBrush inbox 握手 + -script 兜底 + 热文件夹）     |
| 托盘守护        | ✅ 完成（四态图标 / 看门狗自动拉起 / HTTP 优雅关服 / 编辑器守护菜单 / ZBrush 落盘气泡提示） |
| 插件安装器       | ✅ 完成（八软件多源探测 / 版本门槛 / 安装·更新·卸载 / 手动路径记忆 / GitHub 下载 / Toolbag 部署 / ZBrush init.py 钩子注入） |

下一步建议（按性价比排序）：

1. **SP → UE 贴图回传链路**实机验证（协议层已通，缺本机一次完整跑通）。
2. 加**USD 通道**（UE5.4+ 支持），比 FBX 更适合带复杂材质和变体。
3. **Maya 实机**（commandPort）跑通一次完整收发。
4. 资产冲突策略（同名但结构不同时是覆盖还是并存）目前是硬覆盖，需要加提示。

## 7. 已知坑

- Blender 4.x 的 FBX 导出器移除了部分轴向参数 —— 已用 RNA 反射兜住，单位语义按实机校准固化在 `profiles.json`（全 1.0）。
- SP 的 JS API 版本差异较大，`alg.project.create` / `alg.mapexport.save` 的参数签名需按本机文档核对。
- UE 的 `FbxImportUI` 在 5.x 各小版本间属性名会变，用到 `set_editor_property` 的地方建议包 try。
- 服务默认只监听 `127.0.0.1`。要跨机请用 SSH 隧道，不要直接 `--host 0.0.0.0`（协议目前无鉴权）。
- 托盘是 PowerShell 5.1 WinForms：全局 `$ErrorActionPreference='Stop'` 下，任何事件回调里的 cmdlet 出错
  都会变成未处理异常、弹 .NET JIT 崩溃框并杀死整个托盘。已做三层防御：检测/安装函数逐个 try/catch、
  按钮事件就近兜底、`Application.ThreadException` 全局接管（崩不掉，错误进 `logs/tray.log`）。
