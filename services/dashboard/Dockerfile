# Treadmill operator dashboard — multi-stage Node build + nginx serve.
# Mirrors bunkhouse/services/dashboard/Dockerfile but with the v1 dependency cut
# (no Google OAuth, no @xyflow/recharts; auth deferred to multi-tenant later).

FROM node:22-alpine AS build
WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm install --no-audit --no-fund

COPY . .
RUN npm run build

FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
