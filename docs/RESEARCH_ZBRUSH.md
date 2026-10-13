# ZBrush 流转支持调研报告（第一阶段 · 纯知识调研）

- 日期：2026-10-13
- 结论受众：AssetBridge 托盘/插件架构（第 8 个 app，`zbrush`）
- 状态：**待用户确认方案后进入第二阶段**（本机装好后实测）。本文不含代码。

---

## 0. TL;DR

1. **ZBrush 2025.3 没有 Python API**。官方 Python（`zbrush.commands`，含 `-script` 命令行、
   免对话框导出）是 **ZBrush 2026.0.0（2025-09-10 秋季版）才引入的**；2025.3.0（2025-06-18）
   的更新说明全是修 bug。2025.3 时代只有 **ZScript + GoZ**。
2. 两个方向的可行性：
   - **ZBrush → 其他软件（发送）**：✅ 可做。2026 用 Python 一行 `set_next_filename()+press("Tool:Export")`
     免对话框导 OBJ；2025.3 用 ZScript 同款技巧（`FileNameSetNext` + `IPress Tool:Export`），
     以 ZPlugin 按钮手动触发。
   - **其他软件 → ZBrush（接收）**：⚠️ 有硬约束。ZBrush **同目录单实例**（第二个实例会弹
     "Sorry, only one ZBrush instance may be launched from the same directory" 后退出），
     已在运行时 Popen 传文件**不可靠**；且两代都没有「保存后自动回传」事件钩子。
     接收侧推荐 **守护进程投递 + 运行中降级为「文件落盘 + ZBrush 内手动加载」**。
3. 推荐 **双路线设计**：装了 2026.x 走 Python 路线（体验最接近 Blender 插件）；
   只有 2025.3 则走 ZScript 插件 + 守护进程路线（体验类似 PS/SAI2 但资源类型是 models）。
   架构层（中转目录 / revision / claim 去重 / APPS_META）全部直接复用。

---

## 1. 集成方式事实清单

### 1.1 三套可编程接口的版本矩阵

| 接口 | 2025.3 | 2026.0+ | 说明 |
|---|---|---|---|
| ZScript | ✅ | ✅ | 老牌脚本，ZPlugs64 装启动插件、Macros 录制；文件 IO 走内存块；`FileNameSetNext` 免对话框导出 |
| Python API | ❌ | ✅ | `zbrush.commands`；是 ZScript 命令的桥接封装，不能直接操作内部数据；解释器**常驻单会话**（脚本间共享状态） |
| GoZ | ✅ | ✅ | 仅官方适配 Maya/3ds Max/C4D/iPad（2025.1 起支持 Maya 2025/Max 2025）；**不支持自定义 app**，.ztn 格式未公开 |

### 1.2 Python API（2026.x）关键能力（官方文档/示例确认）

- **命令行调用**：`ZBrush.exe -script script.py arg1 arg2...`，脚本收 `sys.argv` 参数，
  可一次完成「打开工程 → 遍历 SubTool → 逐个导出」的批处理。
- **免对话框导出**：`set_next_filename("/abs/path.obj")` + `press("Tool:Export")` ≈ 0.08s、不弹窗；
  同法 `Tool:Save As` 存 .ztl、`Document:Export` 导画布。相对路径会解析到 ZBrush 安装目录，**必须用绝对路径**。
- **启动钩子**：`ZData/Python/init.py` 随启动执行（2026.1 起**安装目录只读**，用户内容挪到用户配置目录，
  init.py 的落点要在第二阶段实测）。
- **限制（官方明示）**：首波 Python 只是 ZScript 桥，不直接操作内部数据；无内建编辑器；
  FBX 插件会弹脚本关不掉的选项窗（社区 2025-03 确认）、USD 导出会忽略预设——**走 OBJ / .ztl 最稳**。
- **OBJ 导出语义**：只导**当前 SubTool**；无 UV 就不带 UV；头部带 `#Auto scale` 行（单位/缩放需第二阶段实测校准，
  对齐 profiles.json 的 global_scale=1.0 体系）。
- **线程/网络未知**：Python 是完整 CPython，理论上 stdlib（socket/requests）可用；但「常驻后台线程轮询 HTTP」
  是否存活（会不会被 ZBrush 主循环饿死/回收）**无官方说法**，列为第二阶段头号实测项。

### 1.3 ZScript（2025.3 可用）关键能力

- **启动插件**：`<安装目录>\ZStartup\ZPlugs64\` 放 `.zsc`（+ 同名 `.txt`——2025.2.2 修复说明显示
  **没有 txt 版会加载失败**，必须两个都装）→ 重启 ZBrush 出现在 Zplugin 菜单。
- **免对话框导出**：`[FileNameSetNext, path]` + `[IPress, "Tool:Export"]`（与 Python 同机制）。
- **无空闲事件、无定时器**：ZScript 只在被点击/加载时执行，跑的时候还会冻结 UI → **做不了常驻轮询**。
- 文件读取走内存块（MemCreateFromFile 等），写 manifest 文本可行但繁琐。

### 1.4 启动 / 文件关联 / 单实例

- 命令行/关联可开：`.zpr`（工程）、`.ztl`（ZTool）、`.zbr`；老版本实测 `.ztl/.txt/.zsc` 可直接作为
  `ZBrush.exe` 的参数传入。
- **同目录单实例**：已运行时再启动会弹错误框退出，**文件参数不会转交给已有实例**（无 DDE/单实例转发证据，
  第二阶段需实测 .ztl 双击在运行中的表现）。

### 1.5 安装与授权

- 安装路径（Win）：2025.x 为 **`C:\Program Files\Maxon ZBrush 2025\ZBrush.exe`**
  （旧 Pixologic 时代是 `C:\Program Files\Pixologic\ZBrush 4Rx\`，2026 为 `...\Maxon ZBrush 2026\`）。
- 注册表（实测样本来自 2025.0.1 / 2025.2.2 卸载信息）：
  - `HKLM\Software\Maxon\ZBrush <完整版本号>`（如 `ZBrush 2025.2.2`）
  - `HKLM\...\Uninstall\ZBrush <完整版本号>`（DisplayName/DisplayVersion/InstallLocation 齐全，
    托盘现有「卸载注册表」探测器可直接命中）
  - 文件关联 ProgID：`zbrush.tool.1` / `zbrush.project.1` / `zbrush.script.1`（MIME：application/maxon-zbrush-tool 等）
- 授权：2024 起纯订阅，经 **Maxon App** 登录激活（2 台机同时激活上限；45 天试用全功能）。
  无证据表明脚本/自动化会触发授权问题，但**Maxon App 必须保持登录**（macOS 有未登录崩溃先例）。

---

## 2. 两个方向的实现路径

### 2.1 接收（其他软件 → ZBrush：自动导入）

| 版本 | 方案 |
|---|---|
| 2026.x（首选） | A1：若后台线程实测存活 → `init.py` 起 worker 轮询桥的 claim 接口，拉到任务后 `press("Tool:Import")` 导入 OBJ/.ztl（**体验=Blender 级**）。A2（兜底）：daemon `Popen([ZBrush.exe, "-script", ab_import.py, file])` 一次性导入——**仅 ZBrush 未运行时可行**（单实例限制） |
| 2025.3 | daemon claim → 投递 OBJ/.ztl 到 payload 子目录 → ZBrush **未运行**时 `Popen([ZBrush.exe, file.ztl])` 直开；**已运行**时只落盘 + 托盘提示「ZBrush 内 Tool:Load 手动加载」。ZScript 无监听机制，做不到运行中自动导入 |

### 2.2 发送（ZBrush → 其他软件）

| 版本 | 方案 |
|---|---|
| 2026.x | ZPlugin 按钮跑 Python（常驻解释器，按钮创建后不销毁）或一次性 `-script`：`set_next_filename(payload) → press Tool:Export` → 写 manifest（纯 Python 文件 IO）→ daemon/发送端接管回传。若线程可用则可轮询「导出队列」 |
| 2025.3 | ZPlugs64 装 `.zsc+.txt` ZPlugin，按钮「AssetBridge: 发送」：`FileNameSetNext` → `IPress Tool:Export` → 内存块写 manifest。**手动触发**，无自动回传 |
| 两版通用兜底 | **热文件夹**：用户把导出的 OBJ 丢进 `runtime/zbrush_outbox/`，daemon 监听稳定后建 jobs——零插件，保底方案 |

### 2.3 「保存后自动回传」明确结论
- **做不到自动**。两代都没有保存事件钩子；ZScript 无空闲事件；Python 无（已证实的）后台回调。
  发送方向是「用户点按钮 / 丢热文件夹」的**手动触发**模型，这一点要在 UI 文案里写清楚，不承诺自动。

---

## 3. 与现有架构的对接点

- **app id**：`zbrush`（中转服务无 app 白名单，server 零改动）。
- **资源类型**：`models`（OBJ/.ztl）。贴图（法线/置换/Polypaint）暂不纳入，Multi Map Exporter 可后续加 P2。
- **直接复用**：中转目录与 revision 目录结构、claim 认领去重、manifest kind=models、托盘探测/安装卡片、
  `profiles.json` 唯一事实源（新增 `zbrush` 节点，global_scale 初值 1.0，**待第二阶段实测校准**后定稿）。
- **需新增**：`routes.zbrush.import/export` 配置；`editor_daemon.json` 增加 `zbrush` exe 路径
  （若走守护进程投递）；ZPlugin 源码目录 `plugins/zbrush/asset_bridge_zbrush/`。

---

## 4. 托盘安装器 $AB_APPS_META 字段建议

```powershell
@{ k='zbrush'; n='ZBrush'; s='ZBrush'; src='plugins/zbrush/asset_bridge_zbrush' }
```

- **探测方式**（多源，按现有探测器风格新增 `Find-ZBrush`）：
  1. 卸载注册表 `HKLM\...\Uninstall\ZBrush *`（DisplayVersion 即精确版本，InstallLocation 给安装根）；
  2. `HKLM\Software\Maxon\ZBrush *` 子键名；
  3. 固定路径 `C:\Program Files\Maxon ZBrush <年>\ZBrush.exe` 与旧 `C:\Program Files\Pixologic\ZBrush *\ZBrush*.exe`；
  4. 文件关联 ProgID `zbrush.tool.1` → exe（兜底）。
- **版本门槛**：`MIN_ZBRUSH` 初值建议 **2025.0**（ZScript 路线可用）；
  若确认走 Python 路线则提为 **2026.0** 并在 note 里标注「Python 需要 2026.0+」。
  版本号三段式（如 2025.3.0），现有 Parse-Version/Ver-GE 直接可用。
- **是否 noinstall**：**否**。ZPlugin（.zsc+.txt 或 .py）是真实文件安装（有装/卸载语义），
  与 PS/SAI2 的「只同步守护配置」不同；安装目标 = `<安装目录>\ZStartup\ZPlugs64\`（2026.1+ 若实测只读，
  改投用户配置目录，第二阶段确认）。
- **行内短标签/主按钮**：沿用现有机制，主按钮文案「安装 ZBrush / 更新 ZBrush」。

---

## 5. 风险与不确定项

| # | 风险 | 等级 | 缓解 |
|---|---|---|---|
| R1 | **2025.3 无 Python**——若坚持 2025.3，自动化的上限就是「手动按钮发送 + 关闭态接收」 | 高 | 决策点 D1：装 2026.x（订阅免费升）还是 2025.3 |
| R2 | 同目录单实例：运行中 Popen 投文件会弹错退出框 | 高 | daemon 投递前探测进程；运行中只落盘+提示 |
| R3 | 无保存/空闲事件钩子 → 无「保存后自动回传」 | 高 | 产品预期降为手动触发；热文件夹兜底 |
| R4 | 2026.1+ 安装目录只读，ZPlugs64/init.py 落点变化 | 中 | 第二阶段实测用户配置目录并写进安装器 |
| R5 | Python 后台线程/网络轮询存活未知（无官方说法） | 中 | 第二阶段第一优先实测；不存活则用一次性 `-script` 模式 |
| R6 | OBJ 只导当前 SubTool、单位/缩放未知、FBX 对话框关不掉 | 中 | 只走 OBJ；多 SubTool 循环导出；实测校准 global_scale |
| R7 | 授权依赖 Maxon App 登录；2 机激活上限 | 低 | 文档提示；不碰授权进程 |
| R8 | 2025.2.2 教训：ZPlugin 缺 .txt 不加载 | 低 | 安装器同时装 .zsc + .txt |
| R9 | 高模 OBJ 体积大（雕刻数亿面） | 低 | 发送前提示 Decimation；payload 清空按钮已有 |

---

## 6. 待用户确认的决策点

- **D1 版本路线**：装 **ZBrush 2026.x**（Python 路线，体验最好，推荐）还是坚持 **2025.3**（ZScript 路线）？
  订阅用户通过 Maxon App 升 2026 无额外费用。
- **D2 发送触发方式**：ZPlugin 按钮（推荐，一键）够用吗？还是必须研究「无感自动」（受 R3/R5 制约，可能做不成）？
- **D3 接收降级体验**：ZBrush 运行中收到模型时，接受「落盘 + 托盘提示手动 Tool:Load」吗？
  还是干脆只在 ZBrush 未运行时自动投递？
- **D4 贴图范围**：第一期只做 models（OBJ/.ztl），贴图后置——OK？

## 7. 装完后第二阶段要验证的本机信息（清单）

1. 实际安装路径、exe 文件版本、`HKLM\Software\Maxon\ZBrush *` 与 Uninstall 键的精确字段值。
2. ZScript 调色板里**有没有 Python 子面板**（有 → 2026 路线；没有 → 2025.3 路线）。
3. `ZBrush.exe -script test.py` 的一次性调用：ZBrush 未运行/运行中各一次（验证 R2、R5、退出码、stdout 去向）。
4. 若 2026：`init.py` 的实际加载位置（安装目录 vs 用户配置目录）+ 后台线程轮询 HTTP 能否存活 10 分钟。
5. `set_next_filename + Tool:Export` 导 OBJ：单位/缩放对 Blender 1m 的比值（校准 global_scale）、
   UV/MTL 是否随出、多 SubTool 循环导出的稳定性。
6. 若 2025.3：ZScript `FileNameSetNext+IPress` 导出是否同样免对话框；`.zsc+.txt` 装入 ZPlugs64 后重启加载确认。
7. ZBrush 运行中双击/`Popen .ztl` 的实际行为（转交已有实例？弹错？静默忽略？）。
8. Maxon App 登录状态对脚本化启动的影响（连跑 3 次不弹授权窗）。
9. `ZStartup\ZPlugs64\` 写权限（2026.1+ 是否只读）。
10. 大文件链路：≥200MB OBJ 走一遍 payload → manifest → 回传全流程的耗时与磁盘占用。

---

## 8. 补充调研 v1.1（2026-10-13，响应用户确认的 D1–D4）

> 决策记录：**D1 = ZBrush 2026.2.1**（Python 路线）；D2 = 按钮触发 + 热文件夹兜底，接受手动语义；
> D3 = 运行中降级为「落盘 + 托盘提示（带文件路径 + 一键复制 + 自动消失）」；D4 = 第一期只做 models，贴图为明确 TODO。

### 8.1 2026.2.1 与 2026.0 的 Python API 差异（D1 决策依据之一）

- **官方文档只发布到 `ZBrush Python SDK 2026.1`**（developers.maxon.net 仅有 2026_0_0 / 2026_1_0 两版），
  2026.2.x 尚无新版 API 文档 → **按 2026.1 文档写代码，并在适配器里做运行时特性探测兜底**
  （关键函数 `has_next_filename()` 等存在性检查，避免未来行为漂移直接崩）。
- **逐版核对 release notes（2026.0.1 → 2026.2.1），无新增/废弃 API 条目**，与桥接相关的变化只有：
  - **2026.1.0（破坏性，必须适配）**：目录重组——安装目录**只读**，用户内容统一迁到
    `C:\Users\<user>\AppData\Roaming\Maxon\ZBrush_[HashValue]\`，自定义内容（含插件）放
    `[用户资产目录]\ZStartup\`；官方**支持用环境变量指定用户目录位置**（安装器可注入固定路径，探测更稳）；
    同版附「updated Python SDK documentation zip」。→ 我们的 ZPlugin/init.py 安装目标**必须**用新目录，
    托盘安装器按 2026.1 分支选择目标路径。
  - **2026.2.0**：新增官方 Substance Bridge（"Send to Adobe Painter"）——官方自己也在做桥接，
    佐证我们方案的合理性；GoZ 独立安装器不再随包（GoZ 本体仍自动装）。
  - **2026.2.1（利好两条）**：① 修复 **VRML/STL/OBJ Extended 导入报错**（我们接收 OBJ 直接受益）；
    ② 修复「自定义 Python 脚本可能破坏自定义 UI 元素引用」（稳定性利好 ZPlugin 常驻按钮）；
    ③ Plugins and Documentation 改为**随 Maxon App / 独立安装器自动安装**。
- **授权模式与自动化（D1 决策依据之二）**：
  - 价格：订阅制 **$49/月 或 $399/年**（rental-only，含 iPad 版）；2024.0 起无永久授权。
    **活跃订阅升级到 2026.2.1 费用为 0**——即从原计划的 2025.3 直接装 2026.2.1 无额外成本。
  - 登录：经 **Maxon App** 登录激活（2 机同时激活上限）；官方有「macOS 未登录 ZBrush 启动崩溃」记录
    （2026.1 修复列表），Windows 未登录行为第二阶段实测。**适配器设计不触碰授权进程**，
    仅要求 Maxon App 保持登录；脚本化启动本身无授权弹窗证据。
  - 离线/多开：多开天然被单实例机制限制；离线运行未见官方禁止说明（授权缓存机制，第二阶段连跑验证）。

### 8.2 ZBrush 单位与 OBJ 缩放（补 §7 实测依据）

- **ZBrush 内部单位无量纲**（generic units），工具最佳工作区 ≈ 2×2×2 单位；OBJ/文件本身**不嵌单位**
  ——同一文件在 Maya(默认 cm) 是 2cm、Blender(默认 m) 是 2m。**不存在全局固定换算系数**。
- **导入方向（其他 → ZBrush）**：ZBrush 默认把 OBJ **自动缩放+居中到 ~2 单位**，并把还原所需的
  Scale/Offset 记录在 `Tool > Export`（Scale=0 时启用该自动调整）。⚠️ 陷阱：若当前已有活动 Polymesh
  且其 Export Scale 非 0，导入会按错误系数缩放——**适配器导入前确保无活动 Polymesh（或 Scale=0）**；
  且该值一旦改成非 0 无法改回 0（需重置）。
- **导出方向（ZBrush → 其他）**：ZBrush 按记录的 Scale/Offset 把 OBJ 还原为**进入 ZBrush 前的原始数值**
  （OBJ 头部有 `#Auto scale`/offset 注释行可校验）。即：**Blender 1m=1 单位进的模型，导出还是 1 单位**。
- **对接结论**：`routes.zbrush.export.global_scale` 初值 **1.0**，与现有体系一致；
  第二阶段用「Blender 1m 立方 → ZBrush → 导出 OBJ → 回 Blender」往返实测确认 1.0，
  并把「源 DCC 单位约定」写进 manifest（models 的单位语义由发送端声明，ZBrush 透传）。
  官方推荐管线：导出前用 **Scale Master（2026 支持真实单位 mm/cm/in）**锁定真实尺寸——
  写进用户文档，适配器不做自动缩放。

### 8.3 回退预案：2026.2.1 Python 不可用 → 2025.3 ZScript 路线（切换成本）

- **原则：做成「版本感知双路线」，按检测到的版本自动选路线，回退零重构。**
- 不变层（占总量 ~85%）：中转服务、daemon 认领/去重/manifest、安装器探测（注册表键两版一致）、
  `AB_APPS_META` 条目、profiles/routes 结构、托盘 UI。
- 需替换层：
  | 组件 | 2026.2.1（Python 路线） | 2025.3 回退（ZScript 路线） | 成本 |
  |---|---|---|---|
  | 发送端 | ZPlugin 跑 Python：`set_next_filename+press` | `.zsc+.txt` ZScript 按钮：`FileNameSetNext+IPress`，内存块写 manifest | 1 个适配文件 ~150 行 ZScript + 测试切片 |
  | 接收端 | daemon `Popen(-script ab_import.py)` / init.py 轮询 | daemon `Popen(file.ztl)`（仅未运行时）/ 落盘提示 | daemon 增加分支，~50 行 |
  | 安装目标 | `[用户资产目录]\ZStartup\`（2026.1+） | `<安装目录>\ZStartup\ZPlugs64\`（2025.3 可写） | 安装器按版本选路径 |
  | 版本门槛 | MIN_ZBRUSH=2026.0 | MIN_ZBRUSH=2025.0 | 常量 |
- **总切换成本 ≈ 0.5–1 天**；且两路线可共存（同 ZPlugin 名、按版本装不同脚本），不存在锁死。
- 触发条件（第二阶段实测任一失败即回退）：① `zbrush.commands` 导入失败；② `-script` 调用不执行/挂起；
  ③ 免对话框导出实际弹窗。

---
*信息来源：Maxon 官方文档/SDK 示例（developers.maxon.net、help.maxon.net）、ZBrushCentral、
Maxon 支持站版本说明、第三方卸载信息库（注册表实测样本）、Softpedia/Toolfarm changelog、
ZBrushCentral 缩放机制长文（GoZ/Blender/Max 缩放解析）、Polycount 单位讨论。*
*v1.1 补充：2026.1/2026.2.1 release notes（Softpedia、ZBrushCentral 2026 发布帖）、Maxon 支持站
《ZBrush 文件目录及用户资产结构重要变更（2026.1+）》、CG Channel 2026.1 报道（订阅价格）。*
