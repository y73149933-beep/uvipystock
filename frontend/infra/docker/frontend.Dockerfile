# Frontend Dockerfile: Vite multi-stage build → nginx static
# Stage 1: build the React SPA
# Stage 2: serve static files with nginx

# ─── Stage 1: Build ──────────────────────────────────────────────────────────
FROM node:20-alpine AS build

WORKDIR /app

# Copy package files first (layer caching)
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci || npm install

# Copy source and build
COPY frontend/ ./
RUN npm run build

# ─── Stage 2: Serve with nginx ───────────────────────────────────────────────
FROM nginx:alpine AS runtime

# Copy built assets
COPY --from=build /app/dist /usr/share/nginx/html

# nginx config: SPA fallback + /api proxy to backend
RUN echo 'server { \
    listen 80; \
    \
    # Proxy /api requests to the backend container \
    location /api { \
        proxy_pass http://backend:8000; \
        proxy_set_header Host $host; \
        proxy_set_header X-Real-IP $remote_addr; \
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for; \
        proxy_set_header X-Forwarded-Proto $scheme; \
        \
        # WebSocket support (for /api/v1/ws/*) \
        proxy_http_version 1.1; \
        proxy_set_header Upgrade $http_upgrade; \
        proxy_set_header Connection "upgrade"; \
        proxy_read_timeout 86400s; \
        proxy_send_timeout 86400s; \
    } \
    \
    # Swagger docs \
    location /docs { \
        proxy_pass http://backend:8000; \
        proxy_set_header Host $host; \
    } \
    location /openapi.json { \
        proxy_pass http://backend:8000; \
        proxy_set_header Host $host; \
    } \
    \
    # SPA fallback \
    location / { \
        root /usr/share/nginx/html; \
        try_files $uri $uri/ /index.html; \
    } \
}' > /etc/nginx/conf.d/default.conf

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -q -O /dev/null http://localhost/ || exit 1

CMD ["nginx", "-g", "daemon off;"]
