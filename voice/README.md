# Voice service — Phase 2

Pipecat on Cloud Run, wrapping a realtime provider. **Not built yet.** This directory
exists so the component has an address before it has code.

Two invariants shape it before a line is written:

- **Invariant 2 — it never touches the news DB.** It takes a session config (persona,
  tools, MCP servers, context, turn policy) and calls tools. No database credentials, no
  schema knowledge. That is what lets it be reused — by Zimmer, among others — instead of
  being welded to Motet's data model.
- **Invariant 1 — the client never speaks a vendor protocol.** Clients talk to this
  service's contract, never to a realtime provider directly, so swapping providers is a
  service change rather than a client rewrite.

The contract it will implement:

```
StartSession(persona, tools, mcp_servers, context, turn_policy) -> session_token
```

emitting transcripts, audio chunks, tool calls, and `interrupted_at(offset)`.

**Do a barge-in spike first** — throwaway, half a day, comparing providers on a windy walk
with an open mic. It settles the provider question with data instead of argument.
