# -*- coding: utf-8 -*-
"""Asset Bridge :: 贴图回传链路 —— UE 5.8 接收端实机自检（commandlet）。

用法：
    UnrealEditor-Cmd.exe "<project>.uproject" -run=pythonscript
        -script="E:\\CirnoWorkSpace\\AssetBridge\\unreal\\scripts\\ue_texture_selftest.py"
        -unattended -nosplash -NullRHI -stdout -FullStdOutLogOutput

走的**真实链路**：调用适配器自身代码 `asset_bridge_unreal._handle(record)`
（内部 = client 拉取 + importer.import_textures + finish 回执），不绕过它。
这样测出来的就是 SP 贴图回传真正会走的代码路径。

验证项：
    a. 3 张贴图资产生成在 /Game/Bridge/Textures，命名符合 T_<asset>_<Channel>
    b. 类型为 Texture2D，实测尺寸 64x64
    c. 增量：重复导入不生成 _1 后缀副本
    d. 压缩设置 / sRGB 默认值 —— 判断 normal 是否被正确识别为法线

结果写到 logs/ue_texture_selftest_run{run}.json（run 由环境变量 TEX_RUN 指定）。
"""
import json
import os
import sys
import traceback

import unreal

# --------------------------------------------------------------------------- 常量
PLUGIN_ROOT = r"E:\CirnoWorkSpace\AssetBridge\unreal\plugin"
LOG_DIR = r"E:\CirnoWorkSpace\AssetBridge\logs"
SERVER = "http://127.0.0.1:27631"
PROJECT = "AssetBridgeTest"
TEX_PATH = "/Game/Bridge/Textures"
ASSET_NAME = "SM_CalibCube"
EXPECT_CHANNELS = ("BaseColor", "Normal", "ORM")
RUN = os.environ.get("TEX_RUN", "1").strip() or "1"
RESULT_JSON = os.path.join(LOG_DIR, f"ue_texture_selftest_run{RUN}.json")

if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)

RESULT = {
    "run": RUN,
    "engine_version": "",
    "python": sys.version.split()[0],
    "import": {},
    "assets": {},
    "checks": {},
    "errors": [],
    "steps": [],
}
_LINES = []


def log(msg, tag="INFO"):
    text = f"[{tag}] {msg}"
    unreal.log("[tex-selftest] " + text)
    RESULT["steps"].append(text)
    _LINES.append(text)


def err(context, exc):
    text = f"{context} -> {type(exc).__name__}: {exc}"
    log(text, "ERROR")
    try:
        log(traceback.format_exc().replace("\n", " | "), "ERROR")
    except Exception:
        pass
    RESULT["errors"].append(text)


def check(name, expected, actual, ok=None):
    passed = (expected == actual) if ok is None else bool(ok)
    RESULT["checks"][name] = {"expected": expected, "actual": actual,
                              "pass": bool(passed)}
    log(f"CHECK {name}: {'PASS' if passed else 'FAIL'} "
        f"(expected={expected!r} actual={actual!r})", "CHECK")
    return passed


# --------------------------------------------------------------------------- 工具
def list_assets(root=TEX_PATH):
    try:
        return sorted(unreal.EditorAssetLibrary.list_assets(
            root, recursive=False, include_folder=False))
    except Exception as exc:
        err(f"list_assets({root})", exc)
        return []


def eng_num(value):
    """把 UE 枚举/属性值转成可读字符串（枚举取 name，其余 str）。"""
    for attr in ("name", "get_name"):
        try:
            v = getattr(value, attr)
            v = v() if callable(v) else v
            if isinstance(v, str) and v:
                return v
        except Exception:
            continue
    try:
        d = value.to_dict()
        if isinstance(d, dict) and "name" in d:
            return str(d["name"])
    except Exception:
        pass
    return str(value)


def texture_size(tex):
    """量 Texture2D 尺寸，依次尝试多种 API。"""
    out = {"ok": False, "attempts": []}
    for xa, ya in (("blueprint_get_size_x", "blueprint_get_size_y"),
                   ("get_size_x", "get_size_y")):
        try:
            x = getattr(tex, xa)
            y = getattr(tex, ya)
            x = x() if callable(x) else x
            y = y() if callable(y) else y
            if x and y:
                out.update(ok=True, method=f"{xa}/{ya}", x=int(x), y=int(y))
                out["attempts"].append(f"{xa}: OK")
                return out
        except Exception as exc:
            out["attempts"].append(f"{xa}: {type(exc).__name__}: {exc}")
    # 兜底：source_size / imported_size（IntPoint）
    for prop in ("imported_size", "source_size"):
        try:
            v = tex.get_editor_property(prop)
            out["attempts"].append(f"{prop}: {v}")
            out.update(ok=True, method=prop, x=int(v.x), y=int(v.y))
            return out
        except Exception as exc:
            out["attempts"].append(f"{prop}: {type(exc).__name__}: {exc}")
    return out


def describe_texture(tex):
    """取压缩设置 / sRGB / LOD 等关键属性。"""
    info = {"class": type(tex).__name__}
    for prop in ("compression_settings", "srgb", "compression_no_alpha",
                 "lod_group", "mip_gen_settings", "never_stream"):
        try:
            v = tex.get_editor_property(prop)
            info[prop] = eng_num(v) if prop in (
                "compression_settings", "lod_group", "mip_gen_settings") else (
                bool(v) if isinstance(v, bool) else str(v))
        except Exception as exc:
            info[prop] = f"ERR {type(exc).__name__}: {exc}"
    info["size"] = texture_size(tex)
    return info


def enum_names(enum_cls):
    try:
        return sorted(p for p in dir(enum_cls) if p.startswith("TC_"))
    except Exception as exc:
        return [f"ERR {exc}"]


# --------------------------------------------------------------------------- 真实链路
def pull_and_import():
    """走适配器真实链路：register -> inbox -> claim -> _handle(-> import_textures)。"""
    import asset_bridge_unreal as abu

    abu.CONFIG["project"] = PROJECT
    abu.CONFIG["server_url"] = SERVER

    c = abu.client()
    try:
        sess = c.register()
        RESULT["import"]["session_id"] = sess.get("session_id")
        log(f"已注册 UE 会话 {sess.get('session_id')}")
    except Exception as exc:
        err("register", exc)
        return

    inbox = c.inbox(limit=20)
    log(f"收件箱 {len(inbox)} 条: "
        f"{[(r.get('asset_name'), r.get('kind'), r.get('status')) for r in inbox]}")

    pending = [r for r in inbox
               if r.get("kind") == "textures" and r.get("asset_name") == ASSET_NAME]
    RESULT["import"]["pending_count"] = len(pending)

    if not pending:
        log("收件箱没有待处理贴图 transfer（本轮只做只读校验）")
        RESULT["import"]["imported"] = False
        return

    processed = []
    for rec in pending:
        tid = rec["id"]
        try:
            full = c.claim(tid)
        except Exception as exc:
            err(f"claim {tid}", exc)
            continue
        log(f"已认领 {tid} rev={full.get('revision')} "
            f"files={full.get('_files_abs')}")
        # 真实链路入口：__init__._handle -> importer.import_textures
        abu._handle(full)
        # _handle 内部会把结果 finish 回服务端；这里回读一下最终状态
        try:
            after = c._request("GET", f"/transfers/{tid}")
            processed.append({
                "transfer_id": tid,
                "revision": after.get("revision"),
                "status": after.get("status"),
                "result": after.get("result"),
            })
        except Exception as exc:
            err(f"回读 {tid}", exc)
            processed.append({"transfer_id": tid})
    RESULT["import"]["imported"] = True
    RESULT["import"]["processed"] = processed
    RESULT["import"]["transfer_ids"] = [p["transfer_id"] for p in processed]


# --------------------------------------------------------------------------- 校验
def verify_assets():
    expected = {f"T_{ASSET_NAME}_{ch}": ch for ch in EXPECT_CHANNELS}
    all_assets = list_assets(TEX_PATH)
    RESULT["assets"]["all_under_textures"] = all_assets

    related = [p for p in all_assets
               if p.rsplit("/", 1)[-1].split(".")[0].startswith(
                   f"T_{ASSET_NAME}_")]
    found = {p.rsplit("/", 1)[-1].split(".")[0]: p for p in related}
    RESULT["assets"]["related"] = sorted(found.keys())

    # a. 命名与数量
    want = sorted(expected.keys())
    got = sorted(found.keys())
    check("tex_三张资产齐备", want, got)

    # b. 类型 + 尺寸
    detail = {}
    for name, path in sorted(found.items()):
        obj = unreal.EditorAssetLibrary.load_asset(path)
        if obj is None:
            detail[name] = {"error": "load_asset 返回 None"}
            continue
        is_tex2d = isinstance(obj, unreal.Texture2D)
        is_tex = isinstance(obj, unreal.Texture)
        d = describe_texture(obj)
        d["is_texture2d"] = bool(is_tex2d)
        d["is_texture"] = bool(is_tex)
        d["path"] = path
        detail[name] = d
        log(f"{name}: class={d['class']} size={d['size']} "
            f"compression={d.get('compression_settings')} srgb={d.get('srgb')}")
    RESULT["assets"]["detail"] = detail

    for name in expected:
        d = detail.get(name, {})
        check(f"tex_{name}_是Texture2D", True, bool(d.get("is_texture2d")))
        sz = d.get("size") or {}
        check(f"tex_{name}_尺寸64x64", [64, 64],
              [sz.get("x"), sz.get("y")], ok=(sz.get("x") == 64 and sz.get("y") == 64))

    # c. 增量：无 _1 副本
    dup = [n for n in got if n not in expected]
    check("tex_无_1后缀副本", [], dup)

    # d. 压缩 / sRGB 结论
    comp = {n: (detail.get(n) or {}).get("compression_settings")
            for n in expected}
    srgb = {n: (detail.get(n) or {}).get("srgb") for n in expected}
    RESULT["assets"]["compression_summary"] = comp
    RESULT["assets"]["srgb_summary"] = srgb

    normal_ok = "NORMALMAP" in str(comp.get(f"T_{ASSET_NAME}_Normal")).upper()
    orm_ok = any(k in str(comp.get(f"T_{ASSET_NAME}_ORM")).upper()
                 for k in ("MASK", "GRAYSCALE"))
    bc_ok = "DEFAULT" in str(comp.get(f"T_{ASSET_NAME}_BaseColor")).upper()
    check("tex_Normal识别为法线压缩", "TC_NORMALMAP", comp.get(f"T_{ASSET_NAME}_Normal"),
          ok=normal_ok)
    check("tex_ORM识别为Masks/Grayscale", "TC_MASKS",
          comp.get(f"T_{ASSET_NAME}_ORM"), ok=orm_ok)
    check("tex_BaseColor保持Default", "TC_DEFAULT",
          comp.get(f"T_{ASSET_NAME}_BaseColor"), ok=bc_ok)
    check("tex_Normal关闭sRGB", False, srgb.get(f"T_{ASSET_NAME}_Normal"))
    check("tex_BaseColor开启sRGB", True, srgb.get(f"T_{ASSET_NAME}_BaseColor"))
    RESULT["assets"]["normal_ok"] = bool(normal_ok)
    RESULT["assets"]["orm_ok"] = bool(orm_ok)
    RESULT["assets"]["basecolor_ok"] = bool(bc_ok)


def preclean():
    """删除修 bug 之前留下的非规范命名贴图（测试夹具清理，默认关闭）。

    TEX_PRECLEAN=1 时才跑：把 /Game/Bridge/Textures 下不属于规范集合
    {T_<asset>_BaseColor/Normal/ORM} 的资产删掉，避免旧命名的残留资产
    污染「资产数量 / _1 副本」这两项断言。
    """
    expected = {f"T_{ASSET_NAME}_{ch}" for ch in EXPECT_CHANNELS}
    removed = []
    for p in list_assets(TEX_PATH):
        short = p.rsplit("/", 1)[-1].split(".")[0]
        if short not in expected:
            try:
                if unreal.EditorAssetLibrary.delete_asset(p):
                    removed.append(short)
            except Exception as exc:
                err(f"preclean 删除 {p}", exc)
    RESULT["preclean_removed"] = removed
    log(f"preclean 删除非规范资产 = {removed}")


def main():
    try:
        RESULT["engine_version"] = unreal.SystemLibrary.get_engine_version()
    except Exception as exc:
        err("get_engine_version", exc)
    log(f"engine = {RESULT['engine_version']}  python = {RESULT['python']}")
    log(f"run = {RUN}  content_path = {TEX_PATH}")

    # API 能力探测（方便版本不兼容时定位）
    api = {}
    for name in ("AssetImportTask", "TextureFactory", "Texture2D",
                 "TextureCompressionSettings", "EditorAssetLibrary",
                 "AssetToolsHelpers"):
        api[name] = hasattr(unreal, name)
    try:
        api["TextureCompressionSettings.enum"] = enum_names(
            unreal.TextureCompressionSettings)
    except Exception as exc:
        api["TextureCompressionSettings.enum"] = f"ERR {exc}"
    RESULT["api"] = api
    log(f"api = {json.dumps({k: v for k, v in api.items() if k != 'TextureCompressionSettings.enum'}, ensure_ascii=False)}")
    log(f"TC enum = {api.get('TextureCompressionSettings.enum')}")

    if os.environ.get("TEX_PRECLEAN", "").strip().lower() in ("1", "true", "yes"):
        preclean()

    pull_and_import()
    verify_assets()


try:
    main()
except Exception as exc:
    err("main", exc)

try:
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(RESULT_JSON, "w", encoding="utf-8") as fh:
        json.dump(RESULT, fh, ensure_ascii=False, indent=2, default=str)
    with open(os.path.join(LOG_DIR, f"ue_texture_selftest_run{RUN}.log"),
              "w", encoding="utf-8") as fh:
        fh.write("\n".join(_LINES))
except Exception as exc:
    unreal.log_error(f"[tex-selftest] 写结果失败: {exc}")

unreal.log("[tex-selftest] SUMMARY " + json.dumps({
    "run": RUN,
    "engine": RESULT["engine_version"],
    "checks": {k: v["pass"] for k, v in RESULT["checks"].items()},
    "errors": RESULT["errors"],
}, ensure_ascii=False))
unreal.log("[tex-selftest] 结束")
