# Plan — Persona Console (Character page ↔ agent personas)

Status: **DEFERRED** (2026-05-11) · Created 2026-05-11

> **Deferred — not being built now.** Reason: of the three agent backends only
> **openclaw** can actually do a per-request persona switch over a native HTTP
> interface (`GET /v1/models` + `model:"openclaw/<id>"`); zeroclaw has a single
> workspace persona with no gateway switch (upstream #5890 would change that),
> and hermes has no plain HTTP chat endpoint at all (its programmatic interface
> is ACP/MCP — folds into the unified-chat / ACP track, `docs/PLAN-UNIFIED-CHAT.md`).
> Building a one-backend "console" wasn't worth it yet.
>
> **Interim approach (shipped):** the companion keeps delivering the active
> character as a **prompt prefix** to whatever agent it talks to, now wrapped in
> a `PERSONA_OVERRIDE_PREAMBLE` (`apps/companion-server/src/main.rs`) that tells
> the model the character outranks its built-in identity/SOUL.md (scoped to
> identity/voice — the agent keeps its tools/rules). Good enough for the
> "costume over Kulukai" case; revisit this plan when openclaw is the primary
> backend or zeroclaw #5890 lands.

## Goal

Turn the companion's **Character page** into a persona console that works against
any agent backend (zeroclaw / openclaw / hermes / custom), local or remote, over
the gateway only — no shell or filesystem access to the agent host required.

What it does:

- **Discover** the personas an agent exposes and let you **pick** which one is active.
- **Bind** each persona to a companion-side presentation: a Live2D **avatar model** + a **TTS voice**.
- **Route** chat to the selected persona (using the agent's native mechanism, no prompt hacks where the agent supports it).
- **Tell the user how to add a persona** on their agent (per-backend copy-paste), since the companion can't create/edit them remotely.

Out of scope (for now): creating or editing persona text from the companion;
making zeroclaw switch between multiple personas (it can't, see below).

## Capability matrix (what each backend supports, gateway-only)

| backend | list personas | switch persona | how chat carries the persona | notes |
|---|---|---|---|---|
| **openclaw** | ✅ `GET /v1/models` → `openclaw/<id>` entries | ✅ per request | `model: "openclaw/<id>"` on `/v1/chat/completions` — the named agent's `workspace/SOUL.md` *is* the persona; nothing injected | one gateway, N personas; isolated memory per agent. WeChat etc. unaffected. **Verified** on the Pi. |
| **hermes** | ⚠️ `hermes profile list` (CLI — needs the bridge to surface it) | ⚠️ per request, via `HERMES_HOME` / profile in the bridge invocation | bridge runs `HERMES_HOME=<profile-home> hermes -z "<msg>"` | requires the `hermes-bridge.py` to accept a `profile`/`HERMES_HOME` per request + expose `GET /profiles`. Each profile has its own `SOUL.md` + memory. |
| **zeroclaw** | ➖ one persona only (the active workspace's) | ❌ not via the gateway (`active_workspace` is global config + CLI; `/api/workspaces` isn't a real endpoint; `?agent=` on `/api/personality` is reserved for upstream #5890) | **prompt-prefix injection** — `POST /webhook {message: "<companion character persona>\n\nUser message: <msg>"}` (current behavior, kept) | the agent's own `SOUL.md` is still active underneath → layered/"costume". To get >1 switchable character on zeroclaw: run multiple zeroclaw daemons (companion picks the *endpoint*), or wait for #5890. |
| **custom** | treated like zeroclaw (`/webhook` shape) | ❌ | prompt-prefix injection | |

So: openclaw/hermes get a **real persona switch**; zeroclaw/custom keep the
**prompt-prefix** approach. The Character page surfaces which mode is in effect.

## Data model

`companion.characters.json` (next to `companion.toml`) — a companion **character**
becomes a *presentation binding*, not the persona store:

```jsonc
{
  "active_id": "asuna",
  "characters": [
    {
      "id": "asuna",
      "name": "Asuna",
      "avatar_model_id": "asuna",          // Live2D model
      "tts_voice": null,                    // null = engine default
      // EXACTLY ONE of the next two is the "persona source":
      "agent_persona_id": "openclaw/asuna", // openclaw/hermes: the agent persona to route to
      "system_prompt": "You are Asuna…"     // zeroclaw/custom: text injected as a prompt prefix
      // (notes / *.md attachments stay supported and get appended to system_prompt when injecting)
    }
  ]
}
```

- If the active backend supports switching and the character has an `agent_persona_id` → **route** (`model:openclaw/<id>` or hermes profile), don't inject.
- Otherwise → **inject** `system_prompt` (+ notes + `*.md` attachments) as today.
- A persona an agent reports that has **no** companion character bound to it → shown in the
  picker as **"discovered — assign an avatar & voice"**. A character with a binding → **"configured"**.
  (This is the "label companion-managed vs agent-native" the page should show.)

Migration: existing `companion.characters.json` entries already have
`system_prompt` → they keep working unchanged against zeroclaw. `agent_persona_id`
is a new optional field; `model_id` (avatar) → renamed to `avatar_model_id` with
a back-compat read of the old key.

## Backend abstraction (`companion-core`)

A `PersonaBackend` per `AgentKind`:

```rust
trait PersonaBackend {
    /// Personas the agent exposes (empty / single for zeroclaw).
    async fn list_personas(&self) -> anyhow::Result<Vec<AgentPersona>>; // {id, display_name}
    /// Whether `model`-style per-request switching is available.
    fn supports_switch(&self) -> bool;
    /// Send a chat turn for the given character (routes or injects per supports_switch + binding).
    async fn chat(&self, character: &Character, message: &str, session_id: Option<&str>) -> anyhow::Result<String>;
    /// Markdown instructions for adding a new persona on this backend.
    fn add_persona_help(&self) -> String;
}
```

- **openclaw**: `list` = `GET /v1/models` → keep `id`s starting `openclaw/`; `chat` = `POST /v1/chat/completions {model: "openclaw/<id>", messages:[{role:"user",content:msg}], }` (+ pass `session`/conversation id however openclaw wants it — confirm); if a character has no `agent_persona_id`, fall back to `model:"openclaw"` with a `{role:"system",content:<persona>}` message. `add_persona_help` = the `openclaw agents add … && edit workspace/SOUL.md` recipe.
- **hermes**: `list` = bridge `GET /profiles` (bridge runs `hermes profile list --json`); `chat` = bridge `POST /webhook {message, profile}` (bridge: `HERMES_HOME=<home-for-profile> hermes -z "<msg>"`); `add_persona_help` = `hermes profile create … && edit ~/.hermes-…/SOUL.md`. **Requires updating `hermes-bridge.py` on the Pi** (currently it ignores profile).
- **zeroclaw / custom**: `list` = `[]` (or a single synthetic "this agent's persona", name parsed from `GET /api/personality` IDENTITY.md when available); `supports_switch = false`; `chat` = current `send_chat_in_session` with `compose_persona_prefix` prepended to the message; `add_persona_help` = "zeroclaw uses one persona per instance and can't switch via the API — the companion injects your selected character as a prompt prefix instead. To change the agent's own base persona: edit `~/.zeroclaw/workspace/SOUL.md` (or `PUT /api/personality/SOUL.md`). For multiple switchable characters: run more zeroclaw instances, or use an openclaw backend."

## Server (`companion-server`)

- `GET /api/characters` → `{ active_id, characters: [...], backend: { kind, url, supports_switch }, discovered_personas: [{id, display_name, bound_character_id|null}] }` — merges the local bindings with `PersonaBackend::list_personas()` (best-effort; if the agent is unreachable, just return the local list + `discovered_personas: []`).
- `POST /api/characters/active {id}` — set active.
- `PUT /api/characters/{id}` — edit the *binding* (avatar, voice, agent_persona_id, and `system_prompt` for the injecting backends). Persona *text* of a routed persona is not editable here (it's on the agent) — the response includes the `add_persona_help` text instead.
- `/api/chat` — dispatches through the active character's `PersonaBackend::chat()`. Drop the unconditional `compose_persona_prefix` (it moves inside the zeroclaw/custom backend).

## Web — Character page

Single UI for all backends:

- Header: **Agent: `<kind>` @ `<url>` — persona switching: `supported` / `not supported (prompt-injection mode)`**.
- **Persona list**: union of companion characters + `discovered_personas`. Each row: avatar thumbnail (or "no avatar — pick one"), name, badge (`configured` / `discovered` / `injected`), and a "set active" button. Active one highlighted (this is also where the `session a1b2` chip from the chat panel ties in).
- Per-character editor (right pane): avatar model picker, TTS voice picker, and — only when `supports_switch` and the character is `agent_persona_id`-bound — a read-only display of "persona: managed on the agent (`<id>`)" with the `add_persona_help` snippet. For injecting backends, the `system_prompt`/notes editor stays.
- **"How to add a character"** panel (collapsible, always visible): renders `add_persona_help` for the current backend — copy-paste commands.
- If `supports_switch == false` (zeroclaw/custom): a one-line note up top — *"On a zeroclaw backend there's a single agent persona; the companion sends your selected character as a prompt prefix on top of it. To switch between multiple distinct characters, see 'How to add a character'."*

## Build order

1. `companion-core`: `PersonaBackend` trait + impls (openclaw, hermes, zeroclaw/custom). Move `compose_persona_prefix` use into the zeroclaw/custom impl.
2. `companion-server`: update `/api/characters*` + `/api/chat` dispatch; characters file schema (`agent_persona_id`, `avatar_model_id` rename w/ back-compat).
3. `hermes-bridge.py` (Pi): add `GET /profiles` + accept `profile` in `POST /webhook` → `HERMES_HOME` per call. (Test before openclaw/hermes get shut down on the Pi.)
4. `web/src/pages` Character page: rebuild per the above; the per-backend help panel.
5. E2E: verify against (a) the Pi's zeroclaw (injection path, picker shows 1, note shown), (b) the Pi's openclaw (`model:openclaw/<id>` switch — make a test agent), (c) hermes via the updated bridge. Then the user retires openclaw+hermes on the Pi; the code paths stay for other deployments.

## Notes / decisions still open

- openclaw `/v1/chat/completions`: confirm how it wants the conversation/session id (so multi-turn memory works) — header? body field? Test.
- Should the picker also let you point a character at a *different agent URL* (so "Asuna = zeroclaw-A, Haru = zeroclaw-B")? That's the "N characters = N zeroclaw daemons" story. Probably yes, but can be a follow-up — for v1 a character uses the one configured backend.
- Avatar/voice live in `companion.characters.json`; persona text for routed personas lives on the agent. So a "character" is split across two homes for openclaw/hermes — acceptable, the page makes it clear.
