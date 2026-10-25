FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        git \
        gnupg \
        grep \
        ripgrep \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Keep in step with @playwright/mcp in .mcp.json. That package pins
# playwright 1.64.0-alpha-1790635538000, so the browser build must match it;
# `@latest` would install a different browser revision than the MCP server uses.
RUN apt-get update \
    && npx -y playwright@1.64.0-alpha-1790635538000 install --with-deps chromium \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock

COPY agents ./agents
COPY web ./web
COPY README.md .

RUN mkdir -p /workspace
COPY .mcp.json /workspace/.mcp.json

WORKDIR /workspace

# The web workbench serves its static files from web/; binding inside a
# container also needs an explicit host and an allowed Host header, because the
# published port differs from the listening one:
#   docker run -p 8765:8765 -e MAWOAGENT_WEB_HOST=0.0.0.0 \
#     -e MAWOAGENT_WEB_ALLOWED_HOSTS=localhost:8765,127.0.0.1:8765 <image> \
#     python -m agents.web --port 8765
EXPOSE 8765

ENTRYPOINT ["python", "-m", "agents.main"]
