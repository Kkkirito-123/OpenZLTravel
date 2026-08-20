FROM node:22-alpine

WORKDIR /app

# 先只复制依赖清单，代码改动时可以复用 npm ci 的镜像缓存层。
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

EXPOSE 5173

# Vite 在容器内必须监听 0.0.0.0，宿主机仍只暴露 loopback 端口。
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
