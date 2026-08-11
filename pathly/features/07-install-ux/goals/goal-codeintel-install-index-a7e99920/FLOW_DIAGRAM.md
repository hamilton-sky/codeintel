# Flow Diagram — codeintel install/index/query/status CLI

## CLI dispatch flow

```
codeintel <subcommand>
        │
        ├─ serve ──────────────────────────────────────────► MCP server (stdio)
        │                                                    (already exists)
        │
        ├─ index [project_root] ──► load_config()
        │                          │
        │                          ├─► SemanticDb.init()
        │                          └─► Indexer.index(root)
        │                              │
        │                              ├─► _cleanup_deleted()
        │                              ├─► _collect_new_chunks()
        │                              └─► _embed_and_write()
        │                                  │
        │                          [best-effort] Reindexer._graph_reindex()
        │                                  │
        │                          print "Indexed N chunks" / "Nothing new"
        │
        ├─ query --op X --target Y ──► _build_gateway()
        │   [--engine E]               │
        │   [--project-root P]         └─► Gateway.query(op, target, engine, root)
        │                                  │
        │                         ┌────────┴───────────┐
        │                         ▼                    ▼
        │                    SingleEngine           FanOut
        │                    dispatch               (both/all)
        │                         │                    │
        │                         └────────┬───────────┘
        │                                  ▼
        │                         Result or safe-null
        │                                  │
        │                         print result / "No result (reason:...)"
        │
        ├─ status [project_root] ──► code_status_handler()
        │                           │
        │                           ├─► probe GraphProvider.available
        │                           ├─► probe LspProvider.available
        │                           ├─► probe SemanticProvider.available
        │                           └─► stat .codeintel/semantic.db (mtime)
        │                               │
        │                           print engine table + index age
        │
        └─ install --agent A ──► Installer.register(A)  [or register_all()]
                                    │
                                    ├─ claude ──► ~/.claude/settings.json
                                    │             mcpServers.codeintel
                                    │
                                    ├─ codex  ──► ~/.codex/config.json
                                    │             mcpServers.codeintel
                                    │
                                    ├─ gemini ──► ~/.gemini/settings.json
                                    │             mcpServers.codeintel
                                    │
                                    └─ zed    ──► ~/.config/zed/settings.json
                                                  context_servers.codeintel
```

## Config load cascade

```
load_config(project_root)
        │
        ├─ read <project_root>/.codeintel.toml  ──► [project overrides]
        │    (missing → skip, no error)                     │
        │                                                    │
        ├─ read ~/.codeintel/config.toml  ────────► [global defaults]
        │    (missing → skip, no error)                     │
        │                                                    │
        └─ built-in defaults ────────────────────► [backend=auto, semantic=on, ...]
                                                            │
                                           merge: project wins > global wins > defaults
                                                            │
                                                   return merged dict
```

## Installer: idempotent JSON merge

```
register(agent)
        │
        ├─ load existing JSON (or {})
        │
        ├─ read nested key (e.g. mcpServers.codeintel)
        │         │
        │    matches target?
        │    ├─ YES ──► return {ok:True, action:"already"}
        │    └─ NO  ──► set nested key
        │               │
        │               write JSON (indent=2)
        │               │
        │               return {ok:True, action:"registered"}
        │
        └─ any exception ──► return {ok:False, action:"failed", reason:str(exc)}
```
