# AssetBridge · ZBrush 适配器

ZBrush **2026.1+** 官方 Python SDK（`zbrush.commands`）适配器。第 8 个接入的 app
（`app id = zbrush`，资源类型 `models`，格式 OBJ）。

> 2025.x 只有 ZScript、没有 Python API，不在支持范围（回退路线见
> `docs/RESEARCH_ZBRUSH.md` §8.3，版本感知双路线，当前不启用）。

## 架构（2026-10-13 重构：ZBrush 卡死事故后的线程模型）

ZBrush 内嵌 Python VM 的 GIL 在**主线程**：`zbrush.commands` 只能主线程调用
（后台线程调 zbc 会饿死）；**主线程里的长循环/同步等待会把 ZBrush 直接冻死**
（旧版 autostart 一启动就进 serve 循环，实机验收时 ZBrush 卡死强退）。现行模型：

```
autostart()（init.py 钩子调用，零阻塞，立即返回）
   |- install_ui()      建 Zplugin > AssetBridge 面板 + 5 个按钮（瞬时完成）
   |- start_watcher()   观察线程：纯文件 IO（扫 inbox / 写心跳 / 写状态文件），
                         绝不调用 zbc、不占主线程、异常自吞

按钮回调（主线程、有界、单次、带对话框探针）
   |- 导入待收模型       处理当前 inbox 任务（set_next_filename + Tool:Import）
   |- 发送当前模型       导出 + HTTP 建传输/上传/publish
   |- 发送回最近发送者    回传给最近一次 from_app
   |- 立即回传热文件夹     outbox 单轮回传
   |- 自动导入（实验，默认关） 主线程 pump 循环：单圈 60s 看门狗 + 总时长上限 + 随时可停
```

**不挂起的保证**：每次 press 前探 `has_next_filename()`（有残留预设 = 疑似原生
对话框挡路，直接跳过）；单文件导入等待上限 `import_wait_sec`（默认 15s）；
导出等待上限 120s；观察线程任何异常自吞。ZBrush 启动路径**零阻塞**。

## 文件

| 文件 | 职责 |
|---|---|
| `asset_bridge_zbrush.py` | 适配器主体：按钮 + 观察线程 + 有界操作 + HTTP。**不 import zbrush.commands（懒加载）**，可在普通 Python 里单测 |
| `one_shot_import.py` | `-script` 一次性导入兜底（ZBrush 未运行时守护进程拉起用） |
| `ab_config.json` | 运行配置（安装器写入；缺失用默认值） |
| `profiles.json` | 单位/轴向换算档案（shared 副本，`tests/profiles_sync.py` 同步） |

## 安装（托盘一键）

托盘安装器自动完成：

1. 拷贝本目录到 **用户资产目录** `ZStartup\ZPlugs64\asset_bridge_zbrush\`
   （2026.1 起安装目录只读，用户内容必须在用户目录；安装路径探测见
   `Find-ZBrush`：卸载注册表 + `installpath.txt` + 固定路径 + DCC 盘扫描）。
2. 写 `ab_config.json`（server / payload / inbox / outbox / 轮询间隔 / 默认发送目标）。
3. 往 `ZData\Python\init.py` 注入自动加载钩子（marker 包裹、幂等；失败不阻塞，
   可 ZScript>Python 面板手动 Load Script）。

装完**重启 ZBrush** 即自动出现 `Zplugin > AssetBridge` 面板，观察线程开始运行
（状态写 `<payload>\bridge_status.json`，含 pending 待收计数）。

## 使用

- **接收**（其他软件 -> ZBrush）：任务到达后观察线程会发现并计数；点
  **「导入待收模型」** 执行导入（主线程、有界、免对话框）。想要「全自动」可点
  **「自动导入」**（实验功能：主线程循环，ZBrush 可能周期性卡顿，随时可停，
  单圈 60s 看门狗）。ZBrush 未运行时守护进程用 `-script` 一次性导入兜底；
  运行中但桥接未就绪 -> 落盘 + 托盘气泡（路径一键复制）+ `Tool:Load` 手动加载。
- **发送**（ZBrush -> 其他软件）：**「发送当前模型」**（默认目标）或
  **「发送回最近发送者」**。导出免对话框，发完 note bar 有提示，对方自动导入/重载。
- **热文件夹兜底**：把导出的 OBJ/.ztl 丢进 `<payload>\zbrush_outbox\`，
  点「立即回传热文件夹」，或等守护进程自动回传。

> 语义说明：ZBrush **没有保存事件钩子**，发送只能是手动触发（按钮/热文件夹），
> 不存在「保存后自动回传」。

## 单位

ZBrush 单位无量纲；导入 OBJ 时自动归一到约 2 单位并把还原系数记在 `Tool>Export`，
导出按该系数还原为**进入前的原始数值**。实测（2026-10-13，1m 立方往返）：
`global_scale = 1.0`，Blender/UE/Maya 链路系数见 `profiles.json` routes（UE 侧 100 倍待实测校准）。

导入陷阱（适配器已规避）：导入前确保无活动 Polymesh（或其 `Tool>Export Scale=0`），
否则会按错误系数缩放。

## 测试

```
python tests/test_zbrush_adapter.py     # 24 项：协议/配置/bbox/有界导入/对话框探针/观察线程/发布流
python tests/test_tray_functions.py     # 托盘切片 + 启动路径运行时冒烟
python tests/profiles_sync.py           # 换算档案一致性
```
