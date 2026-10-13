# AssetBridge · ZBrush 适配器

ZBrush **2026.1+** 官方 Python SDK（`zbrush.commands`）适配器。第 8 个接入的 app
（`app id = zbrush`，资源类型 `models`，格式 OBJ）。

> 2025.x 只有 ZScript、没有 Python API，不在支持范围（回退路线见
> `docs/RESEARCH_ZBRUSH.md` §8.3，版本感知双路线，当前不启用）。

## 架构（为什么是「主线程 serve 循环」）

ZBrush 内嵌 Python VM 把 GIL 留在**主线程**：`zbrush.commands` 只能在主线程调用，
后台线程调 zbc 会饿死（社区权威实现 dcc-mcp-zbrush / scenario cookbook 一致结论）。
因此本适配器没有常驻 worker 线程，而是：

```
[启动桥接] 按钮 -> 主线程循环：
    每圈：处理 inbox 任务（导入 OBJ） -> 处理 outbox 热文件夹 -> 写心跳
          -> zbc.update(1, True) pump UI（停止按钮在 update 里得到处理机会）
          -> sleep(poll_interval)
```

ZBrush 会话内解释器**常驻单会话**：按钮创建后不会被后续脚本销毁，
所以「Load Script 一次」即可；安装器还会往 `ZData\Python\init.py`
注入自动加载钩子（marker 包裹、幂等），实现**零手动步骤**启动。

## 文件

| 文件 | 职责 |
|---|---|
| `asset_bridge_zbrush.py` | 适配器主体：菜单按钮 + serve 循环 + inbox/outbox + HTTP 收发。**不 import zbrush.commands（懒加载）**，可在普通 Python 里单测 |
| `one_shot_import.py` | `-script` 一次性导入兜底（ZBrush 未运行时守护进程拉起用） |
| `ab_config.json` | 运行配置（安装器写入；缺失用默认值） |
| `profiles.json` | 单位/轴向换算档案（shared 副本，`tests/profiles_sync.py` 同步） |

## 安装（托盘一键）

托盘安装器自动完成：

1. 拷贝本目录到 **用户资产目录** `ZStartup\ZPlugs64\asset_bridge_zbrush\`
   （2026.1 起安装目录只读，用户内容必须在用户目录；安装路径探测见
   `Find-ZBrush`：卸载注册表 + `installpath.txt` + 固定路径 + DCC 盘扫描）。
2. 写 `ab_config.json`（server / payload / inbox / outbox / 轮询间隔 / 默认发送目标）。
3. 往 `ZData\Python\init.py` 注入自动加载钩子（失败不阻塞，可手动 Load Script）。

装完**重启 ZBrush** 即自动出现 `Zplugin > AssetBridge` 面板并开始 serve 循环。

## 使用

- **接收**（其他软件 → ZBrush）：全自动。守护进程 claim 后写 inbox 任务文件，
  serve 循环主线程导入（`set_next_filename + Tool:Import`，免对话框），写回执。
  ZBrush 运行中但桥接未启动时，模型落盘 + 托盘气泡提示（路径可一键复制），
  之后 `Tool:Load` 手动加载。
- **发送**（ZBrush → 其他软件）：`发送当前模型` 按钮 →
  `set_next_filename + Tool:Export` 免对话框导出 → HTTP 建传输/上传/publish。
  目标是 `ab_config.json` 的 `default_to`（默认 blender）。
- **发送回最近发送者**：记住最近一次 inbox 任务的 `from_app`，一键回传。
- **热文件夹兜底**：把导出的 OBJ/.ztl 丢进 `<payload>\zbrush_outbox\`，
  守护进程自动回传到 `zbrush_outbox_to`（默认 blender）。

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
python tests/test_zbrush_adapter.py     # 17 项：协议/配置/bbox/发布流/守护分支
python tests/test_tray_functions.py     # 托盘切片：Find-ZBrush / 解析 / 配置同步
python tests/profiles_sync.py           # 换算档案一致性
```
