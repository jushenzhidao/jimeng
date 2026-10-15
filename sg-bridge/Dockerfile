FROM mcr.microsoft.com/playwright:v1.48.0-noble

WORKDIR /app
COPY package.json ./
# 浏览器已在官方镜像内（PLAYWRIGHT_BROWSERS_PATH 指向预装目录）；
# playwright-core 只做驱动，不再下载浏览器。
RUN npm install --omit=dev

COPY bridge.js ./
ENV SG_PROFILE_DIR=/data/profile SG_LISTEN=8300
VOLUME ["/data/profile"]
EXPOSE 8300
CMD ["node", "bridge.js"]
