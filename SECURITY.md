# Security Policy

## Supported versions

codeintel follows semantic versioning. Security fixes land on the latest `0.x` release; please
upgrade to the newest published version (`pip install -U codecortex`) before reporting.

## Reporting a vulnerability

Please report vulnerabilities **privately** — do not open a public issue.

- Preferred: GitHub → the repository's **Security** tab → **Report a vulnerability** (private
  advisory). This keeps the report confidential until a fix is available.

Include the version, a description, and a minimal reproduction. We aim to acknowledge within a few
days and to ship a fix in a patch release, crediting reporters who wish to be named.

## Threat model

codeintel is **local-first**: one process, no cloud service, no outbound network per query, no
telemetry. What that means for security:

- **Default transport is loopback-only.** `serve-http` refuses to bind a non-loopback host without
  `--allow-remote`, and warns loudly if you do so without a token.
- **Authentication** for the HTTP transport is an optional bearer token (`--token` /
  `CODEINTEL_HTTP_TOKEN`), compared in constant time. `/healthz` and `/readyz` are intentionally
  unauthenticated and reveal only up/ready.
- **Not hardened for hostile networks.** The built-in server bounds body size (1 MiB), concurrency,
  and idle connections, but stdlib `http.server` is not a public-internet edge. Front it with a
  reverse proxy (TLS, rate-limiting) — see [docs/deploy.md](docs/deploy.md).
- **The index is sensitive.** `~/.codeintel/semantic.db` holds embeddings of your code; treat it as
  you would the source.
- **Untrusted query input is expected.** Cypher/SQL are parameterized or escaped; providers never
  interpolate a raw `target` into a query.

## Out of scope

- Denial of service from a client that has already been granted network access and a valid token.
- The security of the optional third-party backends (`codebase-memory-mcp`, `serena`) — report
  those upstream.
