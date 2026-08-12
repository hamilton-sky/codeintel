# Deploying codeintel

codeintel runs as **one local process**. There are two transports:

| Transport | Start with | Use for |
|---|---|---|
| **MCP (stdio)** | `codeintel serve` | An agent host that speaks MCP (Claude, Codex, Gemini, Zed). Registered by `codeintel install`. |
| **HTTP** | `codeintel serve-http` | A shared endpoint, a sidecar, or any harness that POSTs JSON. Loopback-only by default. |

This guide covers the HTTP transport for server / container / Kubernetes deployments.

---

## 1. Configuration

Per-repo settings live in `.codeintel.toml` (see the README). Operational settings are environment variables:

| Variable | Default | Effect |
|---|---|---|
| `CODEINTEL_HTTP_TOKEN` | — | Require `Authorization: Bearer <token>` on every request. **Set this for any non-loopback bind.** |
| `CODEINTEL_ALLOW_NO_AUTH` | — | `1` to explicitly serve **unauthenticated** on a non-loopback host (a trusted network). Without a token *or* this, a non-loopback bind refuses to start. |
| `CODEINTEL_LOG_LEVEL` | `WARNING` | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` |
| `CODEINTEL_LOG_FORMAT` | plain | `json` for structured logs (ELK / Splunk / Datadog) |
| `CODEINTEL_HTTP_ACCESS_LOG` | off | `1` to log one line per request (method, path, status, latency) |
| `CODEINTEL_DEBUG` | off | `1` to log the full traceback of any error the never-throw contract swallows |
| `CODEINTEL_REINDEX` | `on` | `off` disables the background reindexer (queries then index inline) |

The semantic index is a single per-machine SQLite file at `~/.codeintel/semantic.db`. Mount it on a persistent volume so a restart doesn't re-index from cold.

---

## 2. Endpoints

| Method / path | Auth (when token set) | Purpose |
|---|---|---|
| `POST /code/query` | required | The main query (`{op, target, engine}`) |
| `POST /code/doctor` | required | Per-engine health + index status |
| `GET /code/status` | required | Engine availability + index state (optional `?project_root=`) |
| `GET /metrics` | required | Prometheus exposition |
| `GET /healthz` | **never** | Liveness — always `200 {"status":"ok"}` |
| `GET /readyz` | **never** | Readiness — `200 {"status":"ready"}` once the gateway is up |

`/healthz` and `/readyz` are intentionally unauthenticated (probes don't send tokens) and reveal nothing beyond up/ready.

---

## 3. Authentication

```bash
export CODEINTEL_HTTP_TOKEN="$(openssl rand -hex 32)"
codeintel serve-http --host 0.0.0.0 --allow-remote      # reads the token from the env
```

Binding a non-loopback host **requires** `--allow-remote` **and** authentication: codeintel **refuses to start** on a non-loopback bind that has no token, unless you set `CODEINTEL_ALLOW_NO_AUTH=1` to explicitly opt into an unauthenticated endpoint on a trusted network (it fails closed by default). Clients send `Authorization: Bearer <token>`; the compare is constant-time.

> The built-in server bounds concurrent connections and drops idle clients, but stdlib `http.server` is **not** hardened for the open internet. For public exposure, put it behind a reverse proxy (TLS, rate-limiting, request-size limits) — see §6.

---

## 4. systemd

`/etc/systemd/system/codeintel.service`:

```ini
[Unit]
Description=codeintel HTTP transport
After=network.target

[Service]
Type=simple
User=codeintel
Environment=CODEINTEL_LOG_FORMAT=json
Environment=CODEINTEL_HTTP_ACCESS_LOG=1
EnvironmentFile=/etc/codeintel/env      # holds CODEINTEL_HTTP_TOKEN=...
ExecStart=/usr/local/bin/codeintel serve-http --host 127.0.0.1 --port 8766
Restart=on-failure
# codeintel handles SIGTERM gracefully (drains, then exits 0)
KillSignal=SIGTERM
TimeoutStopSec=15
# hardening
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/home/codeintel/.codeintel
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload && systemctl enable --now codeintel
```

---

## 5. Docker

```bash
docker build -t codeintel:latest .
docker run -d --name codeintel \
  -p 127.0.0.1:8766:8766 \
  -e CODEINTEL_HTTP_TOKEN="$(openssl rand -hex 32)" \
  -v "$PWD:/repo:ro" \
  -v codeintel-index:/home/codeintel/.codeintel \
  codeintel:latest
```

`docker-compose.yml`:

```yaml
services:
  codeintel:
    build: .
    ports: ["127.0.0.1:8766:8766"]
    environment:
      CODEINTEL_HTTP_TOKEN: ${CODEINTEL_HTTP_TOKEN:?set a token}
      CODEINTEL_LOG_FORMAT: json
    volumes:
      - ./:/repo:ro
      - codeintel-index:/home/codeintel/.codeintel
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8766/healthz',timeout=3).status==200 else 1)"]
      interval: 30s
      timeout: 5s
      retries: 3
volumes:
  codeintel-index:
```

The image bundles the **semantic** engine only. Index the mounted repo once (`docker exec codeintel codeintel index /repo`), or let the first query index it inline.

---

## 6. Kubernetes

```yaml
apiVersion: apps/v1
kind: Deployment
metadata: { name: codeintel }
spec:
  replicas: 1
  selector: { matchLabels: { app: codeintel } }
  template:
    metadata: { labels: { app: codeintel } }
    spec:
      containers:
        - name: codeintel
          image: codeintel:latest
          ports: [{ containerPort: 8766 }]
          env:
            - name: CODEINTEL_HTTP_TOKEN
              valueFrom: { secretKeyRef: { name: codeintel-token, key: token } }
            - name: CODEINTEL_LOG_FORMAT
              value: json
          livenessProbe:
            httpGet: { path: /healthz, port: 8766 }
            initialDelaySeconds: 15
          readinessProbe:
            httpGet: { path: /readyz, port: 8766 }
          resources:
            requests: { cpu: "250m", memory: "512Mi" }
            limits: { memory: "1Gi" }
          volumeMounts:
            - { name: index, mountPath: /home/codeintel/.codeintel }
      volumes:
        - name: index
          persistentVolumeClaim: { claimName: codeintel-index }
```

Probes hit the unauthenticated `/healthz` and `/readyz`. Scrape `/metrics` with a bearer token from the same secret (Prometheus `authorization.credentials`).

### Reverse proxy (nginx, TLS)

```nginx
server {
  listen 443 ssl;
  server_name codeintel.internal;
  ssl_certificate     /etc/ssl/codeintel.crt;
  ssl_certificate_key /etc/ssl/codeintel.key;
  client_max_body_size 1m;                 # matches the server's own 1 MiB cap
  location / {
    proxy_pass http://127.0.0.1:8766;
    proxy_read_timeout 60s;
    limit_req zone=codeintel burst=20 nodelay;   # rate-limit at the edge
  }
}
```

---

## 7. Observability

- **Metrics**: `GET /metrics` (Prometheus). Series: `codeintel_requests_total{method,path,status}`, `codeintel_request_duration_seconds_{sum,count}{path}`, `codeintel_requests_in_flight`, `codeintel_requests_rejected_total` (refused at the concurrency cap), `codeintel_build_info{version}`. Path labels are restricted to known routes, so cardinality is bounded.

  ```yaml
  scrape_configs:
    - job_name: codeintel
      metrics_path: /metrics
      authorization: { type: Bearer, credentials_file: /etc/prometheus/codeintel.token }
      static_configs: [{ targets: ["codeintel:8766"] }]
  ```

- **Logs**: `CODEINTEL_LOG_FORMAT=json` emits one JSON object per line (`ts, level, logger, msg`). `CODEINTEL_HTTP_ACCESS_LOG=1` adds per-request access lines. `CODEINTEL_DEBUG=1` surfaces otherwise-swallowed error tracebacks.

---

## 8. Security checklist

- [ ] `CODEINTEL_HTTP_TOKEN` set whenever the bind is not loopback-only.
- [ ] TLS terminated at a reverse proxy for any network exposure.
- [ ] Rate-limiting / request-size limits at the proxy (the server caps body at 1 MiB and bounds concurrency, but the proxy is your first line).
- [ ] Repo mounted **read-only**; codeintel never writes to your source.
- [ ] `/metrics` and `/code/*` reachable only by trusted callers; `/healthz` + `/readyz` may stay open for probes.
- [ ] The index volume (`~/.codeintel`) treated as sensitive — it contains embeddings of your code.
