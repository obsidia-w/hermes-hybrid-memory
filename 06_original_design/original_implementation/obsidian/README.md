# Hermes Obsidian Memory Provider

User-installed local Memory Provider for `NousResearch/hermes-agent`.

## Current v0.1 vertical slice

- Captures completed turns as append-only Markdown source notes.
- Maintains a rebuildable SQLite/FTS5 index outside the Vault.
- Extracts conservative, explicit user preferences and project facts locally.
- Creates atomic memory notes with provenance, status, trust and promotion metadata.
- Prefetches relevant active memories before model calls.
- Mirrors explicit built-in memory writes into the Vault.
- Reconciles high-signal provider-owned `obs:` entries into `MEMORY.md` and `USER.md`.
- Supports inspect, correction and logical forgetting through one gated model tool.
- Provides status, doctor, reindex, curate and reflection CLI commands.

## Files

- Vault Markdown is the source of truth.
- `$HERMES_HOME/obsidian-memory/index.sqlite3` is a rebuildable derived index.
- `$HERMES_HOME/obsidian-memory/config.yaml` stores non-secret configuration.

## Activation

Install this directory as `$HERMES_HOME/plugins/obsidian/`, then set:

```yaml
memory:
  provider: obsidian
```

The configured Vault for this profile is:

```text
/home/hermes/Documents/Codex/Hermes-Memory-Vault
```

## CLI

```bash
hermes obsidian status
hermes obsidian doctor
hermes obsidian reindex
hermes obsidian curate --limit 100
hermes obsidian reflect --period daily
```

## Safety properties

- Refuses writes through symbolic links or outside the Vault.
- Uses atomic replace for generated Markdown.
- Does not physically delete long-term memory through the model tool.
- Treats recalled memory text as reference data, not instructions.
- Skips automatic extraction for detected secrets and prompt-injection patterns.
- Only manages built-in cache entries prefixed with `obs:`.
