# AssetBridge 交接文档（HANDOVER）

> 产出时间：2026-10-14 11:35（上一个 WB 对话结束前产出；上下文耗尽交接）
> 唯一事实源：本文 + `.workbuddy/memory/2026-10-13.md`（逐小时日志）+ `2026-10-14.md`
> 约束重申：**不 push、不打 tag，全部由用户定**。

---

## 1. 项目现状

- **项目名**：AssetBridge（8 软件互转中转系统）
- **根路径**：`E:\CirnoWorkSpace\AssetBridge`（唯一事实源；C 盘旧副本已删）
- **仓库 URL**：`github.com/zhdcirno/AssetBridge`（public，remote 用 SSH：`git@github.com:zhdcirno/AssetBridge.git`）
- **中转服务**：`http://127.0.0.1:27631`，数据根 `runtime\`；**curl 必带 `--noproxy '*'`**（本机代理劫持回环→502）；健康路由 `/api/v1/health`；只监听 127.0.0.1 无鉴权；服务不跑会拖卡四个 DCC
- **已支持 app（8 个）**：Blender / Maya / UE / Substance Painter / Toolbag / Photoshop / SAI2 / ZBrush（PS/SAI2/ZBrush 为"编辑器"角色：收文件→人工编辑→保存后由守护回传）
- **适配器目录（仓库相对路径）**：
  - 托盘（中枢，8 软件装卸+守护）：`tray/assetbridge_tray.ps1`（+ `tray/run_server.py`、`tray/icons/`）
  - 中转服务：`server/server.py`
  - Blender：`plugins/blender/asset_bridge_blender/`
  - Maya：`plugins/maya/asset_bridge_maya/`
  - UE（引擎级装法）：`unreal/project/AssetBridgeTest/Content/Python/asset_bridge_unreal/`（仓库源）→ 部署到 `G:\Engine\UE_5.8\Engine\Plugins\AssetBridge\`
  - Toolbag：`plugins/toolbag/asset_bridge/`（`__main__.py`）→ 部署到 `D:\DCC\Toolbag 5\data\plugin\asset_bridge\`
  - ZBrush：`plugins/zbrush/asset_bridge_zbrush/` → 部署到 `C:\Users\12069\AppData\Roaming\Maxon\Zbrush2026.2.1_01AC8E5C\ZStartup\ZPlugs64\asset_bridge_zbrush\`
  - 编辑器守护（PS/SAI2 收发 + ZBrush 投递/回传）：`tools/editor_daemon.py`（配置 `tools/editor_daemon.json`）
  - 托盘安装器/图标工具：`tools/gen_icons.py`、`tools/gen_tray_action_icons.py`
  - 测试（纯标准库）：`tests/test_tray_functions.py`

## 2. git 状态（2026-10-14 11:35 实测）

**未 push 提交（`git log --oneline origin/main..HEAD`，7 个，从新到旧）**：

```
75d8baa fix: 切片生成器常量块 head+tail 双剔除——根治 job 内 MyInvocation 推导崩溃
0cc3eb0 fix: 托盘 $pid 撞只读自动变量 + 测试编码 Windows GBK 适配
eeea72e diag: add runspace state-task diagnostic script for real-terminal reproduction
a9a43e9 fix: 后台任务机制更换——runspace 池（state 卡死 30s）改为 Start-Job 独立进程
4b386d0 refactor: 解耦重构——ZBrush 启动零阻塞不卡死 + 托盘全异步（修复验收阻塞）
38878f3 fix: 托盘启动崩溃——ContextMenuStrip 无 add_Popup 事件，改用 add_Opening
d92cc87 feat: ZBrush 2026.1+ 流转支持（第 8 个 app，Python 适配器 + 守护进程握手）
```

每个一句话说明：
- `d92cc87`：ZBrush 第 8 个 app 接入（Python 适配器 + 守护握手 + 热文件夹 + 托盘气泡），用户侧验证时发现卡死 → 引出 4b386d0
- `38878f3`：托盘右键菜单崩溃修复（ContextMenuStrip 没有 add_Popup 事件，PS5.1 运行时 MethodNotFound，ParseFile 抓不到）
- `4b386d0`：ZBrush autostart 零阻塞重构 + 托盘 state 轮询改后台任务。**引入回归**：job 机制此后的连环坑（见 §7）
- `a9a43e9`：后台任务从 runspace 池（state 卡死 30s）换 Start-Job 独立进程。**引入回归**：job 环境差异连环雷（MyInvocation/序列化/$args），10-13 深夜才修完
- `eeea72e`：`tools/diag_runspace_state.ps1` 诊断脚本（runspace 时代产物，现在的 Start-Job 架构下用途降低，保留）
- `0cc3eb0`：托盘 `$pid = 0` 撞只读自动变量 $PID（启动即崩）→ 改名 $srvPid；test_tray_functions.py GBK stdout 两处编码修复
- `75d8baa`：**异步链根治**——切片常量块 head+tail 双剔除、EndBlock 只留纯变量赋值（AST 判型）、job 内 `[scriptblock]::Create` 还原 work、`$args`→`$workArgs`。**已实机验证全绿**（callback ok=True）

**`git status --short`（未提交改动，4 文件）**：

```
 M plugins/toolbag/asset_bridge/__main__.py
 M plugins/zbrush/asset_bridge_zbrush/asset_bridge_zbrush.py
 M tools/editor_daemon.py
 M tray/assetbridge_tray.ps1
```

- `tray/assetbridge_tray.ps1`（+20/−5）：P0-1 修复 `$script:ManualStop`（用户手动停止后守护不再自动拉起）——**未实机验证**
- `tools/editor_daemon.py`（+20）：P0-2 诊断日志 4 处（watch-open / finalize saved / return_of 选项 / return transfer 目录）——**未实机验证**（需重启守护生效）
- `plugins/zbrush/.../asset_bridge_zbrush.py`（+41）：P0-3 诊断埋点 `diag_log()`（autostart 五步 / import_pending 逐任务 / _import_one 七步 / auto_loop 每圈）——**未实机验证**（需重启 ZBrush 生效；已同步 AppData 安装路径）
- `plugins/toolbag/asset_bridge/__main__.py`：P1-7 英文化（42 处运行时文案）——**未实机验证**（已部署 D:\ 安装目录）

**tag**：`v2.0` 已打在 9bbb15f 并已推远端；**GitHub Release 未建**（本机无 gh CLI，用户网页手动建）。当前 `v2.0-7-g75d8baa`。

**push 时机**：由用户定。下一个对话不得主动 push。

## 3. 本次对话（10-13 ~ 10-14）完成的事与踩坑

时间线（详见 `.workbuddy/memory/2026-10-13.md` §5-16、`2026-10-14.md` §1）：

1. **0cc3eb0 之前（P0 真托盘崩）**：产品文件 `assetbridge_tray.ps1:677` `$pid = 0` 在函数内撞只读自动变量 `$PID`（PS 变量名不区分大小写）→ 启动即崩 VariableNotWritable。前几轮只修了测试切片生成器没修产品文件。修复：改名 `$srvPid`。坑：PS 变量大小写不敏感；`Get-StateData` 是后台 state/冒烟必经之路所以一崩全崩。
2. **0cc3eb0（P1 测试编码）**：test_tray_functions.py 两个 bug——`print()` 撞 GBK stdout（UnicodeEncodeError）；PS 子进程 stdout 按 GBK 编码而 python 按 UTF-8 解 → 「锟斤拷」。修：`sys.stdout.reconfigure(utf-8)` + 子进程注入 `[Console]::OutputEncoding=UTF8` + `ps_decode()` 双保险。坑：**锟斤拷是解码层丢数据，打印前 encode/decode 救不回来，必须从子进程源头修**。
3. **实机验证 0cc3eb0 通过**：托盘无红字进消息循环；`ed_status_text` 剩余乱码被定性为 PS5.1 控制台**显示层**（数据已正确），可选 `chcp 65001` 不做。
4. **异步链连环雷（75d8baa，跨越 5 轮实机反馈）**——a9a43e9 换 Start-Job 后 job 从未真正跑通过，层层剥：
   - L1：head 常量块 `$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition` 在 job 里 **`$MyInvocation.MyCommand.Definition` = job 脚本块全文**（非路径！）→ Split-Path 产出垃圾/报错 → EAP=Stop 下 IEX 中断 → 注入循环从未执行。实机报「找不到提供程序」@切片 L32 / 早期变体「Path 空串」@L33。
   - L2：常量块在切片里有**两份**（head 段 + EndBlock tail 段——AST FindAll/EndBlock 追加会原样塞回）→ 只剔 head 不够。验证盲区教训：**只复算 head 的"剔除验证"无效，必须扫完整切片**。
   - L3：EndBlock 尾部的 UI 属性赋值（`$miState.Enabled = $false` 等）在 job 里目标是 $null → EAP=Stop 必炸 → 改为 **AST 判型只保留纯变量赋值**（`VariableExpressionAst`）。
   - L4：**scope 遮蔽**（沙箱实验实证）：IEX 顶层赋值写当前 scope；IEX 定义的函数词法父 scope = job 脚本块 scope → `Set-Variable -Scope Script` 注入被切片自己的同名顶层赋值**完全遮蔽**。修：注入改 **IEX 之前 + 当前 scope**，且切片剔除同名赋值。
   - L5：**scriptblock 不能跨 Start-Job 边界**（PSRemoting 序列化只剩文本）→ `& $sb` 把文本当命令名 → `param($sbText)` + `[scriptblock]::Create([string]$sbText)` 还原。
   - L6：**`$args` 是自动变量，`param($args)` 不绑定位置参数** → workArgs 静默丢失（port 为空实证）→ 改 `$workArgs` + `& $sb @workArgs`。
   - 沙箱复现基建：`C:\Users\12069\AppData\Local\Temp\ab_sim3.ps1`（完整复刻切片生成器 + Start-Job + 真实切片）、`ab_slice_dump.txt`（完整切片文本）、`ab_scope_test.ps1`（作用域微实验，counter=6 证明作用域链正常）。**sim3 与产品生成器是复刻关系，改生成器必须同步 sim**。
   - 埋点日志：`tray_job_trace.log`（job 侧 Trace-Job）+ tray.log 的 btn=... click/dispatched/callback 链。最终实机全绿：callback ok=True。
5. **夜间验收批（10-13 深夜 → 10-14 上午，见 §4/§5）**：P0-1 ManualStop 修复 + P0-2 daemon 诊断日志 + P0-3 ZBrush diag_log + P0-4/5 定位 + P1-6/7 Toolbag 定案。
6. **Toolbag 定案（10-14）**：官方文档证实 Toolbag 插件**不是启动自动执行**——是 Edit→Plugins 菜单里的可运行脚本；自动加载正解 = 官方 boot argument `toolbag.exe "脚本路径"`。已做英文化（42 处）+ 部署 D: + 一键启动器 .bat（COM 建 .lnk 被沙箱拦）。

## 4. 当前未解决 / 待办清单

### P0-1 桥接停止后自动重启 —— 已修待实机验
- 改动：`tray/assetbridge_tray.ps1`（未提交）。`$script:ManualStop` 标志：stop 置位 / start、restart、Guard 恢复清除；`Get-StateData` 条件 `-and -not $script:ManualStop`（抑制时打日志"用户手动停止，守护不拉起"）；图标 warn→down、气泡文案同步；flag 经 `$consts` 注入 job（`MANUAL_STOP`），`constRe` 加 `ManualStop` 防切片尾部 `$script:ManualStop = $false` 覆盖注入值（与 L2 同款坑）。
- **实机步骤**：重启托盘 → 状态窗/菜单点【停止】→ 服务应保持停止、图标红色（不再琥珀）、日志出现"用户手动停止，守护不拉起"；点【启动】恢复。

### P0-2 编辑器守护回传失败（PS + SAI2 同款）—— 诊断日志已埋，等实机日志
- **链路真相**（已定性）：回传 = 守护**新建一条传输**（新 rtid → 新 payload 目录 `payloads\<rtid>\`），带 `options.return_of=原tid` + `source_files`（原文件名列表）发给 `from_app`；UE 端 `reload_textures` 按 **`source_files[i]`→`asset_paths[i]` 名字映射**原地覆盖 Texture2D。**两个 payload 目录不同是设计使然**（原传输 91750fe vs 回传 b5fba6cc），不是 claim 拆分。
- 疑点（等日志定位）：用户截图 PS 编辑 `T_SM_SpCube_Tex.png`（91750fe）而 UE 收 `M_SpA_Base_color.png`（b5fba6cc）——**文件名对不上**，疑似多批发送/跨传输混淆。
- SAI2 两个**硬缺口**（已知，未修）：① 另存到**别的目录**收不到（`Watch.dir` 固定监视原 payload 目录，只扫同目录新图）；② 保存 **.sai2 原生格式**不触发（`IMAGE_EXTS` 只认 png/psd/tga/jpg/bmp/exr/tif）。
- **日志埋点位置**：`tools/editor_daemon.py`——`handle()` 末尾 `[watch-open] tid/editor/return_to/dir/files/orig_options(asset_paths,source_files)`；`finalize()` 三条 `[finalize] saved=... src_paths=...`、`[finalize] return_of=... to_app=... source_files=... asset_paths=...`、`[finalize] return transfer rtid=... dest=...`；`unreal/.../__init__.py` textures 分支 `[bridge] return_of=... source_files=... asset_paths=... arrived=...`（已同步引擎目录）。
- **实机步骤**：① 重启编辑器守护（托盘菜单停/启）+ 重启 UE；② UE 发一张贴图 → PS 打开 → **原文件覆盖保存** → 收集 `runtime\editor_daemon\daemon.log` 新增 + UE Output Log `[bridge]` 行；③ SAI2 同样走一遍。
- **预期日志判读**：`source_files` / `asset_paths` / `arrived` 三者文件名一致 = 映射应成功（若还不重载查 UE 端 import 环节）；不一致 = 抓到断点（大概率是跨传输混淆或保存改名）。

### P0-3 ZBrush 自动接收卡死 —— 诊断埋点已加，等实机日志
- 埋点：`plugins/zbrush/.../asset_bridge_zbrush.py` 的 `diag_log()` → 独立写 `<payload_dir>/zb_bridge_diag.log`（毫秒时间戳）。埋点：autostart 五步（enter / install_ui before/done / start_watcher before/done）、import_pending 逐任务逐文件、`_import_one` 七步（**含 `press Tool:Import` 前后各一条**——若卡死日志停在 press before 即实锤 press 阻塞）、auto_loop 每圈耗时。
- 已同步 AppData 安装路径（需**重启 ZBrush** 生效）。
- **实机步骤**：重启 ZBrush → 复现卡死（点「导入待收模型」或自动接收）→ 强退 → 把 `zb_bridge_diag.log` 贴出来 → **最后一条日志 = 卡死点**。
- 已有怀疑排序：① install_ui 的 add_subpalette/add_button 在 ZBrush 启动早期；② `press("Tool:Import")` 遇 ZBrush 忙/原生对话框；③ auto_loop 主线程忙循环（实验功能，默认关）。4b386d0 的零阻塞重构用户侧从未验证过。

### P0-4 Blender/Maya/UE 缺「发送到 ZBrush」—— 定位完成，直接改
- 纯 UI 注册遗漏，协议层就绪（daemon `handle_zbrush` 已收 kind=models）。
- 要改的三处：
  - Blender `plugins/blender/asset_bridge_blender/__init__.py:57-67` `target_app` EnumProperty items 加 `("zbrush", "ZBrush", "发送到 ZBrush（模型）")`；`ui.py:72` 的贴图专属分支注意 zbrush 走模型分支（zbrush 只收 models）；`operators.py` 发送逻辑放行 zbrush（kind=models，from_app=blender）。
  - Maya `plugins/maya/asset_bridge_maya/__init__.py:1310` optionMenu 列表加 "zbrush"；模型发送路径放行（贴图发送 696 行已限定 photoshop/sai2，别动）。
  - UE `unreal/.../ui.py` 加 "Send to ZBrush" 按钮 → `send_selected_assets('zbrush')`（需确认 send_selected_assets 的 target 校验白名单在哪放开）。
- 改完同步部署：UE→引擎目录；Blender/Maya→用户安装路径（`%APPDATA%\Blender Foundation\Blender\{5.0,5.2}\scripts\addons\`、`Documents\maya\2023\zh_CN\scripts\`）——**Blender 5.0/5.2 两份都要同步**。
- 验证：三个软件面板各出现 ZBrush 目标 → 发一次 → ZBrush 收（ handshake 或落盘+托盘提示）。

### P0-5 ZBrush 无法选发送目标 —— 方案已定，直接改
- 现状：`install_ui()`（asset_bridge_zbrush.py:612）三个按钮：发送→`cfg["default_to"]`（ab_config.json，默认 blender）/ 回发 `_STATE["last_from"]` / outbox 回传。
- **推荐方案（已选）**：ZBrush API 只有 add_button 没下拉 → **每个常用目标一个按钮**（`发送->Blender`、`发送->UE`、`发送->SP`），改 `install_ui` 加按钮 + `on_send_current` 带参化；`回发上次发送者` 保留作兜底。
- 注意：改完同步 AppData 安装路径 + 重启 ZBrush。

### P1-6 Toolbag 无反应/无面板 —— 已定案，剩实机验证
- 诊断结论：插件代码 OK（手工 exec 面板+回调全正常）；**Toolbag 5.00b1 不自动执行 data\plugin 下插件**——官方模型是"菜单可运行脚本"（Edit→Plugins 菜单选中才跑）。
- 已落地：英文化（见 P1-7）+ 部署 D: + 启动器 `桌面\Toolbag5-AssetBridge.bat`（boot argument 自动运行）。
- **实机步骤**：双击 .bat 启动 → 面板出现 + Console `[bridge] Toolbag adapter loaded` → Blender 发模型 → 面板收到 → 烘焙 → 自动回传 SP。
- 待查方向（若 .bat 方式仍无输出）：Toolbag Console 全文（mset 加载层报错）；boot argument 下 `__file__`/工作目录是否异常（`config.json` 相对路径解析）。

### P1-7 Toolbag 中文乱码 —— 已修（英文化），待实机
- 已做：42 处运行时文案英文化（UIWindow/UILabel/UIButton/_log/_set_status），校验 0 中文残留；`_finish` 给服务端的消息保持中文（显示在 Blender/UE 面板，无碍）；py_compile 通过；已部署 D:。
- 实机：见 P1-6 步骤，面板文字应清晰可读。

## 5. 环境事实（接手方别踩坑）

- **用户机器 PATH 上无真 Python**：`where python` 只命中 WindowsApps 占位符（0.0.0.0），无 `py`。真 Python：`C:\Users\12069\.workbuddy\binaries\python\versions\3.13.12\python.exe`（运行仓库测试用它；仓库测试纯标准库零 pip）。仓库测试**在沙箱跑过、用户侧从未跑过**。
- **ZBrush 2026.2.1**：exe `D:\DCC\Zbrush2026.2.1\ZBrush.exe`；内嵌 CPython **3.11.9**（与系统 Python 无关）；用户资产目录 `%APPDATA%\Roaming\Maxon\Zbrush2026.2.1_01AC8E5C\`（**小写 b**，正则用 `[Bb]rush`）；插件安装路径 `...\ZStartup\ZPlugs64\asset_bridge_zbrush\`；自动加载钩子 `D:\DCC\Zbrush2026.2.1\ZData\Python\init.py`。`zbrush.commands` 只能主线程调（GIL 在主线程）。
- **Toolbag 5.00b1**：`D:\DCC\Toolbag 5\toolbag.exe`；内嵌 Python **3.12**；插件目录 `D:\DCC\Toolbag 5\data\plugin\asset_bridge\`（自带插件 Examples/Fun 等）。插件模型 = Edit→Plugins 菜单可运行脚本；自动运行靠 boot argument。
- **桥接服务**：进程 `pythonw.exe`（不是 powershell），日志 `logs\server_console.log`；托盘日志 `logs\tray.log`；编辑器守护 `tools/editor_daemon.py`（PID/状态/日志在 `runtime\editor_daemon\`：daemon.pid / daemon.log / status.json / notify.json）。
- **沙箱陈旧 FS 视图**：agent spawn 的 powershell 子进程可能读到**旧文件**；python 直读才真实；PowerShell 工具会话自己的视图基本可信。**所有"冒烟通过"必须用户真实终端复验才算数**。
- **PowerShell 工具会话执行策略 Restricted**：`& file.ps1` 被静默拦截（退出码 1 零输出）→ 同命令内先 `Set-ExecutionPolicy -Scope Process Bypass -Force`；stdout 常被吞 → 必须 `Out-File` 落盘再 Read；EncodedCommand 被拦；COM（WScript.Shell 建 .lnk）被拦。
- **改 .ps1 铁律**：BOM 必须保持 EF BB BF（Edit 工具保留；验证用 python 读原始字节）；**ParseFile 0 错误 ≠ 能跑**，运行时错误（MethodNotFound/VariableNotWritable）必须实机跑。
- **Win11 托盘图标默认折叠进隐藏区**：用户没看到图标 ≠ 进程没起。
- **bash 陷阱**：双引号里 `\$pid` 被吞成 `$`（正则锚点）导致 grep 漏报——复查要用 `\\\$pid`。
- **curl 必带 `--noproxy '*'`**；UE API 只能游戏线程；Maya commandPort(7001) 必须读气回显再关 socket；daemon 改动需重启守护、UE 插件改动需同步引擎目录+重启 UE、ZBrush 插件改动需同步 AppData+重启 ZBrush、Blender 5.0/5.2 两份安装都要同步。

## 6. 关键决策（别推翻）

- **SSH 替代 PAT**（旧 PAT 已吊销）；git 一律 SSH remote。
- **SemVer**；稳定点 agent 打附注 tag 并 push（`tag -a vX.Y.Z`）；**Release 用户网页手动建**（本机无 gh CLI）。
- **拒绝伪自动化**：ZBrush 回传走手动按钮 + outbox 热文件夹兜底；守护绝不杀用户的编辑器。
- **托盘 UI**：每软件一行 `[主按钮 190×28][指定📁 34×28][卸载🗑 34×28]` + 行尾 120px 状态标签；窗口 560×648。
- **ZBrush 路线**：D1=2026.1+（Python API 路线）；D2=按钮触发 + 热文件夹兜底；D3=运行中降级落盘+托盘气泡提示；D4=第一期只做 models。
- **新规矩**：P1 体验问题不许牵动架构，只改有问题的函数。
- **push / tag / Release 时机全部由用户定**；agent 不主动 push。
- Toolbag 文案英文（Toolbag 对非 ASCII 支持不完整）；其它软件保持中文。

## 7. 本轮教训（已写入项目 MEMORY，接手方必读）

- **ParseFile 抓不到运行时错误**（MethodNotFound / VariableNotWritable / VariableNotWritable 类）——改 .ps1 必须实机真跑，验收不看"0 错误"。
- PowerShell 工具会话 Restricted：`& file.ps1` 静默拦截，必须同命令 Bypass；stdout 吞掉要 Out-File。
- 用户机器 PATH 无 Python；仓库测试从未在用户侧跑过。
- **job 内函数词法父 scope = job 脚本块 scope**：切片顶层同名赋值会**遮蔽**任何 `-Scope Script` 注入（沙箱实验 counter 实证）。注入必须 IEX 前 + 当前 scope，且切片剔除同名赋值。
- **`$MyInvocation.MyCommand.Definition` 在 job 里 = job 脚本块全文**（非路径非空）——任何靠它推导路径的代码在 job 里必炸。
- **scriptblock 跨 Start-Job 边界只剩文本**——`[scriptblock]::Create` 还原。
- **`$args` 是自动变量不绑位置参数**——job 参数名用 `$workArgs`。
- **切片常量块有两份**：head 段（原始文本）+ tail 段（EndBlock 追加），两处都要剔；EndBlock 只留纯变量赋值（`VariableExpressionAst` 判型）。
- **"剔除验证"必须扫完整切片**（本对话最大验证盲区：只扫 head 漏了 tail）。
- **沙箱"通过"不算证据**——spawn 子进程有陈旧 FS 视图；用户实机才是最终裁判。沙箱复现基建（sim3）很有用但要与产品代码同步。
- 诊断日志优先于猜测：多轮连环雷全靠 btn=/Trace-Job/trace 埋点逐层定位。

## 8. 下一步（接手方开场第一件事）

1. **收用户实机日志**（按 §4 各项实机步骤）：P0-1 托盘停止行为、P0-2 `daemon.log`+UE `[bridge]` 行、P0-3 `zb_bridge_diag.log`。拿到日志按判读表定位，再给对应修复 diff（先诊断后修的流程不变）。
2. **可直接改**（方案已定、不用再问）：P0-4 三端加 zbrush 目标（记得三端部署同步 + Blender 双版本）；P0-5 ZBrush 每目标一按钮；P1-7 已改完待验。
3. **P1-6**：方案已落地（.bat boot argument）；若实机仍无输出再查 Toolbag Console 报错 / boot 模式下相对路径。
4. **全部修完 → push 所有未 push 提交 + 打 v2.1 tag + 用户手动建 GitHub Release**（v2.1 内容 = ZBrush 支持 + 异步链修复 + 本轮全部 P0/P1）。
5. **单独一轮评审**：4b386d0 的全盘异步化是否过度设计（state 轮询本可以留在 UI 线程有界同步——见 tray ps1 2604 行注释；现在的 Start-Job 机制值得留，但 state 同步采集已回归，评审确认无死代码/无重复机制）。
6. 接手时先读 `.workbuddy/memory/2026-10-13.md`（16 节逐小时日志）+ `2026-10-14.md`，本文档是索引不是替代。

---

**文档完。当前工作区 4 个未提交文件全部是"已落地待实机验证"的诊断/修复，不要 revert，不要 push。**

## 
## 2026-10-27 收尾记录（v2.1 封版后）

### 已 push 到 origin/main（HEAD = 0b3ccd2）

从 f8bfe4f（v2.1-verified）起，共 13 个 commit 已进远端：

功能/修复（10 个）：
- 0f8acf9 chore: .gitignore 补充 plugins/zbrush 日志与 _repro_capture_run1 忽略规则
- 13818d6 fix(server): 客户端断连不再当作 500 刷日志
- 5900fde feat(install): 新增 ZBrush 适配器安装并修复配置路径对齐
- 7633041 feat(blender): 支持与 ZBrush 互传 OBJ 模型
- 2735256 feat(maya): 支持与 ZBrush 互传 OBJ 模型
- 3e76d20 fix(toolbag): 烘焙模型导入同时进主场景与 Baker 槽位；UI 布局与本地化
- 3d00e9d feat(unreal): 支持发送 ZBrush OBJ + 回传计时诊断 + Windows 路径修复
- 47d143c docs: 对齐 README/ICONS/USAGE 表格
- 0655f78 fix(tray): 保存流转设置后清除 30s 缓存，修复重开显示旧值
- d9dc391 fix(maya): 发送端统一注入 from.app，修复回传被拒

文档（3 个，含一次 revert 修复）：
- 074b02a docs: 补充 v2.1 已知限制（此 commit 引发全文行尾重排，diff 251/307 为假删改）
- 3d15dab Revert "docs: 补充 v2.1 已知限制（...）"
- 0b3ccd2 docs: 补充 v2.1 已知限制（纯追加，撤销 074b02a 行尾重排）

### 真机验证结论

- Blender → ZBrush：通过
- Unreal → ZBrush：通过（[bridge][timing] 分段日志出现）
- Maya → ZBrush：通过（transfers seq 390，from={"app":"maya","version":"2023"}，status=done）
- Toolbag：部分通过。模型已导入主场景、Baker 组已创建，但模型不会自动加入组，需手动拖入/关联。bake 另因输出路径未配置失败（seq 388），非适配器缺陷。
- ZBrush 接收：默认手动触发（点「导入待收模型」），自动轮询为实验功能、默认关闭。13:10:44 那次为手动触发，符合 v2.1 设计。
- 流转设置保存：修复后真机验证「保存→立即重开」显示新值，通过。

### 已知遗留（v2.2 候选，按优先级）

1. Toolbag Baker 自动入组（本次已记入 docs/RELEASE-v2.1.md 已知限制）。
2. Maya _post_transfer 的 groom / 贴图两段尚未真机复测（模型段已过）。
3. 加 .gitattributes（*.md text eol=lf），根除行尾重排导致的假 diff。根因：core.autocrlf=true 且无 .gitattributes。
4. §五 待办：_pid_of 按进程名匹配 PS 退出慢约 16s 滞后；[watch-state] 每 0.5s 刷日志。

### 本轮教训

- 禁止用 PowerShell here-string → python - 管道传非 ASCII，会把中文降级成 0x3F；非 ASCII 内容走 base64 或磁盘 .py 文件。
- PowerShell 的 Measure-Object -Line 对 LF 文件计数不可靠；判行数用 git cat-file -s + Python 数 \n。
- 纯追加一律用 AppendAllText / 二进制 append，禁止编辑器整文件保存。
- 修复已 push 的污染用 revert，不用 force push。
- push 前必看 git rev-list --count origin/main..main，数字以 git 为准。

### 工作区未跟踪文件（约定不提交）

HANDOVER.md、docs/ARCHITECTURE.md、docs/HANDOFF-2026-10-15.md、
docs/TESTING-MATRIX.md、docs/UI-WIREFRAME-2026-10-18.md、start_bridge_server.bat
