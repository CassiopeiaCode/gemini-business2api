# Stage 1: 构建前端
FROM node:20-slim AS frontend-builder
WORKDIR /app/frontend

# 先复制 package 文件利用 Docker 缓存
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --silent

# 为 gptmail-headless 预装 Node 侧代理依赖，供运行时直接复制
RUN mkdir -p /app/gptmail-headless-deps && \
    cd /app/gptmail-headless-deps && \
    npm init -y --silent && \
    npm install --silent undici

# 复制前端源码并构建
COPY frontend/ ./
RUN npm run build

# Stage 2: 最终运行时镜像
FROM python:3.11-slim
WORKDIR /app

# 从前端构建镜像复用 Node.js 运行时，供 gptmail-headless 桥接使用
COPY --from=frontend-builder /usr/local/bin/node /usr/local/bin/node
COPY --from=frontend-builder /usr/local/bin/npm /usr/local/bin/npm
COPY --from=frontend-builder /usr/local/bin/npx /usr/local/bin/npx
COPY --from=frontend-builder /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=frontend-builder /app/gptmail-headless-deps/node_modules /app/node_modules

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    CLOAKBROWSER_AUTO_UPDATE=false \
    CLOAKBROWSER_DOWNLOAD_URL=https://github.com/CloakHQ/cloakbrowser/releases/download

# 安装 Python 依赖和浏览器依赖（合并为单一 RUN 指令以减少层数）
COPY requirements.txt .
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        gcc \
        curl \
        tzdata \
        chromium chromium-driver \
        dbus dbus-x11 \
        xvfb xauth xdotool \
        procps psmisc \
        libglib2.0-0 libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
        libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
        libxfixes3 libxrandr2 libgbm1 libasound2 libpango-1.0-0 \
        libcairo2 fonts-liberation fonts-noto-cjk \
        libdbus-1-3 libatspi2.0-0 \
        libfontconfig1 libx11-xcb1 libx11-6 libxcb1 libxext6 libxshmfence1 \
        libgtk-3-0 libpangocairo-1.0-0 libcairo-gobject2 libgdk-pixbuf-2.0-0 \
        libxss1 libxtst6 && \
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone && \
    pip install --no-cache-dir -r requirements.txt && \
    python -c "from cloakbrowser.download import ensure_binary; ensure_binary()" && \
    apt-get purge -y gcc && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# 复制后端代码
COPY main.py .
COPY core ./core
COPY util ./util

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
