"""Step 3 (v2): 不依赖 Docker，按 `docker save` 经典格式组装出可 `docker load` 的镜像 tar。

层顺序：4 个基础层（官方 python:3.13-slim 原始 blob，字节级未改动）
        + 依赖层（site-packages + /usr/local/bin 的 POSIX 控制台脚本 + /etc/localtime、/etc/timezone）
        + 应用层（/app/app、/app/static、/app/data）

config 严格复刻 Dockerfile：ENV / WORKDIR / CMD / EXPOSE / LABEL / HEALTHCHECK。

关键修正（相对 v1）：
1. pip 在 Windows 上跑，bin/ 里生成的是 *.exe（Windows 启动器）——必须按 entry_points.txt
   重新生成 POSIX 控制台脚本，否则 Linux 容器里没有 uvicorn 命令。
2. 基础镜像已自带 /usr/share/zoneinfo（443 个真实 TZif 文件），因此不覆盖时区库；
   只复刻 Dockerfile 中真正生效的两步：/etc/localtime 符号链接 + /etc/timezone。
3. 不安装 PyPI 的 tzdata 包（Dockerfile 里没有它）。
4. 【v3 修复·根因】history 中非空条目数不得【多于】层数。初版把 Dockerfile 的 8 条指令
   全写成非空层，得到 11 条非空 history 却只有 6 个层。moby image/store.go 的 Create() 里
   判据是 `layerCounter > len(RootFS.DiffIDs)` → 报
   "too many non-empty layers in History section"，而 docker load 在 manifest.json 存在时
   必然走这条路径 → 导入直接失败。现在只保留 2 条非空（依赖层 / 应用层），
   其余标 empty_layer，并在写出前自检。
5. 【v3 加固】每层目录补齐 json 文件，凑成经典 docker save 的 VERSION + json + layer.tar 三件套。
   注：manifest.json 存在时 docker load 走现代路径，并不读这个 json（只有无 manifest.json 的
   legacy 路径才读）；补上是为了兼容「按经典格式解析」的第三方导入器，属保真度加固。
6. 【v3 加固】去掉 config 里的 variant: null（Go 能解析 null，但严格 JSON Schema 会当类型错误）。
"""
import gzip
import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile

WORK = os.environ.get("IMGBUILD_WORK") or os.path.join(tempfile.gettempdir(), "_imgbuild")
BASE = os.path.join(WORK, "base")
DEPS = os.path.join(WORK, "layer_deps")
REPO = os.environ.get("IMGBUILD_REPO") or os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)
OUT_DIR = os.path.join(REPO, "dist")
OUT_TAR = os.path.join(OUT_DIR, "family-insurance-manager-1.8.0-amd64.tar")
TMP = os.path.join(WORK, "tmp")

IMAGE_REF = "family-insurance-manager"
IMAGE_TAG = "1.8.0"
EPOCH = 1758038400  # 固定 mtime，保证可复现
SP = "usr/local/lib/python3.13/site-packages"
PYBIN = "/usr/local/bin/python3.13"
TZ_NAME = "Asia/Shanghai"

SKIP_PREFIXES = ("tzdata/",)
SKIP_DIST_INFO = ("tzdata-",)

MY_ENV = {
    "PYTHONUNBUFFERED": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PIP_NO_CACHE_DIR": "1",
    "DB_PATH": "/app/data/insurance.db",
    "PORT": "8000",
    "TZ": TZ_NAME,
}

MY_LABELS = {
    "org.opencontainers.image.title": "family-insurance-manager",
    "org.opencontainers.image.version": IMAGE_TAG,
    "org.opencontainers.image.description": "家庭保险整理工具：本地优先的家庭保单登记、售后信息归档、电子保单 PDF 归集、年化保费计算与看板统计",
    "org.opencontainers.image.source": "https://example.local/family-insurance-manager",
    "org.opencontainers.image.licenses": "MIT",
    "org.opencontainers.image.authors": "family-insurance-manager",
}

HEALTH_CMD = ("python -c \"import os,sys,urllib.request; port=os.environ.get('PORT','8000'); "
              "sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health', "
              "timeout=4).status == 200 else 1)\"")

CMD = ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port \"${PORT:-8000}\""]

SCRIPT_TPL = """#!{py}
# -*- coding: utf-8 -*-
import re
import sys
from {module} import {func}
if __name__ == '__main__':
    sys.argv[0] = re.sub(r'(-script\\.pyw|\\.exe)?$', '', sys.argv[0])
    sys.exit({func}())
"""


# ---------------------------------------------------------------- tar helpers
def _ti(name, mode, mtime=EPOCH):
    ti = tarfile.TarInfo(name)
    ti.mode = mode
    ti.mtime = mtime
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = "root"
    return ti


def add_dir(tf, name, mode=0o755):
    ti = _ti(name.rstrip("/") + "/", mode)
    ti.type = tarfile.DIRTYPE
    tf.addfile(ti)


def add_bytes(tf, name, data, mode=0o644):
    ti = _ti(name, mode)
    ti.size = len(data)
    tf.addfile(ti, io.BytesIO(data))


def add_symlink(tf, name, target):
    ti = _ti(name, 0o777)
    ti.type = tarfile.SYMTYPE
    ti.linkname = target
    tf.addfile(ti)


def add_file(tf, name, src, mode=0o644):
    ti = _ti(name, mode)
    ti.size = os.path.getsize(src)
    with open(src, "rb") as f:
        tf.addfile(ti, f)


def walk_files(root):
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d != "__pycache__"]
        for fn in fns:
            if fn.endswith((".pyc", ".pyo")):
                continue
            full = os.path.join(dp, fn)
            yield full, os.path.relpath(full, root).replace("\\", "/")


def gen_console_scripts():
    """按 *.dist-info/entry_points.txt 生成 POSIX 控制台脚本（替代 Windows 的 .exe）。"""
    out = {}
    for dp, dns, fns in os.walk(DEPS):
        if not dp.endswith(".dist-info"):
            continue
        ep = os.path.join(dp, "entry_points.txt")
        if not os.path.exists(ep):
            continue
        section = None
        for line in open(ep, "r", encoding="utf-8", errors="replace"):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if s.startswith("[") and s.endswith("]"):
                section = s[1:-1]
                continue
            if section == "console_scripts" and "=" in s:
                name, _, target = s.partition("=")
                name = name.strip()
                target = target.strip()
                if ":" not in target:
                    continue
                mod, _, fn = target.partition(":")
                out[name] = SCRIPT_TPL.format(py=PYBIN, module=mod.strip(), func=fn.strip())
    return out


def build_deps_layer(path_plain):
    print("  构建依赖层 ...")
    scripts = gen_console_scripts()
    print("    生成控制台脚本 %d 个: %s" % (len(scripts), ", ".join(sorted(scripts))))

    dirs = {"usr", "usr/local", "usr/local/bin", "usr/local/lib",
            "usr/local/lib/python3.13", SP, "etc"}
    files = []          # (arcname, srcpath, mode)
    n_exe_skipped = 0
    n_tzdata_skipped = 0

    for full, rel in walk_files(DEPS):
        top = rel.split("/")[0]
        if rel.startswith(SKIP_PREFIXES) or top.startswith(SKIP_DIST_INFO):
            n_tzdata_skipped += 1
            continue
        if rel.startswith("bin/"):
            if rel.endswith(".exe"):
                n_exe_skipped += 1
                continue
            arc, mode = "usr/local/bin/" + rel[4:], 0o755
        else:
            arc, mode = SP + "/" + rel, 0o644
        files.append((arc, full, mode))
        parts = arc.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            dirs.add("/".join(parts[:i]))

    # 应用层与依赖层分开：这里只处理 /usr、/etc
    with tarfile.open(path_plain, "w", format=tarfile.PAX_FORMAT) as tf:
        for d in sorted(dirs):
            add_dir(tf, d)
        for arc, full, mode in sorted(files):
            add_file(tf, arc, full, mode)
        for name, body in sorted(scripts.items()):
            add_bytes(tf, "usr/local/bin/" + name, body.encode(), 0o755)
        # 复刻 Dockerfile 中 apt 之后真正生效的两步
        add_symlink(tf, "etc/localtime", "/usr/share/zoneinfo/" + TZ_NAME)
        add_bytes(tf, "etc/timezone", (TZ_NAME + "\n").encode())
    print("    条目 %d（跳过 tzdata %d 项、Windows .exe %d 项），未压缩 %.1f MB" %
          (len(files) + len(dirs) + len(scripts) + 2, n_tzdata_skipped, n_exe_skipped,
           os.path.getsize(path_plain) / 1024 ** 2))
    return len(files) + len(dirs) + len(scripts) + 2


def build_app_layer(path_plain):
    print("  构建应用层 ...")
    # 严格复刻 Dockerfile（WORKDIR /app）：
    #   COPY requirements.txt ./  -> /app/requirements.txt
    #   COPY app ./app            -> /app/app/**
    #   COPY static ./static      -> /app/static/**
    #   RUN mkdir -p /app/data    -> /app/data/
    dirs = {"app", "app/app", "app/static", "app/data"}
    files = []
    for sub in ("app", "static"):
        for full, rel in walk_files(os.path.join(REPO, sub)):
            arc = "app/" + sub + "/" + rel
            files.append((arc, full, 0o644))
            parts = arc.split("/")[:-1]
            for i in range(1, len(parts) + 1):
                dirs.add("/".join(parts[:i]))
    req = os.path.join(REPO, "requirements.txt")
    files.append(("app/requirements.txt", req, 0o644))
    with tarfile.open(path_plain, "w", format=tarfile.PAX_FORMAT) as tf:
        for d in sorted(dirs):
            add_dir(tf, d)
        for arc, full, mode in sorted(files):
            add_file(tf, arc, full, mode)
    print("    条目 %d，未压缩 %.1f MB" % (len(files) + len(dirs), os.path.getsize(path_plain) / 1024 ** 2))
    return len(files) + len(dirs)


def gzip_file(src, dst):
    with open(src, "rb") as fi, gzip.GzipFile(dst, "wb", compresslevel=9, mtime=0) as fo:
        shutil.copyfileobj(fi, fo, 1 << 20)
    return os.path.getsize(dst)


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    os.makedirs(TMP, exist_ok=True)

    info = json.load(open(os.path.join(BASE, "pull_info.json"), "r", encoding="utf-8"))
    base_cfg = json.load(open(os.path.join(BASE, "config.json"), "r", encoding="utf-8"))

    print("[1] 构建自定义层")
    deps_plain = os.path.join(TMP, "layer_deps.tar")
    deps_gz = os.path.join(TMP, "layer_deps.tar.gz")
    app_plain = os.path.join(TMP, "layer_app.tar")
    app_gz = os.path.join(TMP, "layer_app.tar.gz")
    build_deps_layer(deps_plain)
    build_app_layer(app_plain)
    gzip_file(deps_plain, deps_gz)
    gzip_file(app_plain, app_gz)
    deps_diff = "sha256:" + sha256_file(deps_plain)
    app_diff = "sha256:" + sha256_file(app_plain)
    print("    依赖层 diff_id = %s  (gz %.1f MB)" % (deps_diff[:23], os.path.getsize(deps_gz) / 1024 ** 2))
    print("    应用层 diff_id = %s  (gz %.1f MB)" % (app_diff[:23], os.path.getsize(app_gz) / 1024 ** 2))

    print("[2] 生成 config JSON")
    cfg = json.loads(json.dumps(base_cfg))
    env, idx = [], {}
    for item in cfg["config"].get("Env", []):
        idx[item.split("=", 1)[0]] = len(env)
        env.append(item)
    for k, v in MY_ENV.items():
        if k in idx:
            env[idx[k]] = "%s=%s" % (k, v)
        else:
            env.append("%s=%s" % (k, v))

    labels = dict(cfg["config"].get("Labels") or {})
    labels.update(MY_LABELS)

    cfg["config"]["Env"] = env
    cfg["config"]["WorkingDir"] = "/app"
    cfg["config"]["Cmd"] = CMD
    cfg["config"]["ExposedPorts"] = {"8000/tcp": {}}
    cfg["config"]["Labels"] = labels
    cfg["config"]["Healthcheck"] = {
        "Test": ["CMD-SHELL", HEALTH_CMD],
        "Interval": 30_000_000_000,
        "Timeout": 5_000_000_000,
        "StartPeriod": 10_000_000_000,
        "Retries": 3,
    }
    cfg["config"].pop("Entrypoint", None)
    cfg["config"]["Hostname"] = ""
    cfg["config"]["Domainname"] = ""
    cfg["config"]["User"] = ""
    cfg["config"]["AttachStdin"] = False
    cfg["config"]["AttachStdout"] = False
    cfg["config"]["AttachStderr"] = False
    cfg["config"]["Tty"] = False
    cfg["config"]["OpenStdin"] = False
    cfg["config"]["StdinOnce"] = False
    cfg["config"]["ArgsEscaped"] = False

    cfg["rootfs"]["diff_ids"] = list(info["diff_ids"]) + [deps_diff, app_diff]

    # ⚠️ 硬规则（来自 moby image/store.go 的 Create()，原文）：
    #      layerCounter := 0
    #      for _, h := range img.History { if !h.EmptyLayer { layerCounter++ } }
    #      if layerCounter > len(img.RootFS.DiffIDs) {
    #          return "", errdefs.InvalidParameter(errors.New("too many non-empty layers in History section"))
    #      }
    #    → 判据是【大于】，即「非空 history 条目数不得多于层数」；等于或更少都能通过。
    #    docker load 在 manifest.json 存在时会走这条路径（见 tarexport/load.go 里
    #    `imgID, err := l.is.Create(config)`），所以超了就是硬失败。
    #    本方案把 Dockerfile 的 6 个内容层合并成了 2 个（依赖层 / 应用层），
    #    所以只有「RUN pip install」与「COPY app ./app」两条是非空层，其余指令都要标 empty_layer。
    MY_HISTORY = [
        ("ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple", True, None),
        ("ARG TZ=Asia/Shanghai", True, None),
        ("RUN apt-get update && apt-get install -y --no-install-recommends tzdata "
         "&& ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone "
         "&& rm -rf /var/lib/apt/lists/*", True, "已合并进依赖层"),
        ("COPY requirements.txt ./", True, "已合并进依赖层"),
        ("RUN pip install --no-cache-dir --index-url \"$PIP_INDEX_URL\" -r requirements.txt", False, None),
        ("COPY app ./app", False, None),
        ("COPY static ./static", True, "已合并进应用层"),
        ("RUN mkdir -p /app/data", True, "已合并进应用层"),
    ]
    hist_add = []
    for i, (created_by, is_empty, comment) in enumerate(MY_HISTORY):
        h = {"created": "2025-09-17T00:00:%02dZ" % i, "created_by": created_by}
        if is_empty:
            h["empty_layer"] = True
        if comment:
            h["comment"] = comment
        hist_add.append(h)
    cfg["history"] = list(cfg.get("history", [])) + hist_add

    # 自检：镜像规范 + Docker 的实际校验
    n_nonempty = sum(1 for h in cfg["history"] if not h.get("empty_layer"))
    n_layers = len(cfg["rootfs"]["diff_ids"])
    if n_nonempty > n_layers:
        # 这是 docker load 的硬失败条件（image/store.go: "too many non-empty layers in History section"）
        raise SystemExit(
            "FATAL: history 非空条目数(%d) 多于层数(%d)——docker load 会直接报 "
            "'too many non-empty layers in History section'，已中止。"
            % (n_nonempty, n_layers))
    if n_nonempty != n_layers:
        print("    ⚠️ 警告：history 非空 %d 条 != 层数 %d（未超上限，Docker 能加载，但语义不精确）"
              % (n_nonempty, n_layers))
    print("    history %d 条（其中非空 %d 条 == 层数 %d）✓"
          % (len(cfg["history"]), n_nonempty, n_layers))

    cfg["architecture"] = "amd64"
    cfg["os"] = "linux"
    # variant 必须是字符串或干脆不出现；写成 null 会被严格 JSON Schema 校验器拒绝
    if not cfg.get("variant"):
        cfg.pop("variant", None)
    cfg.pop("docker_version", None)
    cfg.pop("container", None)
    cfg.pop("container_config", None)

    cfg_bytes = json.dumps(cfg, separators=(",", ":")).encode()
    cfg_digest = hashlib.sha256(cfg_bytes).hexdigest()
    cfg_name = cfg_digest + ".json"
    print("    config %.1f KB   镜像 ID = sha256:%s" % (len(cfg_bytes) / 1024, cfg_digest))
    print("    ENV:", cfg["config"]["Env"])

    print("[3] 写出镜像 tar（经典 docker save 格式，层为未压缩 tar）")
    # 经典格式里 layer.tar 按惯例是「未压缩」的 tar；这里严格照此输出，
    # 避免任何导入器对 gzip 层的容忍度差异（Docker 1.x~27.x 全兼容）。
    base_entries = []
    for f, d in zip(info["layer_files"], info["diff_ids"]):
        base_entries.append((d.split(":")[1], os.path.join(BASE, "layers", f), True))
    entries = base_entries + [
        (deps_diff.split(":")[1], deps_plain, False),
        (app_diff.split(":")[1], app_plain, False),
    ]

    manifest = [{"Config": cfg_name,
                 "RepoTags": ["%s:%s" % (IMAGE_REF, IMAGE_TAG)],
                 "Layers": ["%s/layer.tar" % n for n, _, _ in entries]}]
    repositories = {IMAGE_REF: {IMAGE_TAG: entries[-1][0]}}

    out_tmp = OUT_TAR + ".part"
    # 每层的 created 时间：取 history 里第 i 个「非空」条目的 created，与层一一对应
    layer_created = [h["created"] for h in cfg["history"] if not h.get("empty_layer")]
    assert len(layer_created) == len(entries), (len(layer_created), len(entries))
    with tarfile.open(out_tmp, "w", format=tarfile.PAX_FORMAT) as tf:
        for idx, (name, src, is_gz) in enumerate(entries):
            add_dir(tf, name)
            add_bytes(tf, name + "/VERSION", b"1.0")
            # 经典 docker save 的每层目录是 VERSION + json + layer.tar 三件套。
            # 缺 json 会让「按经典格式解析」的导入器认为 tar 不完整。
            # json 内容是 legacy v1 层元数据（image.V1Image 的字段子集）。
            v1 = {"id": name, "created": layer_created[idx],
                  "container_config": {}, "config": {},
                  "architecture": "amd64", "os": "linux"}
            if idx > 0:
                v1["parent"] = entries[idx - 1][0]
            add_bytes(tf, name + "/json", json.dumps(v1).encode())
            if is_gz:
                raw = open(src, "rb").read()
                data = gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw
                add_bytes(tf, name + "/layer.tar", data, 0o644)
                del raw
            else:
                add_file(tf, name + "/layer.tar", src, 0o644)
        add_bytes(tf, cfg_name, cfg_bytes)
        add_bytes(tf, "manifest.json", json.dumps(manifest, indent=3).encode())
        add_bytes(tf, "repositories", json.dumps(repositories).encode())
    os.replace(out_tmp, OUT_TAR)
    print("    -> %s  (%.1f MB)" % (OUT_TAR, os.path.getsize(OUT_TAR) / 1024 ** 2))

    json.dump({"config_name": cfg_name, "config_digest": "sha256:" + cfg_digest,
               "deps_diff": deps_diff, "app_diff": app_diff,
               "layers": [n for n, _, _ in entries], "out": OUT_TAR},
              open(os.path.join(WORK, "build_result.json"), "w", encoding="utf-8"), indent=2)
    print("\nDONE")


if __name__ == "__main__":
    main()
