# Asset Bridge 图标集

一套统一视觉语言的图标，**可复现生成**（改配色/加图标后重跑脚本即可）。

```
生成器 : tools\gen_icons.py
源文件 : assets\icons\png\<16|24|32|48|64>\*.png
         assets\icons\ico\*.ico        （每个 ico 内含 16/24/32/48/64 五档）
预览   : assets\icons\PREVIEW.png        （联系人表，看整体风格）
         assets\icons\PREVIEW_SMALL.png  （16/24/32 实际大小 vs 3 倍放大，验可辨识度）
```

重新生成：

```bash
C:/Users/12069/.workbuddy/binaries/python/envs/default/Scripts/python.exe tools/gen_icons.py
```

## 设计规范

| 项 | 约定 |
|---|---|
| 画布 | 32×32 网格，**8 倍超采样**绘制后降采样 → 边缘干净无锯齿 |
| 状态类 | **圆角方块** + 白色字形（与动作类统一；方块比圆在 16px 下更清晰、更有辨识度） |
| 动作类 | 圆角方块（圆角 7.5）+ 白色字形 |
| DCC 徽标 | 圆角方块 + 白色首字母 |
| 风格 | 扁平、无渐变、无描边；保证 16px 下仍可辨识 |

## 图标清单（16 个）

### 状态类（托盘使用）

| 文件 | 预览 | 含义 | 场景 |
|---|---|---|---|
| `state_running` | 🟢 绿方块 + ⇄ | 服务运行中 | 正常 |
| `state_stopped` | 🔴 红方块 + ■ | 已停止（守护也暂停） | 你手动停了，且不会自动拉 |
| `state_recover` | 🟠 琥珀方块 + ❗ | 掉线，守护会自动拉起 | 服务意外退出，10 秒内自动恢复 |
| `state_paused` | ⚪ 灰方块 + ⏸ | 运行中但守护已暂停 | 你点了「暂停守护」 |

> 托盘实际映射：`运行中+守护开`→绿；`运行中+守护停`→灰；`已停止+守护开`→琥珀；`已停止+守护停`→红。
> 悬停气泡会写明 `运行中 / 已停止` 与 `等待自动拉起`。

### 动作类

| 文件 | 配色 | 含义 | 建议用途 |
|---|---|---|---|
| `act_start` | 绿 | 启动 | Maya shelf「启动」 |
| `act_stop` | 红 | 停止 | Maya shelf「停止」 |
| `act_restart` | 蓝 | 重启 / 刷新 | 重启服务 |
| `act_send` | 青 | 发送（向上箭头 = 发出） | Blender/Maya/SP 的「发送」按钮 |
| `act_receive` | 青 | 接收（向下箭头 = 收下） | 各软件的「接收」按钮 |
| `act_sync` | 蓝 | 同步（上下互发） | 同步 `profiles.json` |
| `act_settings` | 灰 | 设置（齿轮） | 偏好设置入口 |
| `app_logo` | 石墨 | 应用主图标（⇄ 互发） | 文档封面 / 快捷方式 / 关于 |

### DCC 徽标

| 文件 | 配色 | 说明 |
|---|---|---|
| `dcc_blender` | 橙 `#FB923C` | Blender |
| `dcc_unreal` | 石墨 `#2F384A` | Unreal Engine |
| `dcc_maya` | 青 `#06B6D4` | Maya |
| `dcc_painter` | 紫 `#8B5CF6` | Substance Painter |

## 已部署位置

| 目标 | 文件 | 说明 |
|---|---|---|
| 托盘 | `tray\icon_ok.ico` / `icon_down.ico` / `icon_warn.ico` / `icon_paused.ico` | ← 分别来自 `state_running` / `state_stopped` / `state_recover` / `state_paused` |
| Maya shelf | `C:\Users\12069\Documents\maya\2023\zh_CN\prefs\icons\assetbridge_start.png` / `assetbridge_stop.png` | ← `act_start` / `act_stop`（**文件名保持不变**，Maya shelf 无需改动） |
| Maya shelf（备用） | 同目录下全套 `act_*.png` / `state_*.png` / `dcc_*.png` / `app_logo.png` | 给以后新增按钮用 |

## 部署与维护（重要）

改完图标后**必须重跑生成器再部署** —— 否则只是把旧图标原样复制过去，看起来"换了等于没换"：

```bash
# 1) 重新生成（写 assets/icons/）
C:/Users/12069/.workbuddy/binaries/python/envs/default/Scripts/python.exe tools/gen_icons.py

# 2) 状态 4 态 -> 托盘（务必先退出托盘，见下）
cp assets/icons/ico/state_running.ico tray/icon_ok.ico
cp assets/icons/ico/state_stopped.ico tray/icon_down.ico
cp assets/icons/ico/state_recover.ico tray/icon_warn.ico
cp assets/icons/ico/state_paused.ico  tray/icon_paused.ico

# 3) 32px -> Maya shelf 图标目录（文件名保持不变）
cp assets/icons/png/32/act_start.png "C:/Users/12069/Documents/maya/2023/zh_CN/prefs/icons/assetbridge_start.png"
cp assets/icons/png/32/act_stop.png  "C:/Users/12069/Documents/maya/2023/zh_CN/prefs/icons/assetbridge_stop.png"
```

### 三个坑（都踩过）

1. **改了 `gen_icons.py` 却没重跑生成器** → 磁盘上的 ico 还是旧造型，`cp` 只是把旧图标复制进 `tray/`。改完造型**先跑生成器，再看 `PREVIEW.png` 确认**。
2. **托盘运行时锁住 `tray\icon_*.ico`**（`.NET Icon` 构造函数持有文件句柄）→ `cp` 覆盖会失败。**部署前先退出托盘**（右键托盘 → 退出）。
3. 托盘正加载 `png\16\*.png` 时，生成器写这些文件会被占用 → 该文件 `PARTIAL` 跳过（容错，不中断）。退出托盘后再生成即可全部写出。

> 托盘 `Apply-Icon` 按文件时间戳缓存，磁盘 ico 被替换后会自动重载；但**已加载的旧文件句柄会阻止替换**，所以换图标的标准姿势仍是「退出托盘 → 重跑生成器 → cp → 重启托盘」。

## 配色表（改配色就改这里）

```python
C = {
    "green":    (34, 197, 94),    # 运行 / 启动
    "red":      (239, 68, 68),    # 停止
    "amber":    (245, 158, 11),   # 告警 / 自动恢复中
    "slate":    (100, 116, 139),  # 设置 / 守护暂停
    "blue":     (59, 130, 246),   # 重启 / 同步
    "teal":     (20, 184, 166),   # 收发
    "graphite": (47, 56, 74),     # 应用主色 / UE
    "orange":   (251, 146, 60),   # Blender
    "cyan":     (6, 182, 212),    # Maya
    "violet":   (139, 92, 246),   # Substance Painter
}
```

## 想加新图标？

1. 在 `tools\gen_icons.py` 里写一个字形函数（坐标系是 32×32，用 `u()` 换算），
   例如：
   ```python
   def g_upload(d):
       rrect(d, 14.4, 13.4, 17.6, 23.4, r=1.1)
       tri(d, [(16.0, 7.4), (22.0, 14.6), (10.0, 14.6)])
   ```
2. 把它加进 `ICONS` 列表：`("act_upload", "tile", C["blue"], g_upload),`
3. 重跑生成脚本，然后看 `PREVIEW_SMALL.png` 确认 16px 下还认得出。

> 小尺寸是唯一的验收标准：**如果 16px 下看不清，就简化形状**（少笔画、加粗、去掉细节）。
