# syntax=docker/dockerfile:1
#
# codeintel HTTP transport container. Ships the in-house semantic engine (fastembed +
# sqlite-vec); the graph and LSP engines need their external backends (codebase-memory-mcp,
# uvx/serena) and are not bundled — codeintel degrades to a safe-null for those if absent.
#
# IMPORTANT: the default command binds 0.0.0.0 with --allow-remote. To fail closed, the container
# REFUSES TO START unless you set CODEINTEL_HTTP_TOKEN (bearer-token auth — recommended) or
# CODEINTEL_ALLOW_NO_AUTH=1 (serve unauthenticated on a trusted network). See docs/deploy.md.

# ---- build stage: produce a wheel ----
FROM python:3.12-slim AS build
WORKDIR /src
RUN pip install --no-cache-dir build
COPY . .
RUN python -m build --wheel --outdir /dist

# ---- runtime stage ----
FROM python:3.12-slim AS runtime
# libgomp1: onnxruntime (fastembed's backend, the semantic engine) links it; python:slim omits it.
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 codeintel
ENV HOME=/home/codeintel \
    PYTHONUNBUFFERED=1 \
    CODEINTEL_LOG_FORMAT=json \
    CODEINTEL_HTTP_ACCESS_LOG=1
COPY --from=build /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm -f /tmp/*.whl
USER codeintel
WORKDIR /home/codeintel
EXPOSE 8766

# Liveness probe hits the unauthenticated /healthz endpoint.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8766/healthz',timeout=3).status==200 else 1)"

ENTRYPOINT ["codeintel"]
CMD ["serve-http", "--host", "0.0.0.0", "--allow-remote"]
