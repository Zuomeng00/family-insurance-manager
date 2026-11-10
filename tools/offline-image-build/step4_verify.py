"""Step 4: 对组装出来的镜像 tar 做严格校验。

因为本机没有 Docker，无法 `docker load` 实跑，所以用静态校验把能查的全查死：
  A. tar 结构 / manifest.json / repositories / config 文件名与内容摘要
  B. config.rootfs.diff_ids 与每层「解压后」摘要逐一匹配（层顺序也要对）
  C. 逐层叠加出最终文件系统，检查关键路径、类型、权限、符号链接目标
  D. 静态解析 app/ 全部 import，证明依赖树完整（无缺包）
  E. 控制台脚本（uvicorn 等）的 shebang 与导入目标正确
"""
import ast
import gzip
import hashlib
import io
import json
import os
import posixpath
import sys
import tarfile
import tempfile

WORK = os.environ.get("IMGBUILD_WORK") or os.path.join(tempfile.gettempdir(), "_imgbuild")
REPO = os.environ.get("IMGBUILD_REPO") or os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)
OUT_TAR = os.path.join(REPO, "dist", "family-insurance-manager-1.9.0-amd64.tar")
SP = "usr/local/lib/python3.13/site-packages"

PASS, FAIL = [], []


def chk(cond, label, detail=""):
    (PASS if cond else FAIL).append(label)
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", label, ("  <- " + detail) if detail and not cond else ""))


def main():
    print("=" * 74)
    print("A. tar 结构")
    print("=" * 74)
    tf = tarfile.open(OUT_TAR, "r")
    names = tf.getnames()
    print("  tar 条目数 =", len(names))
    for must in ("manifest.json", "repositories"):
        chk(must in names, "存在 %s" % must)

    manifest = json.loads(tf.extractfile("manifest.json").read())
    repositories = json.loads(tf.extractfile("repositories").read())
    m = manifest[0]
    print("  RepoTags =", m["RepoTags"])
    print("  Config   =", m["Config"])
    print("  Layers   = %d 层" % len(m["Layers"]))
    chk(m["RepoTags"] == ["family-insurance-manager:1.9.0"], "RepoTags 正确")
    chk(len(m["Layers"]) == 6, "层数为 6", "实际 %d" % len(m["Layers"]))

    cfg_bytes = tf.extractfile(m["Config"]).read()
    chk(hashlib.sha256(cfg_bytes).hexdigest() + ".json" == m["Config"],
        "config 文件名 == 其内容 sha256（镜像 ID 自洽）")
    cfg = json.loads(cfg_bytes)
    chk(repositories.get("family-insurance-manager", {}).get("1.9.0") is not None,
        "repositories 含 family-insurance-manager:1.9.0")

    # --- 经典 docker save 的「三件套」完整性：每层必须有 VERSION + json + layer.tar ---
    print("  -- 层目录三件套（VERSION + json + layer.tar）--")
    trio_ok, trio_bad = 0, []
    for lp in m["Layers"]:
        d = posixpath.dirname(lp)
        have = sorted(posixpath.basename(n) for n in names if n.startswith(d + "/"))
        if have == ["VERSION", "json", "layer.tar"]:
            trio_ok += 1
        else:
            trio_bad.append((d[:16], have))
    chk(trio_ok == len(m["Layers"]),
        "全部 %d 个层目录都含 VERSION+json+layer.tar" % len(m["Layers"]),
        "异常层: %s" % trio_bad)

    # --- history 与 rootfs.diff_ids 的关系（Docker 的实际校验规则）---
    # moby image/store.go Create():  layerCounter > len(RootFS.DiffIDs)  -> 硬失败
    #   "too many non-empty layers in History section"
    # moby tarexport/load.go:        len(manifest.Layers) != len(RootFS.DiffIDs) -> 硬失败
    hist = cfg.get("history", [])
    nonempty = [h for h in hist if not h.get("empty_layer")]
    diff_ids = cfg["rootfs"]["diff_ids"]
    print("  history %d 条（非空 %d 条），层数 %d" % (len(hist), len(nonempty), len(diff_ids)))
    chk(len(nonempty) <= len(diff_ids),
        "history 非空条目数 <= 层数（超出会被 docker load 报 'too many non-empty layers'）",
        "%d > %d" % (len(nonempty), len(diff_ids)))
    chk(len(nonempty) == len(diff_ids),
        "history 非空条目数 == 层数（一一对应，语义精确）",
        "%d != %d" % (len(nonempty), len(diff_ids)))
    chk(len(m["Layers"]) == len(diff_ids),
        "manifest.Layers 数量 == rootfs.diff_ids 数量（load.go 硬校验）",
        "%d != %d" % (len(m["Layers"]), len(diff_ids)))
    bad_hist = [h for h in hist if "created" not in h or "created_by" not in h]
    chk(not bad_hist, "每条 history 都含 created 与 created_by", str(bad_hist[:2]))

    # config 里不应出现 JSON null（严格校验器会把 null 当成类型错误拒绝）
    nulls = [k for k, v in cfg.items() if v is None]
    chk(not nulls, "config 顶层无 null 值", str(nulls))
    nulls2 = [k for k, v in cfg.get("config", {}).items() if v is None]
    chk(not nulls2, "config.config 段无 null 值", str(nulls2))

    # --- 每层 json 内容合法性 ---
    print("  -- 每层 json 元数据 --")
    json_bad = []
    for lp in m["Layers"]:
        d = posixpath.dirname(lp)
        try:
            j = json.loads(tf.extractfile(d + "/json").read())
        except Exception as e:  # noqa: BLE001
            json_bad.append((d[:16], repr(e)))
            continue
        if j.get("id") != d or j.get("os") != "linux" or j.get("architecture") != "amd64":
            json_bad.append((d[:16], j.get("id"), j.get("os"), j.get("architecture")))
    chk(not json_bad, "每层 json 的 id/os/architecture 均正确", str(json_bad[:3]))

    # --- repositories 的值必须是「最上层」的摘要（moby save.go 的语义）---
    top = posixpath.dirname(m["Layers"][-1])
    chk(repositories.get("family-insurance-manager", {}).get("1.9.0") == top,
        "repositories 的值 == 最上层目录名（与 moby save.go 一致）",
        "%r != %r" % (repositories.get("family-insurance-manager", {}).get("1.9.0"), top))

    print()
    print("=" * 74)
    print("B. 层摘要校验（解压后 sha256 == config.rootfs.diff_ids）")
    print("=" * 74)
    diff_ids = cfg["rootfs"]["diff_ids"]
    chk(len(diff_ids) == len(m["Layers"]), "diff_ids 数量 == 层数")
    layers_raw = []
    for i, (lp, want) in enumerate(zip(m["Layers"], diff_ids)):
        raw = tf.extractfile(lp).read()
        data = gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw
        got = "sha256:" + hashlib.sha256(data).hexdigest()
        layers_raw.append(data)
        chk(got == want, "层 %d 摘要匹配  %s" % (i, lp.split("/")[0][:16]),
            "got=%s want=%s" % (got[:20], want[:20]))

    print()
    print("=" * 74)
    print("C. 逐层叠加出的最终文件系统")
    print("=" * 74)
    fs = {}   # path -> dict(type, mode, link, size, layer)
    order = []
    for i, data in enumerate(layers_raw):
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as lt:
            for mem in lt.getmembers():
                nm = mem.name.rstrip("/")
                if not nm:
                    continue
                # whiteout 处理
                base = posixpath.basename(nm)
                if base.startswith(".wh."):
                    tgt = posixpath.join(posixpath.dirname(nm), base[4:])
                    for k in [k for k in fs if k == tgt or k.startswith(tgt + "/")]:
                        fs.pop(k, None)
                    continue
                fs[nm] = {"type": mem.type, "mode": mem.mode, "link": mem.linkname,
                          "size": mem.size, "layer": i}
                order.append((nm, i))
    print("  合并后文件系统条目数 =", len(fs))

    def has(p):
        return p in fs

    def isdir(p):
        return p in fs and fs[p]["type"] == tarfile.DIRTYPE

    def isfile(p):
        return p in fs and fs[p]["type"] == tarfile.REGTYPE

    def issym(p):
        return p in fs and fs[p]["type"] == tarfile.SYMTYPE

    print("  -- 基础运行环境 --")
    chk(has("usr/local/bin/python3.13"), "Python 解释器 usr/local/bin/python3.13")
    chk(has("usr/local/lib/python3.13/site-packages"), "site-packages 目录")
    # Debian 合并 /usr：/bin 是指向 usr/bin 的符号链接，sh 实际在 usr/bin/sh
    bin_sym = issym("bin") and fs["bin"]["link"] in ("usr/bin", "/usr/bin")
    chk(has("usr/bin/sh") or bin_sym, "shell（usr/bin/sh 或 /bin -> usr/bin）",
        "bin=%r usr/bin/sh=%s" % (fs.get("bin"), has("usr/bin/sh")))

    print("  -- 时区（复刻 Dockerfile 的 apt+ln 两步）--")
    chk(has("usr/share/zoneinfo/Asia/Shanghai"), "usr/share/zoneinfo/Asia/Shanghai 存在（来自基础层）")
    chk(issym("etc/localtime"), "etc/localtime 是符号链接", str(fs.get("etc/localtime")))
    chk(fs.get("etc/localtime", {}).get("link") == "/usr/share/zoneinfo/Asia/Shanghai",
        "etc/localtime -> /usr/share/zoneinfo/Asia/Shanghai",
        str(fs.get("etc/localtime", {}).get("link")))
    tz_txt = b""
    if has("etc/timezone"):
        with tarfile.open(fileobj=io.BytesIO(layers_raw[fs["etc/timezone"]["layer"]]), mode="r:") as lt:
            tz_txt = lt.extractfile("etc/timezone").read()
    chk(tz_txt.strip() == b"Asia/Shanghai", "/etc/timezone 内容为 Asia/Shanghai", repr(tz_txt))

    print("  -- 应用代码（WORKDIR /app，COPY app ./app）--")
    for p in ("app/app", "app/app/main.py", "app/app/models.py", "app/app/schemas.py",
              "app/app/config.py", "app/app/__init__.py",
              "app/app/services/attachment_service.py", "app/app/routers/attachments.py",
              "app/static", "app/static/index.html", "app/requirements.txt", "app/data"):
        chk(has(p), p)
    chk(isdir("app/data"), "app/data 是目录（可被 volume 挂载）")
    chk(not has("app/main.py"), "不存在错误的 app/main.py（层级必须为 /app/app）")

    print("  -- 控制台脚本 --")
    for s in ("uvicorn", "fastapi", "dotenv", "idna", "watchfiles", "websockets"):
        p = "usr/local/bin/" + s
        chk(isfile(p), p)
        if isfile(p):
            chk(fs[p]["mode"] & 0o111 != 0, "  %s 有可执行位 (mode=%o)" % (s, fs[p]["mode"]))
    chk(not has("usr/local/bin/uvicorn.exe"), "不存在 Windows 版 uvicorn.exe")

    print()
    print("=" * 74)
    print("D. 静态 import 完整性（证明镜像里不缺任何依赖）")
    print("=" * 74)
    # 收集 site-packages 中可导入的顶层名
    available = set()
    for p in fs:
        if not p.startswith(SP + "/"):
            continue
        rel = p[len(SP) + 1:]
        top = rel.split("/")[0]
        if not top or top.endswith(".dist-info") or top.startswith("_distutils_hack"):
            continue
        if "." in top:
            base = top.split(".")[0]
            available.add(base)
        else:
            available.add(top)
    print("  site-packages 顶层模块数 =", len(available))

    # 解析 app 全部 import
    app_root = os.path.join(REPO, "app")
    local_mods = set()
    for dp, dns, fns in os.walk(app_root):
        dns[:] = [d for d in dns if d != "__pycache__"]
        for fn in fns:
            if fn.endswith(".py"):
                rel = os.path.relpath(os.path.join(dp, fn), app_root).replace("\\", "/")
                if rel.endswith("/__init__.py"):
                    local_mods.add(rel[:-len("/__init__.py")].replace("/", "."))
                elif rel == "__init__.py":
                    local_mods.add("")
                else:
                    local_mods.add(rel[:-3].replace("/", "."))
    local_top = {x.split(".")[0] for x in local_mods if x}

    stdlib = set(sys.stdlib_module_names)
    missing, checked = [], set()
    for dp, dns, fns in os.walk(app_root):
        dns[:] = [d for d in dns if d != "__pycache__"]
        for fn in fns:
            if not fn.endswith(".py"):
                continue
            src = open(os.path.join(dp, fn), "r", encoding="utf-8").read()
            try:
                tree = ast.parse(src)
            except SyntaxError as e:
                chk(False, "语法错误 %s" % fn, str(e))
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for a in node.names:
                        checked.add(a.name.split(".")[0])
                elif isinstance(node, ast.ImportFrom):
                    if node.level and node.level > 0:
                        continue
                    if node.module:
                        checked.add(node.module.split(".")[0])
    for mod in sorted(checked):
        if mod in stdlib or mod in local_top or mod in available:
            continue
        missing.append(mod)
    print("  app 引用的顶层模块 %d 个: %s" % (len(checked), ", ".join(sorted(checked))))
    chk(not missing, "所有 import 均可在镜像内解析（无缺失依赖）", "缺失: %s" % missing)

    # 关键三方包必须存在
    for must in ("fastapi", "uvicorn", "sqlalchemy", "pydantic", "pydantic_settings",
                 "openpyxl", "multipart", "starlette", "anyio", "greenlet", "yaml"):
        chk(must in available, "site-packages 含 %s" % must)

    print()
    print("=" * 74)
    print("E. 控制台脚本内容 / config 关键字段")
    print("=" * 74)
    with tarfile.open(fileobj=io.BytesIO(layers_raw[fs["usr/local/bin/uvicorn"]["layer"]]), mode="r:") as lt:
        body = lt.extractfile("usr/local/bin/uvicorn").read().decode()
    print("  --- /usr/local/bin/uvicorn ---")
    for line in body.splitlines():
        print("     ", line)
    chk(body.startswith("#!/usr/local/bin/python3.13"), "shebang 指向镜像内真实解释器")
    chk("from uvicorn.main import main" in body, "导入目标 uvicorn.main:main 正确")
    chk("sys.exit(main())" in body, "以 main() 返回值退出")

    c = cfg["config"]
    print()
    print("  WorkingDir =", c.get("WorkingDir"))
    print("  Cmd        =", c.get("Cmd"))
    print("  ExposedPorts =", c.get("ExposedPorts"))
    print("  Healthcheck  =", json.dumps(c.get("Healthcheck"), ensure_ascii=False)[:150])
    print("  Labels       =", json.dumps(c.get("Labels"), ensure_ascii=False)[:200])
    chk(c.get("WorkingDir") == "/app", "WORKDIR = /app")
    chk(c.get("Cmd") == ["sh", "-c", 'exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"'],
        "CMD 与 Dockerfile 一致")
    chk(c.get("ExposedPorts") == {"8000/tcp": {}}, "EXPOSE 8000")
    chk(c.get("Healthcheck", {}).get("Test", [None])[0] == "CMD-SHELL", "HEALTHCHECK 为 CMD-SHELL")
    chk(c.get("Healthcheck", {}).get("Interval") == 30_000_000_000, "healthcheck interval 30s")
    chk(c.get("Labels", {}).get("org.opencontainers.image.version") == "1.9.0", "LABEL version=1.9.0")
    chk(cfg.get("architecture") == "amd64" and cfg.get("os") == "linux", "平台 linux/amd64")
    env = dict(e.split("=", 1) for e in c.get("Env", []) if "=" in e)
    for k, v in [("PYTHONUNBUFFERED", "1"), ("PYTHONDONTWRITEBYTECODE", "1"), ("PIP_NO_CACHE_DIR", "1"),
                 ("DB_PATH", "/app/data/insurance.db"), ("PORT", "8000"), ("TZ", "Asia/Shanghai")]:
        chk(env.get(k) == v, "ENV %s=%s" % (k, v), str(env.get(k)))
    chk("PATH" in env and "/usr/local/bin" in env["PATH"], "PATH 保留且含 /usr/local/bin")

    print()
    print("=" * 74)
    print("结果：PASS %d   FAIL %d" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  -", f)
    print("=" * 74)
    json.dump({"pass": len(PASS), "fail": len(FAIL), "failures": FAIL},
              open(os.path.join(WORK, "verify_result.json"), "w", encoding="utf-8"), indent=2)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
