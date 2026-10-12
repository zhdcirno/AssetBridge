# Asset Bridge 架构设计

> 目标：让 Blender / Unreal / Maya / Substance Painter 之间**一键互发模型**，
> 并且带上材质槽、UV、骨架，保留变换与单位，重复发送时更新同名资产而不是堆 `SM_Chair_1`。

---

## 1. 总体结构

```
┌──────────┐  ┌──────────┐  ┌──────────┐  ┌───────────────────┐
│ Blender  │  │   UE5    │  │   Maya   │  │ Substance Painter │
│  (bpy)   │  │ (unreal) │  │ (cmds)   │  │  (JS / QML)       │
└────┬─────┘  └────┬─────┘  └────┬─────┘  └─────────┬─────────┘
     │             │             │                  │
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
              ~/.asset_bridge/payloads/<transfer_id>/
                            ├── SM_Chair.fbx
                            └── （贴图等）
```

**为什么是「本地中转服务 + 文件」，而不是点对点直连：**

| 方案 | 问题 |
|---|---|
| A 直接调 B 的 API（如 Blender 直接驱动 UE） | 要求 B 一定开着；A 里要写死 B 的版本适配；加一个软件要改 N 个插件 |
| 纯共享目录 + 约定文件名 | 没有通知机制，只能手动刷新；没有版本/状态，做不了增量更新 |
| **本地服务中转（本方案）** | 各软件只跟服务说话，彼此不知道对方存在；加一个新软件 = 加一个适配器，其它不动 |

服务**只处理元数据和小文件**，大文件走磁盘 / PUT 上传。这样即使插件崩了，资产文件还在，可以手动补导入。

---

## 2. 传输协议

全部 REST + JSON，无第三方依赖（DCC 内嵌 Python 里不一定有 `requests`）。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/v1/health` | 健康检查 |
| GET | `/api/v1/config` | 拿 payload 目录（同机直写要用） |
| POST | `/api/v1/sessions` | 注册一个 DCC 会话 |
| POST | `/api/v1/sessions/{id}/heartbeat` | 心跳，超时标记 offline |
| GET | `/api/v1/sessions` | 看当前谁在线 |
| POST | `/api/v1/transfers` | 建一条传输记录（草稿态） |
| PUT | `/api/v1/transfers/{id}/files/{name}` | 上传负载文件 |
| POST | `/api/v1/transfers/{id}/publish` | 发布 → 进入目标软件收件箱 |
| GET | `/api/v1/inbox?app=unreal` | 收件箱（按 app / project 隔离） |
| POST | `/api/v1/transfers/{id}/claim` | 认领（**409 防重复处理**） |
| POST | `/api/v1/transfers/{id}/finish` | 回执（done / error） |
| POST | `/api/v1/transfers/{id}/retry` | 失败重投 |
| GET | `/api/v1/transfers` | 历史记录 |

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

| 软件 | Up 轴 | 前向 | 单位 |
|---|---|---|---|
| Blender | Z | -Y | m |
| Unreal | Z | X | cm |
| Maya | Y | Z | cm |
| Substance Painter | Y | Z | cm |

- **Blender → UE**：两端都 Z-up 无需换轴；m→cm 交给 UE 的 `Convert Scene Unit`，
  所以导出端 `global_scale = 1.0`（在导出端乱乘 100 是社区里最常见的错误）。
- **UE → Blender**：必须 `import global_scale = 0.01`，否则模型放大 100 倍。
- **Maya ↔ UE**：同为 cm 不缩放，Y-up↔Z-up 交给 UE 的 `Convert Scene`。
- **Blender → SP**：SP 是 Y-up/cm，需 `global_scale = 100` 且显式转 Y-up。

> ⚠️ FBX 的单位/轴向语义在 Blender 各版本间有过调整（4.x 移除了部分轴向参数）。
> 上面是默认值，**第一次接入新版本请先做一次校准**：导出一个已知 1m 立方体，
> 在目标软件里量一下，把结果写回 `profiles.json`。Blender 侧用 `io_fbx._call()`
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

| 软件 | 机制 | 说明 |
|---|---|---|
| Blender | `bpy.app.timers` | 子线程碰 `bpy` 会崩，所以全在 timer 里同步做 |
| UE | 后台 `threading.Thread` 轮询 + `register_slate_post_tick_callback` 执行 | 资产操作必须在游戏线程 |
| Maya | `cmds.scriptJob(event=["idle", ...])` | 主线程空闲时执行，天然安全 |
| SP | `PainterPlugin.onTick`（`tickIntervalMS`） | QML 主线程 |

另外：**所有客户端都强制 `ProxyHandler({})` 绕过代理**。公司网络里 `HTTP_PROXY` 会连
`127.0.0.1` 也劫走，表现是 `502 upstream connect failed`，排查起来非常费时间。

---

## 5. 各适配器安装

### 服务（必须先跑）
```bash
python bridge_server/server.py --root ~/.asset_bridge --port 27631
```

### Blender
把 `adapters/blender/asset_bridge_blender` 整目录打包成 zip →
`Edit > Preferences > Add-ons > Install...`。侧栏 `Asset Bridge` 面板里填服务地址即可。

### Unreal
`adapters/unreal` 拷进项目的 `Content/Python`，然后在
`Project Settings > Python > Startup Scripts` 加 `init_unreal.py`；
或在 Output Log 的 Python 命令行里直接执行它。

### Maya
`adapters/maya/asset_bridge_maya` 放进 `MAYA_SCRIPT_PATH`，`userSetup.py` 里加：
```python
import maya.utils
maya.utils.executeDeferred(lambda: __import__("asset_bridge_maya").start())
```

### Substance Painter
`adapters/substance_painter/asset-bridge` 整个文件夹拷到
`Documents/Adobe/Adobe Substance 3D Painter/plugins/`（老版本是 `Allegorithmic` 目录）。

---

## 6. 当前进度 & 待办

| 模块 | 状态 |
|---|---|
| 中转服务 | ✅ 完成，16 项冒烟测试全通过 |
| Blender 适配器 | ✅ 骨架完成（发送 / 接收 / 面板 / 定时器） |
| UE 适配器 | ✅ 骨架完成（自动导入 / 摆放 / 回发） |
| Maya 适配器 | 🟡 骨架完成，未实机验证 |
| SP 适配器 | 🟡 骨架完成，部分 API 需按本机版本核对（见代码 `[待本机验证]`） |

下一步建议（按性价比排序）：
1. **实机跑通 Blender → UE**，并做一次单位校准，把结果固化进 `profiles.json`。
2. 加**贴图/材质回传链路**（SP → UE），走 `kind=textures`。
3. 加**USD 通道**（UE5.4+ 支持），比 FBX 更适合带复杂材质和变体。
4. 写一个常驻托盘/CLI（`bridge status` / `bridge watch`），让服务不用手动起。
5. 资产冲突策略（同名但结构不同时是覆盖还是并存）目前是硬覆盖，需要加提示。

## 7. 已知坑

- Blender 4.x 的 FBX 导出器移除了部分轴向参数 —— 已用 RNA 反射兜住，但**单位语义仍需校准**。
- SP 的 JS API 版本差异较大，`alg.project.create` / `alg.mapexport.save` 的参数签名需按本机文档核对。
- UE 的 `FbxImportUI` 在 5.x 各小版本间属性名会变，用到 `set_editor_property` 的地方建议包 try。
- 服务默认只监听 `127.0.0.1`。要跨机请用 SSH 隧道，不要直接 `--host 0.0.0.0`（协议目前无鉴权）。
