"""Step 5: 补充审计——针对「静态结构检查覆盖不到、但真机导入/启动会立刻炸」的风险。

前面 step4 查的是「结构对不对」，本步查的是「内容是不是真的能用」：

  F. ELF 架构审计：镜像里每个 ELF 文件（python 解释器 + 全部 *.so）必须是 x86-64。
     这是本方案最容易被忽视的致命点——wheel 是手工按平台挑的，一旦混入 aarch64
     的 .so，容器启动时才会以 `Exec format error` / `cannot open shared object` 炸掉。
  G. 应用层字节级完整性：镜像里每个 app 文件必须与仓库源文件 sha256 完全一致。
     防止 tar 组装过程出现静默截断 / 编码损坏。
  H. 叠加冲突审计：检测依赖层是否覆盖了基础层的文件、层内是否有重复路径。
     pip --target 在包之间有文件重名时是「后写覆盖」，必须显式确认没有踩到基础层。
"""
import hashlib
import io
import json
import os
import posixpath
import re
import struct
import sys
import tarfile
import tempfile

SO_RE = re.compile(r"\.so(\.\d+)*$")

WORK = os.environ.get("IMGBUILD_WORK") or os.path.join(tempfile.gettempdir(), "_imgbuild")
REPO = os.environ.get("IMGBUILD_REPO") or os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)
OUT_TAR = os.path.join(REPO, "dist", "family-insurance-manager-1.8.0-amd64.tar")
SP = "usr/local/lib/python3.13/site-packages"

PASS, FAIL, WARN = [], [], []


def chk(cond, label, detail=""):
    (PASS if cond else FAIL).append(label)
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", label,
                           ("  <- " + detail) if detail and not cond else ""))


def warn(label):
    WARN.append(label)
    print("  [WARN] %s" % label)


def elf_machine(blob):
    """返回 (e_machine, class_bits)；非 ELF 返回 (None, None)。"""
    if len(blob) < 20 or blob[:4] != b"\x7fELF":
        return None, None
    ei_class = blob[4]          # 1=32bit 2=64bit
    ei_data = blob[5]           # 1=LE 2=BE
    endian = "<" if ei_data == 1 else ">"
    e_machine = struct.unpack(endian + "H", blob[18:20])[0]
    return e_machine, ei_class * 32


def elf_bits_str(machine, bits):
    names = {62: "x86-64", 183: "aarch64", 3: "i386", 40: "arm"}
    return "%s/%sbit" % (names.get(machine, "machine=%s" % machine), bits)


def main():
    tf = tarfile.open(OUT_TAR, "r")
    m = json.loads(tf.extractfile("manifest.json").read())[0]
    cfg = json.loads(tf.extractfile(m["Config"]).read())

    # 载入所有层
    layers_raw, layer_names = [], []
    for lp in m["Layers"]:
        raw = tf.extractfile(lp).read()
        layers_raw.append(raw)
        layer_names.append(lp.split("/")[0])

    # ---------------------------------------------------------------- F
    print("=" * 74)
    print("F. ELF 架构审计（每个 ELF 必须是 x86-64）")
    print("=" * 74)
    elf_files, bad_elf, non_elf_with_elf_ext = [], [], []
    checked_bytes = 0
    for i, data in enumerate(layers_raw):
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as lt:
            for mem in lt.getmembers():
                if not mem.isfile() or mem.size == 0:
                    continue
                nm = mem.name.rstrip("/")
                head = lt.extractfile(mem).read(20)
                if head[:4] != b"\x7fELF":
                    # 名字像动态库却不是 ELF → 值得警惕
                    # 注意：etc/ld.so.cache、etc/ld.so.conf、*-gdb.py 都不是动态库，
                    # 必须用严格后缀正则 *.so / *.so.N 来判定，否则全是误报。
                    bn = posixpath.basename(nm)
                    if SO_RE.search(bn) and not nm.startswith("etc/"):
                        non_elf_with_elf_ext.append((nm, i))
                    continue
                full = lt.extractfile(mem).read()
                checked_bytes += len(full)
                machine, bits = elf_machine(full)
                elf_files.append((nm, i, machine, bits))
                if machine != 62 or bits != 64:
                    bad_elf.append((nm, i, elf_bits_str(machine, bits)))

    print("  扫描到 ELF 文件 %d 个（合计 %.1f MB）" % (len(elf_files), checked_bytes / 1024 ** 2))
    by_layer = {}
    for nm, i, mc, bt in elf_files:
        by_layer.setdefault(i, []).append((nm, mc, bt))
    for i in sorted(by_layer):
        print("    层 %d (%s): %d 个 ELF" % (i, layer_names[i][:16], len(by_layer[i])))

    # python 解释器本身
    py_elf = [x for x in elf_files if x[0] == "usr/local/bin/python3.13"]
    chk(len(py_elf) == 1, "找到 usr/local/bin/python3.13 且是 ELF 可执行文件",
        "实际 %d 个" % len(py_elf))
    if py_elf:
        print("    python3.13 -> %s" % elf_bits_str(py_elf[0][2], py_elf[0][3]))
        chk(py_elf[0][2] == 62 and py_elf[0][3] == 64,
            "python3.13 是 x86-64", elf_bits_str(py_elf[0][2], py_elf[0][3]))

    chk(not bad_elf, "全部 %d 个 ELF 均为 x86-64（无 aarch64/arm/i386 混入）" % len(elf_files),
        "非 x86-64: %s" % bad_elf[:5])
    chk(not non_elf_with_elf_ext, "没有「名为 .so 但非 ELF」的可疑文件",
        str(non_elf_with_elf_ext[:5]))

    # 依赖层里必须有的关键 C 扩展
    print("  -- 关键 C 扩展抽查 --")
    want_sos = {
        "pydantic_core": "pydantic_core/_pydantic_core",
        "sqlalchemy": "sqlalchemy/cyextension/collections",
        "greenlet": "greenlet/_greenlet",
        "httptools": "httptools/parser/parser",
        "websockets": "websockets/speedups",
        "watchfiles": "watchfiles/_rust_notify",
        "yaml": "yaml/_yaml",
    }
    # 精确定位依赖层：以 usr/local/bin/uvicorn 为准（不能用「ELF 最多」猜，
    # 那会猜中基础层——基础层 655 个 ELF，依赖层只有 14 个）
    deps_layer = None
    for i, data in enumerate(layers_raw):
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as lt:
            if "usr/local/bin/uvicorn" in lt.getnames():
                deps_layer = i
                break
    chk(deps_layer is not None, "定位到依赖层（含 usr/local/bin/uvicorn）")
    print("  依赖层 = 层 %s" % deps_layer)
    for pkg, stem in want_sos.items():
        hits = [n for n, j, _, _ in elf_files
                if j == deps_layer and posixpath.basename(n).startswith(posixpath.basename(stem))]
        chk(bool(hits), "%s 的 C 扩展存在且为 ELF" % pkg,
            "层 %s 中未找到 %s*" % (deps_layer, stem))

    # ---------------------------------------------------------------- G
    print()
    print("=" * 74)
    print("G. 应用层字节级完整性（镜像文件 sha256 == 仓库源文件）")
    print("=" * 74)
    # 找到应用层：包含 app/app/main.py 的层
    app_layer = None
    for i, data in enumerate(layers_raw):
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as lt:
            if "app/app/main.py" in lt.getnames():
                app_layer = i
                break
    chk(app_layer is not None, "定位到应用层")
    print("  应用层 = 层 %d (%s)" % (app_layer, layer_names[app_layer][:16]))

    # 仓库侧期望文件
    expect = {}
    for sub, prefix in (("app", "app/app"), ("static", "app/static")):
        root = os.path.join(REPO, sub)
        for dp, dns, fns in os.walk(root):
            dns[:] = [d for d in dns if d != "__pycache__"]
            for fn in fns:
                if fn.endswith(".pyc"):
                    continue
                full = os.path.join(dp, fn)
                rel = os.path.relpath(full, root).replace("\\", "/")
                expect["%s/%s" % (prefix, rel)] = full
    # requirements.txt 也 COPY 进去了
    expect["app/requirements.txt"] = os.path.join(REPO, "requirements.txt")
    print("  仓库侧应有 %d 个文件" % len(expect))

    got = {}
    with tarfile.open(fileobj=io.BytesIO(layers_raw[app_layer]), mode="r:") as lt:
        for mem in lt.getmembers():
            if mem.isfile():
                got[mem.name.rstrip("/")] = hashlib.sha256(lt.extractfile(mem).read()).hexdigest()

    miss = sorted(set(expect) - set(got))
    extra = sorted(set(got) - set(expect))
    diff = []
    for arc, full in sorted(expect.items()):
        if arc not in got:
            continue
        disk = hashlib.sha256(open(full, "rb").read()).hexdigest()
        if disk != got[arc]:
            diff.append(arc)
    chk(not miss, "镜像里没有缺文件", "缺失: %s" % miss[:8])
    chk(not diff, "全部应用文件与仓库源文件逐字节一致（sha256 相同）",
        "不一致: %s" % diff[:8])
    if extra:
        warn("镜像应用层多出 %d 个文件（非错误，仅提示）: %s" % (len(extra), extra[:8]))
    else:
        print("  [PASS] 镜像应用层无多余文件")

    # 反向抽查：镜像里的 main.py 前 5 行可读且是合法 UTF-8
    with tarfile.open(fileobj=io.BytesIO(layers_raw[app_layer]), mode="r:") as lt:
        body = lt.extractfile("app/app/main.py").read()
    try:
        txt = body.decode("utf-8")
        ok = "FastAPI" in txt or "app" in txt
        chk(ok, "镜像内 main.py 可正常 UTF-8 解码且内容合理",
            txt[:80])
        print("    main.py 首行: %s" % txt.splitlines()[0][:70])
    except UnicodeDecodeError as e:
        chk(False, "镜像内 main.py UTF-8 解码", str(e))

    # ---------------------------------------------------------------- H
    print()
    print("=" * 74)
    print("H. 层叠加冲突审计")
    print("=" * 74)
    seen = {}          # path -> 首个出现的层号
    collisions = []    # 后层覆盖前层
    intra_dupes = []   # 同层内重复路径
    for i, data in enumerate(layers_raw):
        within = {}
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as lt:
            for mem in lt.getmembers():
                nm = mem.name.rstrip("/")
                if not nm or mem.isdir():
                    continue
                if nm in within:
                    intra_dupes.append((nm, i))
                within[nm] = True
                if nm in seen and seen[nm] != i:
                    collisions.append((nm, seen[nm], i))
                seen[nm] = i
    print("  跨层覆盖 %d 处，层内重复 %d 处" % (len(collisions), len(intra_dupes)))

    # 只有「依赖层覆盖基础层」才是真问题；etc/localtime 是刻意覆盖，单独放行
    INTENTIONAL = {"etc/localtime"}
    base_dep_collisions = [(n, a, b) for n, a, b in collisions
                           if a < deps_layer and b == deps_layer and n not in INTENTIONAL]
    n_base = sum(1 for n, i in seen.items() if i < deps_layer)
    chk(not base_dep_collisions,
        "依赖层未意外覆盖任何基础层文件（基础层共 %d 个文件）" % n_base,
        "被覆盖: %s" % base_dep_collisions[:8])
    if collisions:
        for n, a, b in collisions[:10]:
            print("    覆盖: %s  层%d -> 层%d" % (n, a, b))
    chk(not intra_dupes, "层内无重复路径（tar 结构干净）", str(intra_dupes[:5]))

    # 反向确认：那唯一一处刻意覆盖「确实发生了」，且方向正确
    chk(any(n == "etc/localtime" and a < deps_layer and b == deps_layer
            for n, a, b in collisions),
        "etc/localtime 被依赖层刻意覆盖（Etc/UTC -> Asia/Shanghai）")
    base_lt = None
    for i, data in enumerate(layers_raw[:deps_layer]):
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as lt:
            if "etc/localtime" in lt.getnames():
                base_lt = lt.getmember("etc/localtime").linkname
    print("    基础层原 etc/localtime -> %r" % base_lt)
    chk(base_lt != "/usr/share/zoneinfo/Asia/Shanghai",
        "基础层原时区确非 Asia/Shanghai（说明覆盖是必要且有意义的）", repr(base_lt))

    # /etc 下不应有依赖层写的东西（除了我们刻意写的 localtime/timezone）
    etc_dep = sorted(n for n, i in seen.items() if i == deps_layer and n.startswith("etc/"))
    print("  依赖层写入的 /etc 条目: %s" % (", ".join(etc_dep) if etc_dep else "无"))
    chk(set(etc_dep) <= {"etc/localtime", "etc/timezone"},
        "依赖层只写入了刻意设置的时区条目", str(etc_dep))

    # ---------------------------------------------------------------- 汇总
    print()
    print("=" * 74)
    print("结果：PASS %d   FAIL %d   WARN %d" % (len(PASS), len(FAIL), len(WARN)))
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  -", f)
    if WARN:
        print("提示项：")
        for w in WARN:
            print("  -", w)
    print("=" * 74)
    json.dump({"pass": len(PASS), "fail": len(FAIL), "warn": len(WARN),
               "failures": FAIL, "warnings": WARN},
              open(os.path.join(WORK, "audit_result.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
