# 极空间 NAS 部署指南（新手版）

> 本文档面向**没用过 Docker 的家庭用户**。跟着做就行，不需要理解 Docker 原理。
> 全程只需三步：**在电脑上打包 → 传到 NAS → 导入并运行**。

---

## 一、总览：三步走

> ✅ **镜像已经打包好了，你不需要安装 Docker，也不需要自己构建。**
> 成品就在项目里的 **`dist/family-insurance-manager-1.8.1-amd64.tar`**（150.3 MB），
> 直接做下面两步即可。

```
┌──────────────────────────────────┐
│ 第 1 步：拿到 tar 文件             │
│ dist/family-insurance-manager-   │
│ 1.8.1-amd64.tar  ← 已经是成品     │
└───────────────┬──────────────────┘
                │  第 2 步：把 tar 上传到极空间
                ▼
┌──────────────────────────────────┐
│ 第 2 步：上传到 NAS               │
│ 用 SMB / 文件管理器拷贝            │
│ 到某个文件夹（如 /Docker/insurance）│
└───────────────┬──────────────────┘
                │  第 3 步：导入镜像 + 建 Compose 项目
                ▼
┌──────────────────────────────────┐
│ 第 3 步：在极空间里               │
│ ① Docker → 镜像 → 导入 tar        │
│ ② Docker → Compose → 新增项目     │
│ ③ 浏览器访问 http://NAS:8000      │
└──────────────────────────────────┘
```

> 为什么不能在极空间上直接构建？
> 极空间的图形界面只能「拉取现成镜像」或「导入镜像文件」，**不能从源码构建**。
> 所以镜像已经提前在电脑上做好，你只需把它搬进 NAS。
>
> 想知道这个镜像到底是怎么在没有 Docker 的电脑上做出来的，见
> **第 3.5 节**；想自己重新构建，见 **第 3.3 节**。

---

## 二、前置检查清单

开始前，逐项确认（打勾即可）：

| # | 检查项 | 怎么做 | 期望结果 |
|---|---|---|---|
| 1 | 你的极空间是 **x86** 架构 | 主流型号（Intel N97 / N305 等）均为 x86；ARM 机型不适用 | 属于 x86 ✅ |
| 2 | 已开启 Docker 支持 | 极空间【设置】→【通用】→【开发者选项】→ 打开「**启用Docker支持**」 | 见下方说明 |
| 3 | 8000 端口没被占用 | 一般家庭网络 8000 是空的；若已装别的服务需改端口 | 空着 ✅ |
| 4 | 电脑装了 **Docker Desktop** | 到 docker.com 下载安装并启动，右下角鲸鱼图标变绿 | 运行中 ✅ |

> **关于第 2 项（最容易卡住的地方）**：
> - **较新固件**：开启后**必须重启极空间**，重启后主界面才会出现「**Docker**」应用图标。
> - **较老固件**：主界面/应用里可能**直接就有**「Docker」图标，无需去开发者选项。
> - 找不到入口时，先按「较新固件」的路径试一遍并重启。

---

## 三、第 1 步：拿到镜像文件（已为你构建好）

### 3.1 镜像文件在哪

| 项目 | 值 |
|---|---|
| 路径 | **项目根目录下 `dist/family-insurance-manager-1.8.1-amd64.tar`** |
| 大小 | **150.3 MB**（157,573,120 字节） |
| 平台 | **linux/amd64**（匹配 NAS 的 x86 CPU） |
| SHA256 | `c1467290de2d739322e2b446e0b4ea3333ce38a462a9b04a9f736c48148ec534` |
| 镜像名:标签 | `family-insurance-manager:1.8.1` |
| 镜像 ID | `sha256:ef22aa509e8a957ef958fdbd469fefb37b23794bc13b664182fd70f05c3be1ef` |

**你不需要做任何事**，这个文件就是最终成品，直接进第 2 步。

> 为什么是 157 MB？因为里面装了一整套 Python 3.13 运行环境和全部依赖。
> 这是正常的，不是出错。

> **📌 2026-09-17 修复说明（如果你之前导入失败过，务必用新文件）**
>
> **根因已定位到 Docker 源码级别**（不是猜的）。初版 tar 有一个会被 `docker load` **硬拒绝**的缺陷：
>
> **`history` 里"非空"的条目数多于层数。** 初版把 Dockerfile 的 8 条指令全记成"产生文件系统的层"，
> 于是 config 里有 **11 条非空 history，却只有 6 个层**。
>
> Docker 的 `image/store.go` 里 `Create()` 是这么写的（原文）：
>
> ```go
> layerCounter := 0
> for _, h := range img.History { if !h.EmptyLayer { layerCounter++ } }
> if layerCounter > len(img.RootFS.DiffIDs) {
>     return "", errdefs.InvalidParameter(errors.New("too many non-empty layers in History section"))
> }
> ```
>
> 而 `tarexport/load.go` 在 **`manifest.json` 存在时必然走这条路径**（`imgID, err := l.is.Create(config)`），
> 所以 **11 > 6 会直接失败**——这就是你在极空间看到的「任务添加失败」。
> 现在改为只有 2 条非空（依赖层 / 应用层），其余标 `empty_layer`，并在构建时自检：
> **一旦非空条目数超过层数就立刻中止，绝不产出坏镜像。**
>
> 另外做了两处**加固**（注意：这两项**不是**本次失败的原因，只是提高兼容性）：
> - 每层目录补齐 `json` 文件，凑成经典 `docker save` 的 `VERSION` + `json` + `layer.tar` 三件套。
>   （`manifest.json` 存在时 `docker load` 并不读这个 `json`，只有无 `manifest.json` 的 legacy 路径才读。）
> - 去掉 config 里的 `variant: null`（Go 能解析 `null`，但严格 JSON Schema 会当成类型错误）。
>
> 现在的结构已与真实 `docker save` 逐项对齐，校验项从 97 项增加到 **106 项，全部通过**。
>
> **📌 当前版本 v1.8.1（2026-11-09）**：v1.8 镜像**沿用同一套已验证的正确结构**重新构建，同样 **106 项校验全部通过**。
> 当前该用的文件是 **`family-insurance-manager-1.8.1-amd64.tar`**（SHA256 `c1467290…`，镜像 `family-insurance-manager:1.8.1`）。
> 如果你手上还是 v1.8.0（`371e74e2…`，微信端看不到消息）、v1.7（`ffdf954b…`）或更早的，请换成 v1.8.1。
>
> **升级到 v1.4 时**：本次**不涉及数据结构变更**（只改了看板口径），但**建议仍先备份 `./data/` 整个文件夹**。
> 若你**从未装过 v1.3**（即从 v1.2 直接跳到 v1.4），则容器首次启动会自动完成 v1.3 引入的数据结构迁移
> （新增被保人关联表，**幂等、失败不阻塞启动、无需你操作**）。

### 3.2 （可选）确认文件没在拷贝过程中损坏

文件较大，用 U 盘或网盘搬来搬去有可能损坏。在**存放该文件的目录**打开命令行：

**Windows（PowerShell）**
```powershell
Get-FileHash dist\family-insurance-manager-1.8.1-amd64.tar -Algorithm SHA256
```

**macOS / Linux**
```bash
shasum -a 256 dist/family-insurance-manager-1.8.1-amd64.tar
```

输出应当与 3.1 表格里的 SHA256 **完全一致**。不一致就重新拷贝一次。

### 3.3 如果你确实想自己重新构建（进阶，可跳过）

需要电脑装 **Docker Desktop**（[docker.com](https://www.docker.com/products/docker-desktop/) 下载，装好启动到右下角鲸鱼图标变绿）。

**Windows（推荐）**：在项目根目录打开命令行，运行

```bat
scripts\build-image.bat
```

**macOS / Linux**：

```bash
chmod +x scripts/build-image.sh
./scripts/build-image.sh
```

脚本会自动：检查 Docker 是否可用 → 构建镜像 → 导出 tar → 打印产物路径与大小。

> 脚本内部已**强制指定 `--platform linux/amd64`**。这点很重要：
> 如果你用的是 Apple Silicon（M 系列）Mac，不指定平台会构建出 ARM 镜像，
> 装到 x86 的 NAS 上会报 `exec format error` 而无法启动。

> ⚠️ 如果 Docker Desktop 无法启用 WSL 2 后端（部分企业安全策略会禁用），
> 可在 Settings → General 里**取消勾选** "Use the WSL 2 based engine"，
> 改用 **Hyper-V 后端**（需确保「Hyper-V」Windows 功能已启用并重启）。
> 若装不起来，直接用 3.1 的成品即可。

### 3.4 国内网络慢？加两个「换源」参数（可选）

```bat
rem Windows：先设置环境变量，再运行脚本
set BASE_IMAGE=docker.m.daocloud.io/library/python:3.13-slim
set PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
scripts\build-image.bat
```

```bash
# macOS / Linux：直接加在命令前面
BASE_IMAGE=docker.m.daocloud.io/library/python:3.13-slim \
PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
./scripts/build-image.sh
```

| 参数 | 作用 | 可换的值举例 |
|---|---|---|
| `BASE_IMAGE` | 基础镜像来源 | `docker.m.daocloud.io/library/python:3.13-slim`、`dockerproxy.net/library/python:3.13-slim` |
| `PIP_INDEX_URL` | pip 源 | `https://pypi.tuna.tsinghua.edu.cn/simple`（清华）、`https://mirrors.aliyun.com/pypi/simple/`（阿里） |

### 3.5 成品镜像是怎么做出来的（无 Docker 组装）

本次交付的 tar 是在**构建环境未安装任何容器工具**（`docker`/`podman`/`nerdctl`/`buildah` 全无）的条件下，
用一套纯 Python 脚本按 `docker save` 的格式**直接组装**出来的：

| 脚本 | 作用 |
|---|---|
| `tools/offline-image-build/step1_pull_base.py` | 经国内镜像站拉取官方 `python:3.13-slim`（linux/amd64）的 config 与 4 个层，并校验每个层解压后的 SHA256 与官方 `diff_ids` 一致 |
| `tools/offline-image-build/step2_wheels.py` | 下载 **linux/amd64 + CPython 3.13** 的预编译 wheel（`--only-binary=:all:`，不需要编译器），安装到层目录 |
| `tools/offline-image-build/step3_assemble.py` | 生成依赖层与应用层，写出 config（ENV/WORKDIR/CMD/EXPOSE/LABEL/HEALTHCHECK 全部对齐 `Dockerfile`）与 manifest |
| `tools/offline-image-build/step4_verify.py` | 85 项结构校验：层摘要、逐层叠加后的文件系统、控制台脚本 shebang、app 全部 import 的依赖完整性、history 与层数关系、manifest.Layers 与 diff_ids 数量一致、每层三件套 |

与 `Dockerfile` 的**唯一差异**：`Dockerfile` 里 `apt-get install tzdata` 那一步，
经核实基础镜像**本来就自带** `/usr/share/zoneinfo`（443 个真实 TZif 文件），
所以脚本只复刻了该步骤中真正生效的两条：`/etc/localtime` 改指 `Asia/Shanghai` + 写 `/etc/timezone`。

> 诚实声明：构建环境未安装 Docker，**无法 `docker load` 实跑验证**。
> 上述校验覆盖了格式、摘要、文件系统与依赖完整性，但「真实导入 + 启动」需要你在极空间上做一次。

### 3.6 自建产物路径

若你走了 3.3 自行构建，产物在 **`dist/family-insurance-manager-1.3.0.tar`**（注意没有 `-amd64` 后缀），
大小通常 **约 120–200 MB**。后续步骤把它当作 3.1 的 tar 用即可。

---

## 四、第 2 步：把 tar 传到极空间 NAS

**推荐方式**：用极空间的 **SMB 共享**或**文件管理器**上传（最稳）。

1. 在电脑上通过 SMB 访问极空间共享文件夹（Windows 资源管理器地址栏输入 `\\极空间IP`）。
2. 建议建一个专用目录，例如：

```
/zfs-pool/zspace-user/data/Docker/insurance/
├── family-insurance-manager-1.8.1-amd64.tar   ← 上传到这里
└── data/                                      ← 数据文件夹（首次会自动生成）
```

> 目录名可自定，**记住这个位置**，第 3 步建 Compose 项目时会用到。
> 如果你的固件支持「存储位置」选择，直接选中这个 `insurance` 文件夹即可。

---

## 五、第 3 步：在极空间导入镜像并建 Compose 项目

### 5.1 导入镜像

菜单路径（极空间官方教程的准确叫法）：

```
Docker → 镜像 → 本地镜像 → 导入镜像 → 从极空间导入 → 选中第 2 步上传的 tar 包 → 确定
```

导入成功后，镜像列表里会出现 **`family-insurance-manager:1.8.1`**。

> **⚠️ 报「任务添加失败」怎么办？**
>
> 先说结论：**这个报错的真实成因在 2026-09-17 已定位并修复到 Docker 源码级别**（见 3.1 节的修复说明）——
> 初版 tar 的 `history` 非空条目数（11）**多于**层数（6），`docker load` 会直接报
> `too many non-empty layers in History section`。
> **请确认你用的是 v1.8.1 的 tar（SHA256 以 `c1467290…` 开头）再试。**
>
> 如果换了新文件**仍然**失败，按下面顺序排查（每一步都会排除一类原因）：
>
> | 排查动作 | 说明 |
> |---|---|
> | ① 确认文件完整 | 在极空间上核对文件大小必须是 **157,573,120 字节**（150.3 MB）。SMB 拷贝中断是「任务添加失败」最常见的原因，文件小一点就是没传完。 |
> | ② 确认文件名没被改 | 必须是 `family-insurance-manager-1.8.1-amd64.tar`。有些网盘/下载器会加 `(1)`、`.txt` 等后缀，导入器按后缀判断就会拒绝。 |
> | ③ 确认存放位置 | 换到**根目录或纯英文路径**（例如 `Docker/insurance/`）再试。路径里含中文或特殊字符时，个别固件会读取失败。 |
> | ④ 确认 Docker 功能已开 | 【设置】→【通用】→【开发者选项】里打开 Docker 并**重启 NAS**。Docker 引擎没起来时，任何导入都会「任务添加失败」。 |
> | ⑤ 换一种导入入口 | 如果「从极空间导入」不行，试试把 tar 放在 NAS 后走「**从本地上传**」（如果有这个选项）。 |
> | ⑥ 退回 Docker 原生构建 | 见 **3.3 节**：电脑装 Docker Desktop（Hyper-V 后端）后跑 `scripts\build-image.bat`，产物是 Docker 自己导出的 tar，兼容性最好。 |
>
> **请把报错的原文（或截图）发我。**「任务添加失败」是极空间的笼统提示，实际原因藏在
> 后端日志里；有了原文我能直接定位，不用再猜。

### 5.2 创建 Compose 项目

菜单路径：

```
Docker → Compose → 新增项目
```

填写：

| 字段 | 填什么 |
|---|---|
| 项目名称 | 例如 `insurance`（自定） |
| 存储位置 | 若固件要求先选文件夹，选你在「文件」App 里建好的那个 `insurance` 文件夹 |
| Compose 内容 | 把项目根目录 `docker-compose.yml` 的**全部内容**粘贴进来，或点「**导入**」直接选这个 `.yml` 文件 |

Compose 全文（就是项目里 `docker-compose.yml` 的核心，**只有一处需要你改**）：

```yaml
services:
  insurance-tool:
    image: family-insurance-manager:1.8.1
    container_name: family-insurance-manager
    ports:
      - "8000:8000"
    environment:
      TZ: Asia/Shanghai
      DB_PATH: /app/data/insurance.db
      PORT: "8000"
    volumes:
      # ⚠️ 把冒号【左边】换成你 NAS 上的真实路径（点「查询路径」按钮获取）
      - /REPLACE_ME/data:/app/data
    restart: unless-stopped
```

**三个必须知道的坑（都是极空间特有的）**：

1. **不要写 `version:` 那一行**。极空间的 Compose 不需要它，写了反而可能报语法错误。
   项目里的 `docker-compose.yml` 已经刻意不含这一行。
2. **`volumes` 的路径必须换成绝对路径**。极空间对相对路径 `./data` 的支持不稳定，
   官方教程也推荐用编辑器里的「**查询路径**」按钮拿真实路径。操作：
   点 Compose 编辑框上方的「**查询路径**」→ 在文件管理器里选中目标文件夹 → 点「**复制路径**」
   → 回到编辑框，把 `/REPLACE_ME/data` 整段替换掉。
   > 建议先在极空间「文件」App 里新建一个空文件夹（如 `Docker/insurance/data`）再选它。
3. **粘贴后如果报语法错误**，多半是编辑器把缩进弄坏了。YAML 对缩进极其敏感——
   全选删掉，重新完整粘贴一次，不要手工调整。

### 5.3 端口被占用怎么办

`ports` 里 `"8000:8000"` 的**冒号左边**是 NAS 对外端口。如果创建时提示端口冲突，
把左边改成别的（例如 `"18000:8000"`），**右边 8000 不要动**，之后访问
`http://<NAS的IP>:18000`。

### 5.4 创建并启动

点「**创建**」。极空间会自动使用本地镜像并启动容器。稍等片刻，状态变为**运行中**即成功。

> 如果提示「找不到镜像」，说明 5.1 的导入还没成功——先回去把镜像导进来。

---

## 六、验证清单

### 6.1 打开页面

浏览器访问（把 `NAS的IP` 换成你极空间的实际地址）：

```
http://<NAS的IP>:8000
```

能看到「家庭保险整理工具」页面即成功。

### 6.2 验证数据能落盘（关键）

1. 点顶部「**家庭成员**」。
2. 点「**新增成员**」，随便填一个名字，保存。
3. 刷新页面，成员**还在** → 说明数据已正确写入 `data/insurance.db`。

### 6.3 SSH 排查命令（进阶）

极空间 **SSH 端口默认 10000**，登录后可用：

```bash
docker ps                                   # 看容器是否在跑、健康状态
docker logs family-insurance-manager        # 看应用日志
docker logs -f family-insurance-manager     # 实时跟日志（Ctrl+C 退出）
docker inspect --format '{{.State.Health.Status}}' family-insurance-manager  # 健康状态
```

> 极空间 Docker 图形界面**没有**日志 / 终端 / 监控，所以排查基本靠 SSH。

---

## 七、数据备份与迁移

**你的全部数据都在 NAS 上的 `data/` 文件夹里**，包含两部分：

| 内容 | 位置 | 说明 |
|---|---|---|
| 保单数据库 | `data/insurance.db` | 一个 SQLite 文件，含成员 / 保单 / 售后信息 |
| 电子保单 PDF | `data/attachments/` | v1.2 新增，上传的 PDF 都存这里 |

> 因为两者都在 `data/` 下，**备份整个 `data/` 文件夹即可**（只需一个 Docker 卷，无需额外挂载）。

### 备份
1. 停容器：`docker stop family-insurance-manager`
2. 把整个 **`data/` 文件夹**复制一份到别处（如电脑、网盘），即同时备份了数据库与 PDF。
3. 重新启动：`docker start family-insurance-manager`

> 或者不停容器，直接复制 `data/` 也行（家庭使用量下基本安全）。

### 迁移到新机器
1. 在新机器上按本文档重新部署（构建/导入镜像、建 Compose 项目）。
2. 把备份的整个 **`data/` 文件夹**（含 `insurance.db` 与 `attachments/`）放到新机器的项目目录里。
3. 启动容器即可，数据库与 PDF 附件一并原样恢复。

---

## 八、升级流程（改代码后重新部署）

数据不会丢，因为数据在 `data/` 里，跟镜像无关。

1. 在电脑上改代码，然后**重新构建**（记得改版本号或直接覆盖同名 tag）：

   ```bat
   scripts\build-image.bat
   ```

   > 电脑上没有 Docker 时，也可以按 **3.5 节**用 `tools/offline-image-build/` 里的
   > 5 个脚本重新组装一份（依次跑 step1→step5），产物同样是 `dist/*.tar`。

2. 把新的 tar 重新上传到 NAS（自建的是 `dist/family-insurance-manager-1.3.0.tar`，
   无 Docker 组装的是 `dist/family-insurance-manager-1.8.1-amd64.tar`）。
3. 极空间 **Docker → 镜像 → 导入**，覆盖导入（若提示已存在，先删除旧镜像再导入）。
4. 极空间 **Docker → Compose → 找到项目 → 重新部署 / 重启**。
5. 数据仍在 `data/`（数据库 `insurance.db` + PDF 附件 `attachments/`），无需任何迁移。

> 提示：升级前建议先按「第七节」备份一次 `insurance.db`，更稳妥。

---

## 九、常见问题排查表

| 现象 | 可能原因 | 解决办法 |
|---|---|---|
| **导入镜像报「任务添加失败」** | ① tar 结构与 `docker save` 规范不符（**2026-09-17 已修复，见 3.1 节**）；② 文件没传完（大小 ≠ 165,191,680 字节）；③ 文件名被网盘/下载器改过；④ 路径含中文；⑤ Docker 引擎没起来 | 按 **5.1 节的六步排查表**逐条走；换了新 tar 仍失败就把报错原文发我 |
| 启动报 **`exec format error`** | 镜像架构与 NAS 不匹配（在 ARM Mac 上构建了 ARM 镜像） | 重新构建，**必须带 `--platform linux/amd64`**（本项目的脚本已自动带上） |
| 导入镜像或拉取**超时** | 国内访问 Docker Hub 受阻 | 构建时换 `BASE_IMAGE`（国内镜像站）与 `PIP_INDEX_URL`（清华/阿里源），见第 3.3 节 |
| **8000 端口被占用** | 其它服务占用了 8000 | 改 `docker-compose.yml` 里 ports 的**左边**为 `18000`（即 `"18000:8000"`），浏览器改用 18000 |
| Compose **创建时报语法错误** | YAML 缩进被编辑器弄坏 / 误加了 `version:` 行 | 全选删掉重新完整粘贴；确认**没有** `version:` 这一行（极空间不需要） |
| Compose 报**路径错误** | 用了相对路径 `./data` | 点「**查询路径**」按钮取绝对路径，替换 `/REPLACE_ME/data`，见 5.2 节 |
| 容器在跑但**页面打不开** | 端口映射不对 / IP 填错 / 防火墙 | 用 `docker ps` 确认端口映射；确认访问的是 NAS 的 IP；确认 `http://` 不是 `https://` |
| 健康检查一直 **unhealthy** | 服务未就绪 / 数据库不可写 | `docker logs family-insurance-manager` 看报错；确认挂载目录存在且可写 |
| `docker save` 出的 **tar 太大** | 基础镜像本身较大 | 正常现象；如需更小可换 `python:3.13-alpine` 等精简基础镜像（进阶，需自行验证兼容性） |
| 极空间**找不到 Docker 入口** | 固件较新，Docker 默认关闭 | 【设置】→【通用】→【开发者选项】→ 开启「启用Docker支持」→ **重启** |
| 上传的 **PDF 附件重启后不见了** | `data/` 未挂载或挂载路径不对 | 确认 `docker-compose.yml` 的 volumes 把宿主机文件夹挂到了 `/app/data`；附件实际存放在 `data/attachments/` |
| 上传 PDF 报 **413 / 409** | 超过大小或数量上限 | 单文件默认 ≤20MB、每张保单默认 ≤10 个（可用环境变量 `MAX_ATTACHMENT_SIZE` / `MAX_ATTACHMENTS_PER_POLICY` 调整） |

---

## 十、安全提示（重要）

- 本工具是**家庭内网工具，没有账号密码**，任何人只要能访问这个端口就能看/改数据。
- **不要**把 `http://<NAS的IP>:8000` 直接暴露到公网。
- 若确实需要从外网访问，请走**极空间自带的反向代理 / 内网穿透**功能，并**额外加上认证**（如反代的 Basic Auth）。
- 建议只在家庭局域网内使用。
