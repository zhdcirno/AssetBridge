// Asset Bridge :: Substance 3D Painter 适配器
//
// 安装：整个 asset-bridge 文件夹拷到
//   C:/Users/<你>/Documents/Adobe/Adobe Substance 3D Painter/plugins/
//   （老版本是 .../Documents/Allegorithmic/Substance Painter/plugins/）
//
// SP 在这条链路上承担三件事：
//   1) 接收模型：别的软件把 FBX 发过来，SP 直接用它开项目
//   2) 接收贴图：Toolbag 把烘焙结果（kind=textures）发过来，
//      导入当前工程的资源库并选中（必须先打开一个项目）
//   3) 回传贴图：把画好的 baseColor/normal/ORM 等发回 UE / Blender /
//      Maya，或发给 Photoshop / SAI2 用外部编辑器精修
//
// ---------------------------------------------------------------------------
// API 全部按本机 Substance 3D Painter 自带文档核对过
// （D:/DCC/Adobe Substance 3D Painter/resources/javascript-doc），
// 修正了几处凭记忆写错的地方：
//   1. alg.project.reloadMesh() **不存在** —— 没有「原地刷新网格」这回事，
//      只能 close 后用新网格 create() 开新项目。
//   2. alg.mapexport.save(dataPath, filePath) 的第一个参数是「要渲染什么」的
//      路径数组，不是配置对象。批量按预设导出要用 exportDocumentMaps。
//   3. alg.fileIO **没有 copy()**。所以不拷贝文件：模型直接从负载目录打开，
//      贴图直接导出到负载目录。
//   4. exportDocumentMaps(preset, path, format, mapInfo) 的返回值结构是
//      { "贴图集名": { "map 模板键": "绝对路径" | "" } }——没有导出的 map 是
//      空字符串，所以摊平时必须过滤 falsy，否则会拿到一堆空路径。
//   5. alg.texturesets.getActiveTextureSet() 返回的是 StackPath
//      （Array.<string>，形如 ["贴图集名"] 或 ["贴图集名", "堆栈名"]），
//      而 alg.mapexport.textureSetResolution(name) 要的是**贴图集名字符串**，
//      所以得取 [0]（这里额外做了「万一是字符串」的兜底）。
// ---------------------------------------------------------------------------
//
// 两个交互入口：
//   * 工具栏按钮「收模型」/「发贴图」——人工操作
//   * 收件箱里的 kind = "textures_request" ——**远程触发回传贴图**，
//     让 Blender / UE 侧脚本能一键把 SP 的贴图拉回去，不需要人点按钮。
//     请求里可以用 options.target 指定回传给谁（默认 unreal）。
// ---------------------------------------------------------------------------
//
// 健壮性（Bug2 修复）：
//   服务不可达时，旧实现「每个 tick 同步 XHR + 无退避 + 注册失败不重试」
//   会把 SP 主线程拖死并疯狂刷日志。这里改为：
//     * http() 设 1.5s 超时；
//     * onTick 自建时间闸门（>=1000ms 才真正轮询，不依赖 tickIntervalMS）；
//     * 连续失败指数退避 min(1000*2^(n-1), 30000) ms，任意成功立即复位；
//     * 失败日志最多每 30s 一条；
//     * 未拿到 sessionId 前不调 claim；注册失败按同样策略在 tick 里重试；
//     * 连续失败 >=5 次熔断，输出明确提示并按 30s 间隔探测恢复。

import QtQuick 2.7
import Painter 1.0
import AlgWidgets 2.0

PainterPlugin {
    // 每 1000ms 轮询一次收件箱（SP 的 tick 跑在主线程，可以直接干活）
    tickIntervalMS: 1000
    jsonServerPort: -1

    // ---- 配置 ---------------------------------------------------------
    property string serverUrl: "http://127.0.0.1:27631"
    property string appName: "substance_painter"
    property string projectName: ""
    property var sessionId: ""
    property var seenIds: ({})        // 已处理过的 transfer，避免重复
    property int tickCount: 0
    property string exportPreset: ""  // 留空则用工程自带的导出预设
    property string exportFormat: "png"

    // 回传贴图时默认发给谁（请求方可以用 options.target 覆盖）
    property string defaultTargetApp: "unreal"
    property string defaultTargetProject: "AssetBridgeTest"
    property string defaultContentPath: "/Game/Bridge/Textures"

    // 工具栏按钮点「发贴图」时是否先烘焙。默认关：
    // 烘焙很慢（2K 可能十几秒到几分钟），美术一般自己烘好了再回传。
    // 远程请求（textures_request）默认**会**烘焙，见 handleTexturesRequest。
    property bool bakeBeforeExport: false

    // ---- 退避 / 熔断 / 日志限频状态 -----------------------------------
    property int pollFailStreak: 0     // 连续失败计数
    property double nextPollTs: 0      // 下次允许真正轮询的时间戳(ms)
    property double lastFailLogTs: 0   // 上次失败日志时间戳(ms)
    property bool circuitOpen: false   // 熔断是否已触发
    property double lastProbeTs: 0      // 熔断后上次探测时间戳(ms)

    // ---- 生命周期 -----------------------------------------------------
    Component.onCompleted: {
        alg.log.info("[bridge] Substance Painter 适配器已加载");
        var btn = alg.ui.addToolBarWidget("toolbar.qml");
        if (btn) {
            btn.pullClicked.connect(receiveNow);
            btn.pushClicked.connect(sendTexturesTo);
        }
        register();
    }

    // 工具栏「回传→XX」：指定目标后走原来的 sendTextures
    function sendTexturesTo(app) {
        if (app) defaultTargetApp = app;
        sendTextures();
    }

    // ---- 底层 HTTP ----------------------------------------------------
    function http(method, path, body) {
        var xhr = new XMLHttpRequest();
        var url = serverUrl + "/api/v1" + path;
        xhr.open(method, url, false);            // 同步：tick / 按钮回调里够用
        // 设超时，避免服务不可达时同步 XHR 一直卡住主线程。
        // 部分 QML 引擎对「同步 XHR 设 timeout」会抛错，用 try 兜住。
        try { xhr.timeout = 1500; } catch (e) { }
        xhr.setRequestHeader("Content-Type", "application/json; charset=utf-8");
        try {
            xhr.send(body ? JSON.stringify(body) : "");
            if (xhr.status >= 200 && xhr.status < 300) {
                return xhr.responseText ? JSON.parse(xhr.responseText) : {};
            }
            throw new Error("HTTP " + xhr.status + " " + xhr.responseText);
        } catch (e) {
            alg.log.warn("[bridge] " + method + " " + path + " 失败: " + e);
            return null;
        }
    }

    function register() {
        var r = http("POST", "/sessions", {
            app: appName,
            project: projectName,
            capabilities: { formats: ["fbx", "obj", "png", "tga"], send: true, receive: true }
        });
        if (r && r.session) {
            sessionId = r.session.session_id;
            alg.log.info("[bridge] 已注册 SP 会话 " + sessionId);
        }
    }

    // 这条 transfer 的负载目录（服务端建记录时就创建好了）
    function payloadDirFor(tid) {
        var cfg = http("GET", "/config");
        if (!cfg || !cfg.payload_dir) {
            alg.log.error("[bridge] 拿不到服务端的负载目录");
            return "";
        }
        return cfg.payload_dir + "/" + tid;
    }

    // 当前项目名（拿不到就返回空串）
    function currentProjectName() {
        try {
            var n = alg.project.name();
            if (n) return n;
        } catch (e) { }
        return "";
    }

    // 当前激活的贴图集名字。
    // getActiveTextureSet() 返回 StackPath（Array.<string>，形如 ["贴图集名"]），
    // 而烘焙 / 读分辨率要的是**名字字符串**，所以要取 [0]；万一返回字符串就兜住。
    function activeTextureSetName() {
        try {
            var ts = alg.texturesets.getActiveTextureSet();
            var name = (ts instanceof Array) ? ts[0] : ts;
            return name || "";
        } catch (e) {
            alg.log.warn("[bridge] 取不到激活贴图集: " + e);
            return "";
        }
    }

    // 烘焙（同步、会阻塞主线程，脚本化流程里用得着）。
    // 返回空串表示成功，否则返回错误说明。
    function ensureBaked(setName) {
        if (!setName) return "没有可烘焙的贴图集";
        try {
            if (alg.project.isBusy()) return "项目正忙，跳过烘焙";
            alg.log.info("[bridge] 开始烘焙 " + setName + "（可能耗时较久，界面会卡住）");
            alg.baking.bake(setName);
            alg.log.info("[bridge] 烘焙完成: " + setName);
            return "";
        } catch (e) {
            alg.log.warn("[bridge] 烘焙失败: " + e);
            return "烘焙失败: " + e;
        }
    }

    // ---- 接收 ---------------------------------------------------------
    function receiveNow() {
        var r = http("GET", "/inbox?app=" + appName + "&limit=5");
        if (r === null) return -1;          // 传输层失败（用于退避计数）
        if (!r.transfers) return 0;         // 有效响应但无待处理项

        var n = 0;
        for (var i = 0; i < r.transfers.length; ++i) {
            var rec = r.transfers[i];
            if (seenIds[rec.id]) continue;
            var full = http("POST", "/transfers/" + rec.id + "/claim",
                            { session_id: sessionId });
            if (!full || !full.transfer) continue;
            seenIds[rec.id] = true;
            try {
                handleIncoming(full.transfer);
                n++;
            } catch (e) {
                http("POST", "/transfers/" + rec.id + "/finish",
                     { status: "error", result: { message: "" + e } });
                alg.log.error("[bridge] 处理失败: " + e);
            }
        }
        return n;
    }

    // 为「开新项目」腾位子：SP 同一时间只能打开一个工程。
    //
    // 旧实现只 save() 不 close()，于是第二次接收时 create() 必然抛
    // 「Can't create a new project because one is already opened」——
    // 这就是「blender2sp 只能发送一次」的真正原因。
    // 而且 untitled 工程 save() 一定失败（没有路径），所以这里改成：
    //   有未保存内容 -> 能原地 save 就 save，否则 saveAsCopy 备份到负载目录；
    //   然后 close()（用 isOpen() 守卫：无项目时 close() 会抛 ProjectError）。
    function ensureProjectSlot(rec) {
        if (!alg.project.isOpen()) return;

        if (alg.project.needSaving()) {
            var kept = false;
            try {
                alg.project.save();          // 保存过的工程：原地保存
                kept = true;
                alg.log.info("[bridge] 已保存当前工程");
            } catch (e) {
                alg.log.warn("[bridge] 原地保存失败（多半是未命名的工程）: " + e);
            }
            if (!kept) {
                try {
                    var bak = payloadDirFor(rec.id) + "/sp_autosave.spp";
                    alg.project.saveAsCopy("file:///" + bak.split("\\").join("/"));
                    alg.log.warn("[bridge] 已把当前工程另存为备份: " + bak);
                } catch (e2) {
                    alg.log.warn("[bridge] 备份失败，关闭后将丢失未保存内容: " + e2);
                }
            }
        }

        try {
            alg.project.close();
            alg.log.info("[bridge] 已关闭当前工程");
        } catch (e) {
            alg.log.warn("[bridge] 关闭当前工程失败: " + e);
        }
    }

    function handleIncoming(rec) {
        // 远程请求回传贴图：导出 → 建一条新 transfer 发给目标软件 → 回执
        if (rec.kind === "textures_request") {
            handleTexturesRequest(rec);
            return;
        }

        // Toolbag 烘焙结果回传（kind=textures）：导入当前工程资源库
        if (rec.kind === "textures") {
            handleTexturesIncoming(rec);
            return;
        }

        var files = rec._files_abs || [];
        if (!files.length) throw new Error("没有附件");

        var fbx = null;
        for (var i = 0; i < files.length; ++i) {
            if (/\.(fbx|obj)$/i.test(files[i])) { fbx = files[i]; break; }
        }
        if (!fbx) throw new Error("没有可打开的模型文件");

        // 正忙（烘焙/重算）时不动工程，直接报错让它稍后重试，避免半途改坏工程
        if (alg.project.isBusy()) {
            alg.log.warn("[bridge] SP 正忙（烘焙/计算中），本次接收推迟");
            http("POST", "/transfers/" + rec.id + "/finish",
                 { status: "error", result: { message: "SP 正忙，请稍后重试" } });
            return;
        }

        // 关掉当前工程（必要时先备份），腾出位子
        ensureProjectSlot(rec);

        var url = "file:///" + fbx.split("\\").join("/");
        alg.log.info("[bridge] 用新网格开项目: " + fbx);
        try {
            alg.project.create(url);
        } catch (e) {
            alg.log.error("[bridge] 打开项目失败: " + e);
            http("POST", "/transfers/" + rec.id + "/finish",
                 { status: "error", result: { message: "打开失败: " + e } });
            return;
        }
        alg.log.info("[bridge] 已打开 " + rec.asset_name + "（第 " + rec.revision + " 版）");

        http("POST", "/transfers/" + rec.id + "/finish",
             { status: "done", result: { message: "SP 已接收" } });
    }

    // ---- 接收贴图（kind=textures，目前来自 Toolbag 烘焙结果）-----------
    // 把贴图文件导入当前工程的资源库（shelf），并在资源视图里选中，
    // 方便直接拖进图层栈。SP 必须已经打开一个工程——
    // importProjectResource 在无工程时直接抛 ResourcesError。
    function handleTexturesIncoming(rec) {
        var files = rec._files_abs || [];
        var images = [];
        for (var i = 0; i < files.length; ++i) {
            if (/\.(png|jpg|jpeg|tga|tif|tiff|bmp|exr)$/i.test(files[i])) {
                images.push(files[i]);
            }
        }
        if (!images.length) {
            http("POST", "/transfers/" + rec.id + "/finish",
                 { status: "error", result: { message: "没有可导入的贴图文件" } });
            return;
        }

        // 没有打开的项目：importProjectResource 必然失败，直接报错回执
        if (!alg.project.isOpen()) {
            alg.log.warn("[bridge] SP 没有打开的项目，无法接收贴图");
            http("POST", "/transfers/" + rec.id + "/finish",
                 { status: "error",
                   result: { message: "请先在 SP 打开一个项目再接收贴图" } });
            return;
        }

        // 正忙（烘焙/重算）时不动资源库，直接报错让它稍后重试
        if (alg.project.isBusy()) {
            alg.log.warn("[bridge] SP 正忙（烘焙/计算中），本次接收贴图推迟");
            http("POST", "/transfers/" + rec.id + "/finish",
                 { status: "error", result: { message: "SP 正忙，请稍后重试" } });
            return;
        }

        var urls = [];
        var errors = [];
        for (var j = 0; j < images.length; ++j) {
            var p = images[j];
            try {
                // usage 必须与文件兼容：先查 allowedUsages，
                // 优先用 "texture"（普通贴图），不在列表里才取第一个
                var usages = alg.resources.allowedUsages(p);
                var usage = "texture";
                if (!usages || usages.indexOf(usage) < 0) {
                    usage = (usages && usages.length) ? usages[0] : "texture";
                }
                var url = alg.resources.importProjectResource(p, [usage]);
                urls.push(url);
                alg.log.info("[bridge] 已导入贴图资源: " + p + " (" + usage + ")");
            } catch (e) {
                errors.push(p + ": " + e);
                alg.log.warn("[bridge] 导入贴图失败 " + p + ": " + e);
            }
        }

        if (!urls.length) {
            http("POST", "/transfers/" + rec.id + "/finish", {
                status: "error",
                result: { message: "贴图全部导入失败", detail: errors }
            });
            return;
        }

        // 在 shelf 视图里选中刚导入的资源（失败不影响整体结果）
        try {
            alg.resources.selectResources(urls);
        } catch (e2) {
            alg.log.warn("[bridge] 选中导入的资源失败（不影响导入）: " + e2);
        }

        http("POST", "/transfers/" + rec.id + "/finish", {
            status: "done",
            result: {
                message: "SP 已导入 " + urls.length + " 张贴图",
                imported: urls,
                failed: errors
            }
        });
        alg.log.info("[bridge] 已接收 " + rec.asset_name + " 的 "
                     + urls.length + " 张贴图（来自 "
                     + ((rec.from || {}).app || "?") + "）");
    }

    // ---- 回传贴图：公共实现 -------------------------------------------
    // 把当前项目的贴图按预设导出到 outDir，返回 {ok, preset, resolution, paths, error}
    function exportMapsTo(outDir) {
        var out = { ok: false, preset: "", resolution: [], paths: [], error: "" };

        if (!alg.project.isOpen()) {
            out.error = "没有打开的项目";
            return out;
        }
        if (!outDir) {
            out.error = "负载目录不可用";
            return out;
        }

        var preset = exportPreset;
        if (!preset) {
            try { preset = alg.mapexport.getProjectExportPreset(); } catch (e) { preset = ""; }
        }
        if (!preset) preset = "PBR MetalRough";   // 最后兜底

        // 分辨率：取当前激活的贴图集（SP 默认 2048）
        var res = [2048, 2048];
        var setName = activeTextureSetName();
        if (setName) {
            try {
                var r = alg.mapexport.textureSetResolution(setName);
                if (r && r.length >= 2) res = r;
            } catch (e) {
                alg.log.warn("[bridge] 读不到贴图集分辨率，用 2048: " + e);
            }
        }

        var exported = null;
        try {
            exported = alg.mapexport.exportDocumentMaps(
                preset, outDir, exportFormat, { resolution: res });
        } catch (e) {
            out.error = "贴图导出失败: " + e;
            alg.log.error("[bridge] " + out.error);
            return out;
        }

        // 把导出结果摊平；没有导出的 map 是空字符串，必须过滤掉
        var paths = [];
        for (var stack in exported) {
            var maps = exported[stack];
            for (var ch in maps) {
                if (maps[ch]) paths.push(maps[ch]);
            }
        }

        out.ok = true;
        out.preset = preset;
        out.resolution = res;
        out.paths = paths;
        return out;
    }

    // ---- 回传贴图：工具栏按钮（SP -> 默认目标软件）--------------------
    function sendTextures() {
        if (!alg.project.isOpen()) {
            alg.log.warn("[bridge] 没有打开的项目，无贴图可发");
            return;
        }

        var name = currentProjectName() || projectName || "Textures";
        var rec = http("POST", "/transfers", {
            asset_name: name,
            kind: "textures",
            format: exportFormat,
            to: { app: defaultTargetApp, project: defaultTargetProject,
                  content_path: defaultContentPath },
            options: { mode: "update" },
            note: "from Substance Painter"
        });
        if (!rec || !rec.transfer) return;
        var tid = rec.transfer.id;

        if (bakeBeforeExport) {
            ensureBaked(activeTextureSetName());
        }

        // 直接把贴图导出到这条 transfer 的负载目录，省掉一次拷贝
        var r = exportMapsTo(payloadDirFor(tid));
        if (!r.ok || !r.paths.length) {
            alg.log.error("[bridge] 没有导出任何贴图（项目里还没烘焙/绘制？）: " + r.error);
            return;
        }

        http("POST", "/transfers/" + tid + "/publish", {});
        alg.log.info("[bridge] 已回传 " + r.paths.length + " 张贴图（预设 " + r.preset + "）");
    }

    // ---- 回传贴图：远程请求 -------------------------------------------
    // 请求体示例（发给 SP 的收件箱）：
    //   {"asset_name":"SM_SpCube","kind":"textures_request",
    //    "to":{"app":"substance_painter"},
    //    "options":{"target":{"app":"unreal","project":"AssetBridgeTest",
    //                         "content_path":"/Game/Bridge/Textures"}}}
    function handleTexturesRequest(rec) {
        var opts = rec.options || {};
        var target = opts.target || {};
        var name = rec.asset_name || currentProjectName() || "Textures";

        // 新建一条真正承载贴图的 transfer，导出直接落进它的负载目录
        var created = http("POST", "/transfers", {
            asset_name: name,
            kind: "textures",
            format: exportFormat,
            to: {
                app: target.app || defaultTargetApp,
                project: target.project || defaultTargetProject,
                content_path: target.content_path || defaultContentPath
            },
            options: { mode: "update" },
            note: "SP auto-export (requested by " + ((rec.from || {}).app || "?") + ")"
        });
        if (!created || !created.transfer) {
            http("POST", "/transfers/" + rec.id + "/finish",
                 { status: "error", result: { message: "无法创建回传 transfer" } });
            return;
        }

        var outId = created.transfer.id;

        // 默认先烘焙（请求方可以传 options.bake=false 关掉）。
        // 烘焙失败也继续往下走——可能之前已经烘过了，直接导出还是有东西的。
        if (opts.bake === undefined || opts.bake) {
            var bakeErr = ensureBaked(activeTextureSetName());
            if (bakeErr) {
                alg.log.warn("[bridge] " + bakeErr + "，仍继续尝试导出");
            }
        }

        var r = exportMapsTo(payloadDirFor(outId));

        if (!r.ok || !r.paths.length) {
            // 导出失败 / 一张都没导出：把新 transfer 作废（不 publish），请求回执报错
            http("POST", "/transfers/" + rec.id + "/finish", {
                status: "error",
                result: { message: r.error || "预设没有导出任何贴图（项目里还没烘焙/绘制？）" }
            });
            alg.log.error("[bridge] 回传贴图请求失败: " + (r.error || "没有导出任何贴图"));
            return;
        }

        http("POST", "/transfers/" + outId + "/publish", {});
        http("POST", "/transfers/" + rec.id + "/finish", {
            status: "done",
            result: {
                message: "已回传 " + r.paths.length + " 张贴图",
                transfer_id: outId,
                preset: r.preset,
                resolution: r.resolution,
                maps: r.paths
            }
        });
        alg.log.info("[bridge] 已回传 " + r.paths.length + " 张贴图 -> "
                     + (target.app || defaultTargetApp) + "（预设 " + r.preset + "）");
    }

    // ---- 退避 / 熔断 / 日志限频 ---------------------------------------
    // 一次成功：复位失败计数与熔断，恢复正常 1s 轮询。
    function onPollSuccess() {
        pollFailStreak = 0;
        circuitOpen = false;
        nextPollTs = Date.now() + 1000;
    }

    // 一次失败：指数退避 + 日志限频 + 达到阈值熔断。
    function onPollFailure(what) {
        pollFailStreak++;
        // 指数退避：第 n 次失败的间隔 = min(1000 * 2^(n-1), 30000) ms
        var backoff = Math.min(1000 * Math.pow(2, pollFailStreak - 1), 30000);
        nextPollTs = Date.now() + backoff;
        // 失败日志限频：最多每 30 秒一条
        var now = Date.now();
        if (now - lastFailLogTs >= 30000) {
            lastFailLogTs = now;
            alg.log.warn("[bridge] 中转服务通信失败(" + pollFailStreak + "): " + what);
        }
        // 连续失败 >=5 次：熔断，输出明确提示，之后按 30s 间隔探测恢复
        if (pollFailStreak >= 5 && !circuitOpen) {
            circuitOpen = true;
            lastProbeTs = Date.now();
            alg.log.error("[bridge] 中转服务不可达，已暂停轮询。请先启动 start_bridge_server.bat");
        }
    }

    // ---- 定时轮询 -----------------------------------------------------
    onTick: {
        tickCount++;
        var now = Date.now();

        // 时间闸门：距上次真正轮询 < 1000ms 直接返回（不依赖 tickIntervalMS）。
        // 这一步同时实现了「退避」——失败时 onPollFailure 会把 nextPollTs 推远。
        if (now < nextPollTs) {
            return;
        }

        // 熔断后：仅按 30s 间隔做一次轻量探测，恢复前不碰收件箱。
        if (circuitOpen) {
            if (now - lastProbeTs < 30000) {
                return;
            }
            lastProbeTs = now;
            if (!sessionId) {
                register();
            } else {
                http("POST", "/sessions/" + sessionId + "/heartbeat", {});
            }
            if (sessionId) {
                circuitOpen = false;
                pollFailStreak = 0;
                alg.log.info("[bridge] 中转服务已恢复，恢复轮询");
            } else {
                return;
            }
        }

        // 注册重试：未拿到 sessionId 前只尝试 register，绝不发 claim。
        if (!sessionId) {
            register();
            if (!sessionId) {
                onPollFailure("注册失败");
                return;
            }
            alg.log.info("[bridge] 已重新注册 SP 会话 " + sessionId);
        }

        // 心跳：每 30 个真实轮询计数一次（tickCount 已是「真正轮询」的计数）。
        if (tickCount % 30 === 1 && sessionId) {
            http("POST", "/sessions/" + sessionId + "/heartbeat", {});
        }

        var n = receiveNow();
        if (n < 0) {
            onPollFailure("收件箱请求失败");
        } else {
            onPollSuccess();
        }
    }
}
