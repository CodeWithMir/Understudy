# ==============================================================================
# UNDERSTUDY - ALL-IN-ONE CONTAINER (used by deploy_docker.* when docker compose is not available)
# Multi-stage build: Python 3.11 (FastAPI, Playwright) + Node.js 24 LTS (web UI and JavaScript tools)
# Ports: 8000 (backend + demo ERP portals) and 3000 (Studio /wizard and Mission Control /)
#   docker build -t understudy .
#   docker run -d -p 3000:3000 -p 8000:8000 --env-file .env -v "$PWD/generated:/app/generated" understudy
# Build with --build-arg INSTALL_JAVA=true to preinstall Java + Maven (Understudy can also download them itself).
# ==============================================================================

# STAGE 1: Build Frontend Next.js App
FROM node:24-alpine AS frontend-builder
WORKDIR /build/understudy-ui

COPY understudy-ui/package*.json ./
RUN npm ci --no-audit --no-fund

COPY understudy-ui/ ./
ENV NEXT_TELEMETRY_DISABLED=1
ENV NODE_ENV=production
RUN npm run build

# STAGE 2: Python Backend & Production Runtime
FROM python:3.11-slim AS production
ARG INSTALL_JAVA=false

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1

WORKDIR /app

# Install system dependencies + Node.js runtime for serving Next.js
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    wget \
    gnupg \
    ca-certificates \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    && curl -fsSL https://deb.nodesource.com/setup_24.x | bash - \
    && apt-get install -y --no-install-recommends nodejs xvfb \
    && if [ "$INSTALL_JAVA" = "true" ]; then apt-get install -y --no-install-recommends default-jdk-headless maven; fi \
    && rm -rf /var/lib/apt/lists/*

# Install Python backend dependencies
COPY requirements.txt requirements-gcp.txt ./
# requirements-gcp.txt = requirements.txt + Firestore / Cloud Storage clients (used only when configured)
RUN pip install --no-cache-dir -r requirements-gcp.txt

# Install Playwright browser
RUN python -m playwright install --with-deps chromium

# Copy Python backend code
COPY . .

# Copy pre-built frontend from Stage 1
COPY --from=frontend-builder /build/understudy-ui/.next /app/understudy-ui/.next
COPY --from=frontend-builder /build/understudy-ui/node_modules /app/understudy-ui/node_modules

# Ensure the entrypoint is executable and the runtime folders exist
RUN chmod +x /app/entrypoint.sh && mkdir -p /app/generated /app/uploads /app/skills

# Expose backend (8000) and frontend (3000)
EXPOSE 8000 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD curl -fs http://127.0.0.1:8000/api/health || exit 1

# Start both services
ENTRYPOINT ["/app/entrypoint.sh"]
