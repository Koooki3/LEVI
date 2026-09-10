# Modified for LEVI (2026); see NOTICE and docs/UPSTREAM.md.
FROM oven/bun:1.3.10 AS frontend
WORKDIR /app
COPY package.json bun.lock ./
RUN bun install --frozen-lockfile
COPY src ./src
COPY public ./public
COPY next.config.ts postcss.config.mjs tsconfig.json ./
ENV NEXT_TELEMETRY_DISABLED=1
RUN bun run build

FROM python:3.11-slim
COPY --from=ghcr.io/astral-sh/uv:0.10.9 /uv /usr/local/bin/uv
COPY --from=frontend /usr/local/bin/bun /usr/local/bin/bun
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY . .
COPY --from=frontend /app/.next ./.next
COPY --from=frontend /app/node_modules ./node_modules
# Container filesystem is an intentional standalone workspace boundary.
ENV LEVI_WORKSPACE=/workspace UV_CACHE_DIR=/workspace/.cache/uv NEXT_TELEMETRY_DISABLED=1
RUN uv sync --locked --no-dev
EXPOSE 7860
CMD ["uv", "run", "--no-sync", "levi", "serve", "--host", "0.0.0.0"]
