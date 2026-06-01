# Kubedian MCP server image.
FROM python:3.12-slim AS base

# kustomize for accurate rendering.
ARG KUSTOMIZE_VERSION=v5.4.3
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL "https://raw.githubusercontent.com/kubernetes-sigs/kustomize/master/hack/install_kustomize.sh" | bash -s -- "${KUSTOMIZE_VERSION#v}" /usr/local/bin \
    && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

RUN pip install --no-cache-dir ".[mcp]"

ENV KUBEDIAN_MCP_TRANSPORT=http \
    UVICORN_HOST=0.0.0.0 \
    UVICORN_PORT=8000
EXPOSE 8000

# Mount a manifests repo at /repo and index it, then serve.
# Override with `docker run ... kubedian index ...` as needed.
ENTRYPOINT ["kubedian-mcp"]
