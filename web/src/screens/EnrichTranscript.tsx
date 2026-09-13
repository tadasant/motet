// PROTOTYPE — the agent transcript of an enrichment run, rendered as a readable list.
//
// The owner's requirement: review what the agent did. Every entry is one line of the
// redacted transcript the worker stored — a tool call (role + tool + args), its result
// (an excerpt), or the model's own text — with the wall-clock time it happened. The
// redaction happened at write time, so there is nothing here to hide client-side.

import { useEffect, useState } from "react";

import { api } from "../api/client";
import type { components } from "../api/schema.gen";

type Transcript = components["schemas"]["EnrichTranscriptResponse"];
type Entry = components["schemas"]["EnrichTranscriptEntry"];

function clock(iso: string): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return iso;
  return at.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function toolLabel(tool: string | null | undefined): string {
  if (!tool) return "tool";
  if (tool.startsWith("playwright_"))
    return `browser · ${tool.slice("playwright_".length)}`;
  const [server, ...rest] = tool.split("__");
  return rest.length > 0
    ? `${server?.split("_")[0] ?? server} · ${rest.join("__")}`
    : tool;
}

function Line({ entry }: { entry: Entry }) {
  const [open, setOpen] = useState(false);
  const role =
    entry.kind === "assistant"
      ? "model"
      : entry.kind === "tool_call"
        ? "call"
        : "result";
  const body =
    entry.kind === "assistant"
      ? entry.text
      : entry.kind === "tool_call"
        ? entry.args
        : entry.result;
  const long = (body ?? "").length > 220;
  return (
    <li
      className={`transcript-line kind-${entry.kind}${entry.ok === false ? " is-error" : ""}`}
    >
      <span className="transcript-seq mono">#{entry.seq}</span>
      <span className="transcript-at mono" title={entry.at}>
        {clock(entry.at)}
      </span>
      <span className={`transcript-role role-${role}`}>{role}</span>
      <span className="transcript-body">
        {entry.kind !== "assistant" && (
          <strong className="transcript-tool">{toolLabel(entry.tool)}</strong>
        )}
        {entry.kind === "tool_result" && entry.ok === false && (
          <span className="error"> error</span>
        )}
        <pre className={`transcript-text${open || !long ? "" : " is-clipped"}`}>
          {body ?? ""}
        </pre>
        {long && (
          <button
            type="button"
            className="linkish hint"
            onClick={() => setOpen((o) => !o)}
          >
            {open ? "less" : "more"}
          </button>
        )}
        {entry.kind === "assistant" && entry.cost_usd != null && (
          <span className="hint">
            {" "}
            · ${entry.cost_usd.toFixed(4)} for this turn
          </span>
        )}
      </span>
    </li>
  );
}

export function EnrichTranscript({ sourceItemId }: { sourceItemId: string }) {
  const [data, setData] = useState<Transcript | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    api
      .enrichTranscript(sourceItemId)
      .then((next) => {
        if (!cancelled) setData(next);
      })
      .catch((err: unknown) => {
        if (!cancelled)
          setError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [sourceItemId]);

  if (error) {
    return (
      <p className="error" role="alert">
        {error}
      </p>
    );
  }
  if (!data) return <p className="hint">Loading transcript…</p>;
  const calls = data.entries.filter((e) => e.kind === "tool_call").length;
  return (
    <div className="transcript" aria-label="Agent transcript">
      <p className="hint transcript-summary">
        {data.entries.length} entries · {calls} tool call
        {calls === 1 ? "" : "s"} ·{" "}
        {data.run.cost_usd != null
          ? `$${data.run.cost_usd.toFixed(4)}`
          : "cost unknown"}{" "}
        · redacted at write time (mailbox results, links, codes, addresses and
        cookies are not stored)
      </p>
      <ol className="transcript-list">
        {data.entries.map((entry) => (
          <Line key={entry.seq} entry={entry} />
        ))}
      </ol>
    </div>
  );
}
