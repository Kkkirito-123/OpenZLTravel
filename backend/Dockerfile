FROM python:3.11-slim

# 容器内统一使用 UTF-8，并让日志立即输出到 Docker logs。
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    LANGGRAPH_STRICT_MSGPACK=true

WORKDIR /app

# backend/.env 被 .dockerignore 排除：密钥只在容器启动时注入，
# 不会被复制进镜像层。
COPY backend /app/backend
COPY langgraph.json /app/langgraph.json

RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -e "/app/backend[dev]"

EXPOSE 2024

# 容器里不需要文件热重载；修改代码后重新 build，运行边界更可预期。
CMD ["langgraph", "dev", "--config", "langgraph.json", "--host", "0.0.0.0", "--port", "2024", "--no-browser", "--no-reload"]
