FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Ingress is this binary dialling out to Cloudflare. There is no load balancer and
# the task takes no inbound connections, so the tunnel client ships in the image.
ARG CLOUDFLARED_VERSION=2026.8.3
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL -o /usr/local/bin/cloudflared \
       "https://github.com/cloudflare/cloudflared/releases/download/${CLOUDFLARED_VERSION}/cloudflared-linux-$(dpkg --print-architecture)" \
    && chmod +x /usr/local/bin/cloudflared \
    && cloudflared --version \
    && apt-get purge -y --auto-remove curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY app ./app
RUN pip install .

COPY alembic.ini ./
COPY alembic ./alembic
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
    && useradd --system --uid 10001 --no-create-home appuser

USER appuser
EXPOSE 8000

# The commit this image was built from, read back out over `/healthz` so that a
# pull-based deploy can tell which bytes are serving. Deliberately the last layer:
# it changes on every single build, and anything above it would be a cache miss on
# an arm64 build that is already emulated and slow.
ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}

CMD ["docker-entrypoint.sh"]
