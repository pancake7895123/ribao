# 使用腾讯云文档推荐的 alpine 基础镜像
FROM alpine

# 安装 Python 和 pip，并设置时区为上海
RUN apk add --no-cache python3 py3-pip tzdata && \
    cp /usr/share/zoneinfo/Asia/Shanghai /etc/localtime && \
    echo "Asia/Shanghai" > /etc/timezone

# 设置工作目录
WORKDIR /app

# 复制代码并安装依赖（使用腾讯云内网 pip 源加速）
COPY . .
RUN pip config set global.index-url http://mirrors.cloud.tencent.com/pypi/simple && \
    pip config set global.trusted-host mirrors.cloud.tencent.com && \
    pip install --no-cache-dir -r requirements.txt

# 暴露端口（CloudBase 云托管默认端口是 8080，保持和之前一致）
EXPOSE 8080

# 启动命令
CMD exec gunicorn --bind :8080 --workers 1 --threads 4 --timeout 60 app:app