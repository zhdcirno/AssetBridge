# Asset Bridge

Blender / Unreal Engine / Maya / Substance Painter 之间的模型快速收发通道。
工作区即本仓库根目录，所有代码、测试、日志、备份都在这里。

**核心思路**：起一个本地中转服务，每个软件只装一个轻量适配器跟它说话，
彼此不知道对方存在。加一个新软件 = 加一个适配器，其它代码不动。

```
Blender ─┐
UE5     ─┼── HTTP/JSON ──> bridge_server ──> runtime/payloads/<transfer_id>/
Maya    ─┤                      │
SP      ─┘                      └── inbox 路由 / revision 计数 / 认领去重
```

## 目录

```
AssetBridge/
├── server/                 中转服务（stdlib only）
├── shared/profiles.json    换算档案【唯一事实源】改这里，再 --sync
├── plugins/                四个 DCC 适配器（源 = 事实源）
│   ├── blender/asset_bridge_blender/        Blender 插件
│   ├── maya/asset_bridge_maya/              Maya 插件
│   ├── substance_painter/asset-bridge/      Substance Painter 插件 (QML)
│   └── unreal/AssetBridge/                  UE 引擎级插件（.uplugin + Content/Python）
├── tray/                   托盘守护 + 安装器（assetbridge_tray.ps1）
├── assets/icons/           图标源（由 tools/gen_icons.py 生成）
├── tools/                  代码生成 / 诊断脚本
├── samples/AssetBridgeTest/ UE 测试工程（轻量样本）
├── tests/                  测试与自检脚本
└── install.py              一键安装器（CLI）
```

## 安装

### 方式一：托盘一键安装（推荐）
1. 双击 `tray/AssetBridge-Tray.vbs` 启动托盘（自动拉起中转服务）。
2. 右键托盘图标 → **安装插件...**，安装器会：
   - 自动探测本机 Blender / Maya / Substance Painter / Unreal 的安装路径与版本；
   - 按最低版本要求校验，过低会提示无法安装：
     - **Blender ≥ 5.0**
     - **Maya ≥ 2023**
     - **Substance Painter ≥ 10.0.1（build 3757）**
     - **Unreal Engine ≥ 5.8**
   - 一键把对应插件装到正确位置；UE 装成**引擎级插件** `<UE>/Engine/Plugins/AssetBridge`，对所有该版本工程通用，无需改工程配置；
   - 支持「从 GitHub 下载最新版」后安装；未探测到的软件可手动指定路径。

### 方式二：命令行
`python install.py` 把四个适配器部署到本机各软件（含 Blender 多版本、UE 引擎级插件）。

### 中转服务
托盘常驻会自动守护中转服务（`127.0.0.1:27631`）。也可手动运行：
`python server/server.py --root runtime` 或 `server/start_bridge_server.bat`。

## 实机验证结果（2026-09-24，本机真实跑出来的）

| 项目 | 结果 |
|---|---|
| Blender 5.0.1 发送端 | ✅ 通过 |
| UE 5.8.3 接收端 | ✅ 通过 |
| Blender → UE（静态网格） | ✅ 1m 立方体 → UE 实测 **100×100×100 cm** |
| UE → Blender（反向） | ✅ 2×1×0.5m 长方体回 Blender 仍是 **2×1×0.5 m** |
| 骨骼分支 | ✅ SkeletalMesh + 3 根骨骼，无报错 |
| 增量更新 | ✅ 重复导入覆盖，不生成 `_1`，revision 递增 |
| 材质槽 | ✅ 2 个材质槽随模型过到 UE |
| 协议层回归 | ✅ 16/16 |
| QA 全链路 | ✅ 6 用例 / 65 断言全通过 |
| 贴图回传 → UE | ✅ 3 张 64×64 PNG → `Texture2D`，增量不生成 `_1`，与网格资产同树共存 |
| Blender → SP（SP 自动收下并开项目） | ✅ 实测 SP 自行认领并回执「SP 已接收」 |
| SP → UE（SP 自动烘焙 + 导出） | ⏳ 待 SP 重启加载新版插件后验证 |
| Maya 适配器逻辑（桩驱动打真服务） | ✅ 26/26（含 idle 节流、收发、异常路径） |
| Maya 实机（commandPort） | ⏳ 需在 Maya 里手动启动一次 |

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
python tests/profiles_sync.py            # 一致性自检（18 个关键字段 × 4 份）
```

## 快速开始

```bash
# 1. 启动服务（默认 127.0.0.1:27631，端口被占会拒绝启动）
python server/server.py --root E:/CirnoWorkSpace/AssetBridge/runtime

# 2. Blender：把 blender/addon/asset_bridge_blender 打包 zip，
#    Edit > Preferences > Add-ons > Install...

# 3. UE：unreal/scripts/init_unreal.py 挂到 Project Settings > Python > Startup Scripts

# 4. Maya：**不要**往 Documents/maya/<版本>/scripts/ 放 userSetup.py
#    —— Maya 只执行 MAYA_SCRIPT_PATH 里找到的第一个 userSetup.py，
#    再放一个同名文件会把用户自己的 userSetup（比如病毒防护）顶掉。
#    正确做法：把 maya/plugin/userSetup.py 的启动代码**追加**到
#    用户已有的 Documents/maya/scripts/userSetup.py 末尾；
#    或者每次在 Script Editor 里执行一行（幂等，可重复执行）：
#      exec(open(r"E:\\CirnoWorkSpace\\AssetBridge\\maya\\plugin\\userSetup.py").read())

# 5. SP：substance_painter/plugin/asset-bridge 拷到
#    Documents/Adobe/Adobe Substance 3D Painter/plugins/
#    ⚠️ SP **不做插件热重载**：改了 main.qml 必须重启 SP 才生效
```

## 自检

```bash
python tests/smoke_test.py                  # 协议层 16 项
python tests/profiles_sync.py               # 换算档案一致性（4 份 × 18 字段）
python tests/maya_stub_harness.py           # Maya 适配器逻辑（假 maya 模块 + 真服务）26 项
python tests/maya_commandport_verify.py     # Maya 实机（需先在 Maya 里启动适配器）
python tests/sp_qml_lint.py                 # SP 插件 JS 语法自检（重启 SP 前先跑）
python tests/sp_send_model.py               # Blender 发模型给 SP（SP 会自动开项目）
python tests/sp_to_ue_chain.py              # SP 烘焙 + 回传贴图 -> UE 收入（一条命令跑完）
```

## 注意事项

- **服务只监听 127.0.0.1**，协议无鉴权，跨机请走 SSH 隧道。
- 客户端全部强制绕过 HTTP 代理，否则公司网络下连本机地址会被劫成 502。
- Windows 上 `SO_REUSEADDR` 允许两个进程绑同一端口，所以服务启动前会先探端口，
  被占就拒绝启动 —— 避免「连的是旧实例、数据落在别处」这种极难查的问题。
- Maya 2023 的 mayapy / mayabatch 在这台机器上起不来（DLL 初始化失败，无桌面会话），
  所以走 commandPort 远程驱动，见 `maya/plugin/userSetup.py`。

详细设计见 [`ARCHITECTURE.md`](ARCHITECTURE.md)。
