# Asset Bridge

Blender / Unreal Engine / Maya / Substance Painter 之间的模型快速收发通道，  
并额外支持 **Marmoset Toolbag 5**（烘焙 / 渲染）与 **Photoshop / SAI2**（贴图编辑：  
其它软件把贴图发过去、**保存后自动回传**）。  
工作区即本仓库根目录，所有代码、测试、日志、备份都在这里。  
仓库：<https://github.com/zhdcirno/AssetBridge>

**核心思路**：起一个本地中转服务，每个软件只装一个轻量适配器跟它说话，  
彼此不知道对方存在。加一个新软件 = 加一个适配器，其它代码不动。

```
Blender ─┐
UE5     ─┼── HTTP/JSON ──> bridge_server ──> runtime/payloads/<transfer_id>/
Maya    ─┤                      │
SP      ─┤                      ├── inbox 路由 / revision 计数 / 认领去重
Toolbag ─┤                      │
PS/SAI2 ─┘（由「编辑器守护进程」调用本程序打开，保存后回传，不经适配器轮询）
```

## 目录

```
AssetBridge/
├── server/                 中转服务（stdlib only）
├── shared/profiles.json    换算档案【唯一事实源】改这里，再 --sync
├── plugins/                七个 DCC / 编辑器适配器（源 = 事实源）
│   ├── blender/asset_bridge_blender/        Blender 插件
│   ├── maya/asset_bridge_maya/              Maya 插件（+ userSetup.py.example）
│   ├── substance_painter/asset-bridge/      Substance Painter 插件 (QML)
│   ├── unreal/AssetBridge/                  UE 引擎级插件（.uplugin + Content/Python）
│   └── toolbag/asset_bridge/                Toolbag 5 插件（mset API，data/plugin 自动加载）
├── tray/                   托盘守护 + 插件安装器（assetbridge_tray.ps1）
│   └── tray_config.json    托盘配置：端口 / 自检间隔 / 中转路径 / 手动指定路径
├── tools/
│   ├── editor_daemon.py     编辑器守护进程（PS / SAI2：打开贴图 → 监听保存 → 自动回传）
│   └── editor_daemon.json   守护配置：server_url + 各编辑器 exe 路径
├── runtime/payloads/       中转文件区（各软件互传的模型/贴图）
├── runtime/editor_daemon/  守护进程的状态文件（daemon.pid / status.json / stop.flag）
├── backups/                安装/卸载前的自动备份（install_*/uninstall_*）
├── assets/icons/           图标源（由 tools/gen_icons.py 生成）
├── samples/AssetBridgeTest/ UE 测试工程（轻量样本）
├── tests/                  测试与自检脚本
└── install.py              一键安装器（CLI）
```

## 安装

### 方式一：托盘一键安装（推荐）

1. 双击 `tray/AssetBridge-Tray.vbs` 启动托盘（自动拉起中转服务）。
2. 右键托盘图标 → **安装插件...**，安装器会：
   - 自动探测本机 Blender / Maya / Substance Painter / Unreal / Toolbag 5 / Photoshop / SAI2 的安装路径  
     （PATH / 环境变量 / App Paths 注册表 / 卸载注册表 / Autodesk 注册表 / 文件关联 / 快捷方式 / 各盘 DCC 目录扫描）；
   - 按最低版本要求校验，过低会提示无法安装：
     - **Blender ≥ 5.0**
     - **Maya ≥ 2023**
     - **Substance Painter ≥ 10.0.1（build 3757）**
     - **Unreal Engine ≥ 5.8**
     - **Marmoset Toolbag ≥ 5.0**（烘焙 / 渲染两用）
     - **Photoshop / SAI2：不限版本**（不装插件，仅由「编辑器守护进程」调用打开，路径同步进 `tools/editor_daemon.json`）
   - 一键把对应插件装到正确位置；UE 装成**引擎级插件** `<UE>/Engine/Plugins/AssetBridge`，对所有该版本工程通用，无需改工程配置；
   - 已装过的软件显示「已安装（最新）/ 已安装（源已更新，可重装）」，安装按钮变成**更新**；
   - **卸载**会先把插件移到 `backups/uninstall_<时间戳>/`，可回滚，绝不直接删；
   - 没探测到的软件用「指定」按钮手动选路径——**指定一次就会记住**（存进 `tray/tray_config.json` 的 `manual_paths`），之后刷新检测、重开安装器都认，一键安装 / 更新按钮照常可用；检测结果与手动指定都会参与合并（例如检测到 Blender 5.0、手动指定 5.2，两个版本都会装上）；
   - 支持「从 GitHub 下载最新版」后安装。

### 托盘设置

右键托盘 → **设置**：服务端口、自检间隔、中转文件路径（可「打开文件夹」或「清空」）。  
「清空」会删掉中转目录里的全部互传文件（传输历史记录仍保留，不影响已安装的插件），删除前有二次确认并报告删除项数与释放空间。

### 方式二：命令行

`python install.py` 把四个适配器部署到本机各软件（含 Blender 多版本、UE 引擎级插件）。

### 中转服务

托盘常驻会自动守护中转服务（`127.0.0.1:27631`）。也可手动运行：  
`python server/server.py --root runtime` 或 `server/start_bridge_server.bat`。  
互传的模型/贴图落在 `runtime/payloads/<transfer_id>/`，可在托盘设置里查看路径、打开文件夹或一键清空。

## 支持的流转

| 源 → 目标                    | 内容                        | 说明                                                       |
| ------------------------- | ------------------------- | -------------------------------------------------------- |
| 任意 → Blender/UE/Maya/SP   | 模型（网格/骨骼/材质槽/UV）          | 适配器常驻轮询 inbox，收到自动导入，支持增量更新                              |
| SP → UE / Blender         | 贴图（baseColor/normal/ORM…） | SP 回传 `kind=textures`，发送端对应资产原地重载（引用不丢）                  |
| 任意 → **Toolbag 5**        | 模型                        | 发送时选 **烘焙模式**（建烘焙组+设高低模）或 **渲染模式**（仅导入模型/材质）             |
| Toolbag → SP              | 烘焙贴图                      | 八猴只把烘焙结果发 SP（暂不支持发其它软件）；SP 用 `importProjectResource` 收下  |
| 任意 → **Photoshop / SAI2** | 贴图（仅贴图）                   | 由**编辑器守护进程**调用 exe 打开文件；保存后自动回传给发送端并重载 **（无需 ps2other）** |
| Photoshop / SAI2 → 其它     | —                         | 不支持（编辑器是纯接收端，保存即回传）                                      |

> **编辑器守护进程**（`tools/editor_daemon.py`，由托盘「启动编辑器守护」拉起）：  
> PS / SAI2 没有可编程 API，所以由一个独立进程负责「打开贴图文件 → 监听文件 mtime/大小（含另存为新文件）→ 稳定后自动回传 → 触发发送端重载」。  
> 托盘右键菜单里可以启动 / 停止 / 查看守护状态，停止只发 `stop.flag`，**绝不杀用户的 PS / SAI2**。

## 实机验证结果（2026-09-24，本机真实跑出来的）

| 项目                        | 结果                                                 |
| ------------------------- | -------------------------------------------------- |
| Blender 5.0.1 发送端         | ✅ 通过                                               |
| UE 5.8.3 接收端              | ✅ 通过                                               |
| Blender → UE（静态网格）        | ✅ 1m 立方体 → UE 实测 **100×100×100 cm**                |
| UE → Blender（反向）          | ✅ 2×1×0.5m 长方体回 Blender 仍是 **2×1×0.5 m**           |
| 骨骼分支                      | ✅ SkeletalMesh + 3 根骨骼，无报错                         |
| 增量更新                      | ✅ 重复导入覆盖，不生成 `_1`，revision 递增                      |
| 材质槽                       | ✅ 2 个材质槽随模型过到 UE                                   |
| 协议层回归                     | ✅ 16/16                                            |
| QA 全链路                    | ✅ 6 用例 / 65 断言全通过                                  |
| 贴图回传 → UE                 | ✅ 3 张 64×64 PNG → `Texture2D`，增量不生成 `_1`，与网格资产同树共存 |
| Blender → SP（SP 自动收下并开项目） | ✅ 实测 SP 自行认领并回执「SP 已接收」                            |
| SP → UE（SP 自动烘焙 + 导出）     | ⏳ 待 SP 重启加载新版插件后验证                                 |
| Maya 适配器逻辑（桩驱动打真服务）       | ✅ 26/26（含 idle 节流、收发、异常路径）                         |
| Maya 实机（commandPort）      | ⏳ 需在 Maya 里手动启动一次                                  |

### 托盘与安装器（2026-10-13，本机无头用例）

| 项目                                             | 结果      |
| ---------------------------------------------- | ------- |
| 四个软件多源探测（PATH / 注册表 / 快捷方式 / 各盘 DCC 扫描）        | ✅ 通过    |
| 手动指定路径持久化（`tray_config.json` → `manual_paths`） | ✅ 40/40 |
| 检测结果 × 手动指定合并（targets 并集 / 版本合并）               | ✅ 通过    |
| 安装 / 更新 / 卸载（先备份到 `backups/`，可回滚）              | ✅ 通过    |
| 清空中转文件夹（统计项数 + 释放空间，失败项进日志）                    | ✅ 通过    |
| 事件异常三层兜底（逐个 try/catch + 全局 ThreadException）    | ✅ 通过    |

### 新增流转（2026-10-13，无头用例 + 部署到本机真实路径）

| 项目                                                                                                | 结果           |
| ------------------------------------------------------------------------------------------------- | ------------ |
| Toolbag 适配器逻辑（烘焙回传 SP / 渲染仅导入 / 贴图拒绝 / 服务宕机退避 / 空转安全）                                             | ✅ 5/5        |
| 编辑器守护闭环（PS 原地保存回传 + options 合并 / SAI2 另存为新文件 / 缺 from.app 报错 / 未保存 cancelled / 服务不可达 / --stop 空闲） | ✅ 7/7        |
| Blender 发贴图到编辑器 + 保存后重载（回传重载 / 普通载入 / 匹配规则 / from.project）                                        | ✅ 7/7        |
| Maya / UE 发贴图到编辑器 + 回传重载协议                                                                        | ✅ 部署完成       |
| SP 接收 `kind=textures`（importProjectResource + selectResources）                                    | ✅ lint 通过    |
| 托盘：Toolbag/PS/SAI2 探测 + 手动指定解析 + 守护菜单 + 配置同步                                                      | ✅ 无头用例通过     |
| install.py 增加 Toolbag 安装步骤 + 守护配置同步                                                               | ✅ dry-run 通过 |

### 单位换算（实测定论，别改）

**Blender 5.0 导出的 FBX 有效单位是厘米**：顶点按米写（1m 立方体半边长 0.5），  
米→厘米由根 Model 节点的 `Lcl Scaling=100` 承载，`UnitScaleFactor=1.0`。

- `blender>unreal` 导出 `global_scale = 1.0`（用 100 会放大 100 倍）
- `unreal>blender` 导入 `global_scale = 1.0`（**旧值 0.01 是错的**，Blender 5.0 导入器已内建 cm→m）
- `blender>maya` / `maya>unreal` / `unreal>maya`：`scale_factor = 1.0`
- `blender>substance_painter`：`global_scale = 1.0`

改完 `shared/profiles.json` 后**必须**同步：

```bash
python tests/profiles_sync.py --sync     # 同步
python tests/profiles_sync.py            # 一致性自检（30 个关键字段 × 6 份）
```

## 快速开始

```bash
# 1. 启动服务（默认 127.0.0.1:27631，端口被占会拒绝启动）
python server/server.py --root E:/CirnoWorkSpace/AssetBridge/runtime

# 2. Blender：把 plugins/blender/asset_bridge_blender 打包 zip，
#    Edit > Preferences > Add-ons > Install...

# 3. UE：引擎级插件 plugins/unreal/AssetBridge 装到
#    <UE>/Engine/Plugins/AssetBridge（对该版本所有工程通用，无需改工程配置）

# 4. Maya：**不要**往 Documents/maya/<版本>/scripts/ 放 userSetup.py
#    —— Maya 只执行 MAYA_SCRIPT_PATH 里找到的第一个 userSetup.py，
#    再放一个同名文件会把用户自己的 userSetup（比如病毒防护）顶掉。
#    正确做法：把 plugins/maya/userSetup.py.example 的启动代码**追加**到
#    用户已有的 Documents/maya/scripts/userSetup.py 末尾；
#    或者每次在 Script Editor 里执行一行（幂等，可重复执行）：
#      exec(open(r"E:\\CirnoWorkSpace\\AssetBridge\\plugins\\maya\\userSetup.py.example").read())

# 5. SP：plugins/substance_painter/asset-bridge 拷到
#    Documents/Adobe/Adobe Substance 3D Painter/plugins/
#    ⚠️ SP **不做插件热重载**：改了 main.qml 必须重启 SP 才生效
```

## 自检

```bash
python tests/smoke_test.py                  # 协议层 16 项
python tests/profiles_sync.py               # 换算档案一致性（6 份 × 30 字段）
python tests/maya_stub_harness.py           # Maya 适配器逻辑（假 maya 模块 + 真服务）26 项
python tests/maya_commandport_verify.py     # Maya 实机（需先在 Maya 里启动适配器）
python tests/sp_qml_lint.py                 # SP 插件 JS 语法自检（重启 SP 前先跑）
python tests/sp_send_model.py               # Blender 发模型给 SP（SP 会自动开项目）
python tests/sp_to_ue_chain.py              # SP 烘焙 + 回传贴图 -> UE 收入（一条命令跑完）
# 本轮新增流转的自检（无头，不需起 DCC）
python tests/test_toolbag_adapter.py        # Toolbag 适配器 5 项（烘焙回传 SP / 渲染仅导入 / 拒绝贴图 / 退避 / 空转）
python tests/test_editor_daemon.py          # 编辑器守护 7 项（PS/SAI2 保存回传 + options 合并 + cancelled / 服务不可达）
python tests/test_blender_textures.py       # Blender 发贴图 + 保存后重载 7 项
python tests/test_tray_functions.py         # 托盘安装器逻辑（探测 / 手动指定 / 守护菜单 / 配置同步）
```

## 注意事项

- **服务只监听 127.0.0.1**，协议无鉴权，跨机请走 SSH 隧道。
- 客户端全部强制绕过 HTTP 代理，否则公司网络下连本机地址会被劫成 502。
- Windows 上 `SO_REUSEADDR` 允许两个进程绑同一端口，所以服务启动前会先探端口，  
  被占就拒绝启动 —— 避免「连的是旧实例、数据落在别处」这种极难查的问题。
- Maya 2023 的 mayapy / mayabatch 在这台机器上起不来（DLL 初始化失败，无桌面会话），  
  所以走 commandPort 远程驱动，见 `plugins/maya/userSetup.py.example`。

详细设计见 [`ARCHITECTURE.md`](ARCHITECTURE.md)。
