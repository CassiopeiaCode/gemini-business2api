# Stage 1: 构建前端
FROM node:20-slim AS frontend-builder
WORKDIR /app/frontend

# 先复制 package 文件利用 Docker 缓存
COPY frontend/package.json frontend/package-lock.json ./
RUN npm install --silent

# 复制前端源码并构建
COPY frontend/ ./
RUN npm run build

# Stage 2: 最终运行时镜像
FROM python:3.11-slim
WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

# 安装 Python 依赖和浏览器依赖（合并为单一 RUN 指令以减少层数）
COPY requirements.txt .
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        gcc \
        curl \
        tzdata \
        chromium chromium-driver \
        dbus dbus-x11 \
        xvfb xauth \
        libglib2.0-0 libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
        libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
        libxfixes3 libxrandr2 libgbm1 libasound2 libpango-1.0-0 \
        libcairo2 fonts-liberation fonts-noto-cjk && \
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone && \
    pip install --no-cache-dir -r requirements.txt && \
    apt-get purge -y gcc && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# 复制后端代码
COPY main.py .
COPY core ./core
COPY util ./util

# 下载并解压 fingerprint-chromium 浏览器
RUN apt-get update && \
    apt-get install -y --no-install-recommends xz-utils && \
    curl -L -o /tmp/ungoogled-chromium.tar.xz \
        https://github.com/adryfish/fingerprint-chromium/releases/download/142.0.7444.175/ungoogled-chromium-142.0.7444.175-1-x86_64_linux.tar.xz && \
    cd /app && \
    tar -xf /tmp/ungoogled-chromium.tar.xz && \
    rm /tmp/ungoogled-chromium.tar.xz && \
    chmod +x /app/ungoogled-chromium-142.0.7444.175-1-x86_64_linux/chrome && \
    apt-get purge -y xz-utils && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

# 从 builder 阶段只复制构建好的静态文件
COPY --from=frontend-builder /app/static ./static

# 创建数据目录
RUN mkdir -p ./data

# 复制启动脚本
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

# 声明数据卷
VOLUME ["/app/data"]

# 声明端口
EXPOSE 7860

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:7860/admin/health || exit 1

# 启动服务
CMD ["./entrypoint.sh"]
