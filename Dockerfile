# syntax=docker/dockerfile:1
# ============================================================================
# 家庭保险整理工具 — 单阶段生产镜像
# ----------------------------------------------------------------------------
# 前后端同源、无构建产物、无外部 CDN：仅需「装依赖 → 拷代码 → 起 uvicorn」三步。
#
# 可覆盖的构建参数（build-arg）：
#   BASE_IMAGE    基础镜像，国内可换镜像站，例如
#                 --build-arg BASE_IMAGE=docker.m.daocloud.io/library/python:3.13-slim
#   PIP_INDEX_URL pip 源，国内可换清华/阿里，例如
#                 --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
#   TZ            时区，默认 Asia/Shanghai
# ============================================================================

ARG BASE_IMAGE=python:3.13-slim
FROM ${BASE_IMAGE}

# ---- 构建参数（默认值保证可移植性；国内用户可按需覆盖）----
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG TZ=Asia/Shanghai

# ---- 运行期环境变量 ----
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DB_PATH=/app/data/insurance.db \
    PORT=8000 \
    TZ=${TZ}

WORKDIR /app

# ---- 时区数据层 ----
# python:3.13-slim 不含 tzdata：若只设 ENV TZ，glibc/Python zoneinfo 找不到
# /usr/share/zoneinfo/Asia/Shanghai，会静默退化为 UTC，日志时间差 8 小时。
# 因此这里显式安装 tzdata 并落盘 /etc/localtime，确保 TZ 真正生效；
# 安装后清理 apt 缓存以控制镜像体积。
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && ln -snf /usr/share/zoneinfo/${TZ} /etc/localtime \
    && echo ${TZ} > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# ---- 依赖层（先拷贝 requirements 以利用构建缓存）----
COPY requirements.txt ./
RUN pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" -r requirements.txt

# ---- 应用代码与前端静态资源 ----
COPY app ./app
COPY static ./static

# ---- 数据目录（运行期数据由 volume 挂载持久化）----
# /app/data 同时存放 SQLite 数据库（insurance.db）与电子保单附件（attachments/），
# 因此 Docker 只需挂载 /app/data 一个卷即可持久化「数据库 + PDF 附件」；
# attachments/ 子目录由应用启动时自动创建（见 app/main.py 的 _ensure_attachments_dir）。
RUN mkdir -p /app/data

# ---- OCI 标准镜像元数据 ----
LABEL org.opencontainers.image.title="family-insurance-manager" \
      org.opencontainers.image.version="1.8.0" \
      org.opencontainers.image.description="家庭保险整理工具：本地优先的家庭保单登记、售后信息归档、电子保单 PDF 归集、年化保费计算与看板统计" \
      org.opencontainers.image.source="https://example.local/family-insurance-manager" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.authors="family-insurance-manager"

EXPOSE 8000

# ---- 健康检查：使用 Python 标准库（镜像内不含 curl/wget），尊重 $PORT ----
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,sys,urllib.request; port=os.environ.get('PORT','8000'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health', timeout=4).status == 200 else 1)"

# ---- 启动服务：shell 形式 + exec，保证 SIGTERM/SIGINT 正确传递给 uvicorn ----
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port \"${PORT:-8000}\""]
