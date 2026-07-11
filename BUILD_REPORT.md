# CacheLayer × GitHub Copilot (VS Code) — Build Report
## Spec: `copilot-vscode-plugin-build-prompt (1).md` v1.0

Repo (local): `/home/ubuntu/cachelayer-copilot-vscode-plugin`  
Intended GitHub URL: `https://github.com/befugngr/cachelayer-copilot-vscode-plugin`  
*(Push pending empty repo creation on GitHub — SSH auth as `befugngr` works; create-repo API token not available in this environment.)*

---

## Backend Symbol Map (§2.1)

| Concern | Symbol | File:line | Signature / use | Adapter? |
|--------|--------|-----------|-----------------|----------|
| Identity → scope_ns (account) | `scope_ns_for_user_record` | `cache/identity.py:46` | `(user: dict) -> str` → `u_<sha256(email)[:16]>` or `o_<org_id>` | Direct |
| Identity → scope_ns (user helper) | `_user_ns` | `cache/identity.py:38` | `(user_email: str) -> str` | Direct |
| Identity → scope_ns (org) | `_org_ns` | `cache/identity.py:42` | `(org_id: str) -> str` | Direct |
| Proxy key → identity | `resolve_identity` | `cache/identity.py:318` | `(api_key, redis) -> dict` incl. `scope_ns`, `_unauthorized` | Not used by connect-token path; keymap for proxy only |
| Legacy per-key ns | `_legacy_identity` / `make_namespace` | `cache/identity.py:297` / `config` | Deprecated; connect-token path must not land here | Avoided |
| Connect token resolve | `resolve_connect_token` | `auth/connect_tokens.py:31` | `(redis, token) -> dict\|None` | Direct |
| Token → scope_ns | `scope_ns_from_connect_record` | `auth/connect_tokens.py:48` | `(rec) -> str` prefers stored `scope_ns` | Direct |
| Exact key | `exact.make_key` | `cache/exact.py:31` | `(path, body, namespace) -> str` | Via `cache.step_lookup.step_key` |
| Exact get/set | `exact.get` / `exact.set` | `cache/exact.py:49` / `:66` | async redis | Direct |
| Exact payload | `exact.build_cache_payload` | `cache/exact.py:115` | build Redis JSON | Direct (MCP save) |
| Semantic lookup/store | `semantic.lookup` / `semantic.store` | `cache/semantic.py:248` / `:299` | FAISS per `mcp_<scope_ns>` | Direct |
| Embeddings | `semantic.get_embedding` | `cache/semantic.py:204` | messages + http + key | MCP only (hook uses exact; same key space) |
| Agent-safety gate | `check_agent_serve_safe` | `cache/agent_safety.py:221` | `(messages, payload) -> (bool, str)` | Via `step_lookup.serve_safe` |
| Fingerprint | `build_agent_fingerprint` | `cache/agent_safety.py:212` | messages → entities/tools | MCP save |
| Write refuse serve | `should_refuse_serve_for_payload` | `cache/side_effects.py:120` | payload → refuse write | Via `step_lookup.serve_safe` |
| Side-effect classify | `response_side_effect_summary` | `cache/side_effects.py:79` | body → worst effect | MCP save |
| Shared lookup (NEW) | `lookup_replayable_step` | `cache/step_lookup.py` | exact→semantic→safety | **Single source for MCP + hook** |
| Shared descriptor (NEW) | `normalize_step_descriptor` | `cache/step_lookup.py` | tool_name + camelCase/snake inputs | Hook + skill agreement |
| MCP auth | `ConnectTokenVerifier` | `auth/mcp_auth.py` | FastMCP Bearer `clct_` | Direct |
| Hook HTTP | `handle_pre_tool_use` | `auth/hooks.py` | POST `/hooks/pre-tool-use` | Calls shared lookup |

---

## MCP transport reality (§2.2)

- Process: FastMCP dual transport on `127.0.0.1:8770` (`mcp_server/cachelayer_mcp.py`).
- Paths: SSE `/sse` + messages `/messages/`; Streamable HTTP `/http`.
- nginx (`api.cachelayer.org`):
  - `location = /mcp/sse` → `:8770/sse`
  - `location = /mcp/http` → `:8770/http`
  - **NEW** `location = /mcp` → `:8770/http` (VS Code HTTP-first URL from spec §5.1)
- Evidence: `POST https://api.cachelayer.org/mcp` → **401** without token, **200** initialize with `clct_` Bearer.

---

## [DELEGATED DECISION]s

### §2.3 Hook-serve semantics → **(a) allow + additionalContext**
Justification: Safer, non-blocking; model may reuse injected context. Agent-safety already refuses write/entity-mismatch serves, so hard `deny` short-circuit is unnecessary for v1. Fail-open absolute on errors/401.

### §4.4 Path strategy → **(a) scripts with Copilot `hooks.json` commands**
Justification: Spec §4.2 locks `bash scripts/pre_tool_use.sh` + Windows override. Scripts are fail-open and self-contained (curl + python3 mapping). Prefer (b) only if §9 install proves cwd breakage — **VS Code install not available on this EC2**; flagged for user verification.

### §4.7 PostToolUse auto-save → **omit**
Justification: MCP `save_step` + skill discipline sufficient; auto-save adds latency on every tool and risks caching incomplete/unsafe results. Fail-open PreToolUse already deterministic for lookup.

---

## Shared normalization (§5.3)

Lowercase, trimmed, verb + target:

- `read file <path>`
- `write file <path>`
- `edit file <path>`
- `run command <cmd>`
- `search <query>`

Implemented in `cache.step_lookup.normalize_step_descriptor` (VS Code camelCase `filePath` + Claude `file_path`). Skill + hook + MCP descriptions must use these forms.

Evidence: hook `create_file` + `{filePath:"src/auth.ts"}` → description `write file src/auth.ts` → same `step_key` as MCP `save_step`/`lookup_step`.

---

## Observed VS Code `tool_name` values (§4.3 / §9.7)

**Not observed live** (no VS Code + Copilot on this host). Branched against documented VS Code names from [Agent hooks](https://code.visualstudio.com/docs/copilot/customization/hooks) and community lists, plus Claude names for PascalCase payloads per [GitHub Copilot hooks reference](https://docs.github.com/en/copilot/reference/hooks-reference):

Documented / coded for: `create_file`, `replace_string_in_file`, `apply_patch`, `read_file`/`view`, `bash`/`powershell`/`run_in_terminal`, `grep`/`glob`/`semantic_search`, and Claude `Write`/`Edit`/`Read`/`Bash`/`Grep`/`Glob`.

**Action required:** After install, capture real names from **GitHub Copilot Chat Hooks** output channel and extend the mapper if any differ.

---

## §9 Verification Matrix

| # | Gate | Result | Evidence |
|---|------|--------|----------|
| 1 | Backend reachability | **PASS** | `/mcp` 401/200; `/hooks/pre-tool-use` 401/200 (pasted in session) |
| 2 | Symbol binding | **PASS** | Hook + MCP `lookup_step` → `cache.step_lookup.lookup_replayable_step`; imports in `auth/hooks.py`, `mcp_server/cachelayer_mcp.py` |
| 3 | Install & discovery | **BLOCKED** | Needs VS Code UI — user must run Chat: Install Plugin From Source |
| 4 | Identity coherence | **PASS** | `step_key` `oai_proxy:u_23064130824dac25:…` = registered `_user_ns(security-fix@…)` not legacy key ns |
| 5 | Cross-plane | **PASS** | MCP save → hook hit + `additionalContext` for `write file src/auth.ts` |
| 6 | Agent-safety | **PASS** | Write-shaped save → `non_replayable:true`; lookup → `hit:false`, `reason:write-tool` |
| 7 | Hook tool names | **PARTIAL** | Mapper ready; live channel names not captured |
| 8 | Fail-open | **PASS** (script) | Bad URL → `{"continue":true}` exit 0. Full “stop backend” agent run needs VS Code |
| 9 | Lifecycle | **BLOCKED** | Needs VS Code disable plugin |
| 10 | Secret hygiene | **PASS** | Repo grep: no secrets |

---

## Doc deltas (live docs vs this spec)

1. Spec path `code.visualstudio.com/docs/copilot/customization/agent-hooks` → **404**. Live hooks doc: [code.visualstudio.com/docs/copilot/customization/hooks](https://code.visualstudio.com/docs/copilot/customization/hooks) (also `/docs/agent-customization/hooks`).
2. Plugin MCP docs examples emphasize `command` servers; HTTP `type`/`url`/`headers`/`inputs` confirmed in [MCP configuration reference](https://code.visualstudio.com/docs/copilot/reference/mcp-configuration). Spec §5.1 shape matches that reference (`mcpServers` key for plugins per [agent plugins](https://code.visualstudio.com/docs/copilot/customization/agent-plugins)).
3. GitHub hooks reference maps PascalCase `PreToolUse` payloads toward Claude tool names (`Write`/`Edit`); VS Code hooks doc says VS Code uses `create_file` / `replace_string_in_file`. Mapper accepts both.

---

## Scope fence compliance

- No model-call interception / MITM / subscription-token relay.
- Backend changes: shared `cache/step_lookup.py`, `auth/hooks.py` (shared lookup + `additionalContext`), MCP `lookup_step` uses shared function, nginx `location = /mcp`.
- Plugin-only surface otherwise.

---

## User actions to finish Definition of Done

1. Create empty GitHub repo `befugngr/cachelayer-copilot-vscode-plugin` (or provide `GH_TOKEN`), then ask agent to `git push -u origin main`.
2. In VS Code: install plugin, set `CACHELAYER_TOKEN`, complete §9.3 / §9.7 / §9.8 / §9.9 and paste Hook channel `tool_name` values.
