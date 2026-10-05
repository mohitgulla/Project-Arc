/**
 * `/ops/runs/:runId` (E8.8e): a concise, full-page run detail.
 *
 * Summary header first (job label + persona, status, duration, trigger, scheduled-for, config
 * version, git sha, LLM cost and tokens, chain link), then Declared vs Actual, context as
 * counts per kind (ids one tap away), LLM calls, outputs, links, and two collapsed blocks:
 * the full D27 manifest and the last 50 log lines. Every number of the former page is still
 * reachable (D48: same level of detail, moved behind a disclosure).
 */
import { useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";

import { CappedList } from "../components/CappedList";
import { Card } from "../components/Card";
import { KeyValueList } from "../components/KeyValueList";
import { formatNumber } from "../lib/format";
import {
  LOG_LEVELS,
  contractRows,
  externalInputs,
  filterLog,
  formatDuration,
  llmCost,
  manifestGroups,
  personaLabel,
  runStatusLabel,
  type RunDetail,
  type StepView,
} from "../lib/ops";
import { isHashLike, kindCounts, kindCountsText, shortHash, tailLines, type KindCount } from "../lib/opsConfig";
import { useRun } from "../lib/useApi";
import { CONTROL, CopyValue, Disclosure, Loading, PersonaChip, Pill, RunStatus, et, shortId } from "./opsShared";

const LOG_TAIL = 50;

type Manifest = Record<string, unknown>;

function mnum(m: Manifest | null, k: string): number | null {
  const v = m?.[k];
  return typeof v === "number" ? v : null;
}

/** LLM cost and tokens for one step: the manifest's totals, else the sum of its persona calls. */
export function stepLlm(step: StepView): { cost: number | null; inTok: number; outTok: number; calls: number } {
  const m = step.manifest as Manifest | null;
  const calls = step.persona_calls;
  const sum = (f: (c: (typeof calls)[number]) => number | null | undefined) => calls.reduce((a, c) => a + (f(c) ?? 0), 0);
  const cost = mnum(m, "cost_usd") ?? (calls.some((c) => c.cost_usd != null) ? sum((c) => c.cost_usd) : null);
  return {
    cost,
    inTok: mnum(m, "input_tokens") ?? sum((c) => c.input_tokens),
    outTok: mnum(m, "output_tokens") ?? sum((c) => c.output_tokens),
    calls: calls.length,
  };
}

// ---------------------------------------------------------------------------
// Summary header
// ---------------------------------------------------------------------------

function Fact({ label, children, testid }: { label: string; children: React.ReactNode; testid?: string }) {
  return (
    <div className="min-w-0" data-testid={testid}>
      <dt className="text-micro font-semibold uppercase tracking-wide text-muted">{label}</dt>
      <dd className="text-caption font-semibold tabular-nums text-primary [overflow-wrap:anywhere]">{children}</dd>
    </div>
  );
}

function Summary({ data, step }: { data: RunDetail; step: StepView }) {
  const r = step.run;
  const m = step.manifest as Manifest | null;
  const sha = typeof m?.git_sha === "string" ? m.git_sha : null;
  const cfgV = mnum(m, "config_version");
  const llm = stepLlm(step);
  return (
    <Card
      testid="run-summary"
      title={
        <span className="flex min-w-0 flex-wrap items-center gap-2">
          <span className="[overflow-wrap:anywhere]">{step.label ?? r.job}</span>
          <PersonaChip persona={step.persona} />
        </span>
      }
      action={{ label: "OPS", to: "/ops" }}
    >
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <RunStatus r={r} />
        {!step.contract.ok && <Pill tone="neg">contract mismatch</Pill>}
        <code className="min-w-0 text-micro text-muted [overflow-wrap:anywhere]" title={r.run_id}>
          {r.job} · {r.run_id}
        </code>
      </div>
      {(r.error ?? r.summary) && (
        <p className={`mb-3 text-caption [overflow-wrap:anywhere] ${r.error ? "text-neg-text" : "text-secondary"}`} data-testid="run-outcome">
          {r.error ?? r.summary}
        </p>
      )}
      <dl className="grid grid-cols-2 gap-x-4 gap-y-2 min-[700px]:grid-cols-4">
        <Fact label="Duration">{formatDuration(r.duration_ms)}</Fact>
        <Fact label="Trigger">{r.reason || "—"}</Fact>
        <Fact label="Scheduled">{et(r.scheduled_for)}</Fact>
        <Fact label="Config">{cfgV === null ? "—" : `v${cfgV}`}</Fact>
        <Fact label="Git sha">{sha ? <CopyValue value={sha} shown={sha.slice(0, 12)} label="git sha" /> : "—"}</Fact>
        <Fact label="LLM" testid="run-llm">
          {llm.calls === 0 && llm.cost === null ? "none" : `${llm.cost === null ? "—" : llmCost(llm.cost)} · ${formatNumber(llm.inTok)} / ${formatNumber(llm.outTok)} tok`}
        </Fact>
        <Fact label="Attempt">{r.attempts}</Fact>
        <Fact label="Chain">
          {data.chain_run_id ? (
            <Link to={`/ops/runs/${data.chain[0]?.run.run_id ?? r.run_id}`} className="arc-action arc-hit" title={data.chain_run_id}>
              {data.chain.length > 1 ? `${data.chain.length} steps` : "single step"}
            </Link>
          ) : (
            "—"
          )}
        </Fact>
      </dl>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Chain steps
// ---------------------------------------------------------------------------

function ChainSteps({ data, selected, onSelect }: { data: RunDetail; selected: string; onSelect: (id: string) => void }) {
  return (
    <Card title="Chain" subtitle={data.chain_run_id ?? undefined}>
      <ol className="divide-y divide-line" data-testid="chain-steps">
        {data.chain.map((s) => {
          const on = s.run.run_id === selected;
          const llm = stepLlm(s);
          return (
            <li key={s.run.run_id}>
              <button
                type="button"
                aria-pressed={on}
                onClick={() => onSelect(s.run.run_id)}
                data-testid="chain-step"
                className={`arc-press flex min-h-[40px] w-full flex-wrap items-center gap-x-3 gap-y-1 rounded-control px-2 py-1.5 text-left text-caption max-tablet:min-h-[44px] ${
                  on ? "bg-range-active text-[color:var(--range-active-text)]" : "hover:bg-hover"
                }`}
              >
                <span className="w-5 shrink-0 text-muted tabular-nums">{s.run.step_index}</span>
                <span className="min-w-0 font-semibold [overflow-wrap:anywhere]">{s.label ?? s.run.job}</span>
                <span className={s.run.status === "failed" ? "text-neg-text" : "text-secondary"}>{runStatusLabel(s.run)}</span>
                <span className="ml-auto flex gap-3 tabular-nums text-muted">
                  <span>{formatDuration(s.run.duration_ms)}</span>
                  <span>
                    r{s.read.length} · w{s.wrote.length}
                  </span>
                  {llm.calls > 0 && <span>{llm.calls} LLM</span>}
                  {!s.contract.ok && <span className="text-neg-text">mismatch</span>}
                </span>
              </button>
            </li>
          );
        })}
      </ol>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Body
// ---------------------------------------------------------------------------

function ContractTable({ step }: { step: StepView }) {
  const c = step.contract;
  const rows = [
    ...contractRows(c.declared_reads, c.actual_reads, c.undeclared_reads).map((r) => ({ ...r, dir: "read" })),
    ...contractRows(c.declared_writes, c.actual_writes, c.undeclared_writes).map((r) => ({ ...r, dir: "write" })),
  ];
  return (
    <div data-testid="contract">
      {c.declared_reads === null && c.declared_writes === null ? (
        <p className="text-caption text-muted">This job declares no I/O contract.</p>
      ) : !c.ok ? (
        <p className="mb-2 rounded-control bg-neg-bg px-2 py-1 text-caption font-semibold text-neg-text" data-testid="contract-mismatch">
          Contract mismatch: undeclared {[...c.undeclared_reads.map((k) => `read ${k}`), ...c.undeclared_writes.map((k) => `write ${k}`)].join(", ")}
        </p>
      ) : (
        <p className="mb-2 text-caption text-pos-text">Reads and writes match the declared contract.</p>
      )}
      {rows.length > 0 && (
        <table className="w-full text-caption">
          <thead className="text-muted">
            <tr>
              <th className="py-1 text-left font-normal">Kind</th>
              <th className="text-left font-normal">Dir</th>
              <th className="text-center font-normal">Declared</th>
              <th className="text-center font-normal">Actual</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={`${r.dir}-${r.kind}`} data-mismatch={r.mismatch || undefined} className={`border-t border-line ${r.mismatch ? "bg-neg-bg text-neg-text" : ""}`}>
                <td className="py-1 [overflow-wrap:anywhere]">{r.kind}</td>
                <td>{r.dir}</td>
                <td className="text-center">{r.declared ? "✓" : "—"}</td>
                <td className="text-center">{r.used ? "✓" : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function KindChip({ c, open, onToggle, controls }: { c: KindCount; open: boolean; onToggle: () => void; controls: string }) {
  return (
    <button
      type="button"
      aria-expanded={open}
      aria-controls={controls}
      onClick={onToggle}
      data-testid="kind-count"
      data-kind={c.kind}
      data-undeclared={c.undeclared || undefined}
      className={`arc-press inline-flex min-h-[32px] items-center gap-1.5 rounded-pill px-2.5 text-caption max-tablet:min-h-[44px] ${
        c.undeclared ? "bg-neg-bg font-semibold text-neg-text" : open ? "bg-range-active text-[color:var(--range-active-text)]" : "bg-control text-secondary"
      }`}
    >
      <span>{c.kind}</span>
      <span className="tabular-nums font-semibold">{formatNumber(c.n)}</span>
      {c.undeclared && <span className="text-micro uppercase">undeclared</span>}
    </button>
  );
}

function ContextRefs({ title, items, testid }: { title: string; items: StepView["read"]; testid: string }) {
  const counts = kindCounts(items);
  const [open, setOpen] = useState<string | null>(null);
  const [ids, setIds] = useState(false);
  const shown = open ? items.filter((e) => e.kind === open) : [];
  const listId = `${testid}-list`;
  return (
    <div data-testid={testid} className="min-w-0">
      <div className="mb-1 flex items-baseline justify-between gap-2">
        <span className="text-micro font-semibold uppercase tracking-wide text-muted">
          {title} <span className="tabular-nums">{formatNumber(items.length)}</span>
        </span>
      </div>
      {counts.length === 0 ? (
        <p className="text-caption text-muted">None.</p>
      ) : (
        <>
          <p className="sr-only">{kindCountsText(counts)}</p>
          <div className="flex flex-wrap gap-2">
            {counts.map((c) => (
              <KindChip key={c.kind} c={c} open={open === c.kind} controls={listId} onToggle={() => setOpen(open === c.kind ? null : c.kind)} />
            ))}
          </div>
          {open && (
            <div id={listId} className="mt-2 rounded-control bg-control/40 p-2">
              <div className="mb-1 flex items-center justify-between gap-2">
                <span className="text-caption text-secondary">
                  {open} · {shown.length}
                </span>
                <button
                  type="button"
                  aria-pressed={ids}
                  onClick={() => setIds(!ids)}
                  className="arc-action arc-press inline-flex min-h-[32px] items-center max-tablet:min-h-[44px]"
                  data-testid={`${testid}-show-ids`}
                >
                  {ids ? "Hide ids" : "Show ids"}
                </button>
              </div>
              <CappedList className="grid gap-1 text-caption" noun={open} testid={`${testid}-entries`}>
                {shown.map((e) => (
                  <li key={e.id} className="min-w-0 [overflow-wrap:anywhere]">
                    <Link to={`/ops/context/${e.id}`} className={`arc-action normal-case tracking-normal ${e.undeclared ? "text-neg-text" : ""}`}>
                      {e.kind}:{e.subject}
                    </Link>
                    {ids && (
                      <code className="ml-2 text-micro text-muted" data-testid="context-id">
                        {e.id}
                      </code>
                    )}
                  </li>
                ))}
              </CappedList>
            </div>
          )}
        </>
      )}
    </div>
  );
}

function LlmCalls({ step }: { step: StepView }) {
  if (step.persona_calls.length === 0) return null;
  return (
    <Card title="LLM Calls">
      <CappedList className="divide-y divide-line text-caption tabular-nums" testid="persona-calls" noun="calls">
        {step.persona_calls.map((c) => (
          <li key={c.id} className="flex flex-wrap gap-x-3 py-1.5">
            <span className="font-semibold text-title">{personaLabel(c.persona)}</span>
            <span className="text-muted [overflow-wrap:anywhere]">{c.model}</span>
            <span>
              {formatNumber(c.input_tokens ?? 0)} / {formatNumber(c.output_tokens ?? 0)} tok
            </span>
            <span>{formatDuration(c.latency_ms)}</span>
            <span className="ml-auto">{c.cost_usd == null ? "—" : llmCost(c.cost_usd)}</span>
          </li>
        ))}
      </CappedList>
    </Card>
  );
}

function Outputs({ step }: { step: StepView }) {
  const entries = Object.entries(step.outputs);
  if (entries.length === 0) return null;
  return (
    <Card title="Outputs">
      <div className="grid gap-3" data-testid="outputs">
        {entries.map(([kind, refs]) => (
          <div key={kind} className="min-w-0">
            <div className="text-micro font-semibold uppercase tracking-wide text-muted">
              {kind} <span className="tabular-nums">{refs.length}</span>
            </div>
            <CappedList className="flex flex-wrap gap-x-3 gap-y-1 text-caption" noun={kind}>
              {refs.map((r) => (
                <li key={r.id} className="min-w-0 [overflow-wrap:anywhere]">
                  {r.route ? (
                    <Link to={r.route} className="arc-action normal-case tracking-normal" title={r.label}>
                      {shortId(r.label, 28)}
                    </Link>
                  ) : (
                    r.label
                  )}
                </li>
              ))}
            </CappedList>
          </div>
        ))}
      </div>
    </Card>
  );
}

function Links({ step }: { step: StepView }) {
  if (!(step.proposals.length || step.decisions.length || step.gate_decisions.length || step.slack_posts.length || step.events.length)) return null;
  const items = [
    step.proposals.length > 0 && {
      label: "Proposals",
      value: (
        <span className="flex flex-wrap justify-end gap-x-2">
          {step.proposals.map((p) => (
            <Link key={p.id} className="arc-action arc-hit normal-case tracking-normal" to={p.route ?? "#"}>
              {p.label}
            </Link>
          ))}
        </span>
      ),
    },
    step.decisions.length > 0 && { label: "Decisions", value: <span className="[overflow-wrap:anywhere]">{step.decisions.join(", ")}</span> },
    step.gate_decisions.length > 0 && { label: "Gate decisions", value: <span className="[overflow-wrap:anywhere]">{step.gate_decisions.join(", ")}</span> },
    step.slack_posts.length > 0 && {
      label: "Slack posts",
      value: (
        <span className="flex flex-wrap justify-end gap-x-2">
          {step.slack_posts.map((s) => (
            <a key={s.id} className="arc-action arc-hit normal-case tracking-normal" href={s.route ?? "#"} target="_blank" rel="noreferrer">
              {s.label} ↗
            </a>
          ))}
        </span>
      ),
    },
    step.events.length > 0 && { label: "Events", value: <span className="[overflow-wrap:anywhere]">{step.events.map((e) => `${e.role} ${e.name}`).join(", ")}</span> },
  ].filter(Boolean) as Array<{ label: string; value: React.ReactNode }>;
  return (
    <Card title="Links" testid="run-links">
      <KeyValueList items={items} />
    </Card>
  );
}

/** A manifest value: hashes shortened with copy-on-tap; long text wraps. */
function ManifestValue({ k, v }: { k: string; v: string }) {
  if (k !== "git_sha" && isHashLike(v)) return <CopyValue value={v} shown={shortHash(v)} label={k} />;
  if (k === "config_hashes" || k === "snapshot_ids" || k === "input_digest") {
    // `name: hash · name: hash` -> each hash shortened
    return (
      <span className="font-normal [overflow-wrap:anywhere]" title={v}>
        {v
          .split(" · ")
          .map((part) => part.replace(/([0-9a-f]{16,})/gi, (h) => shortHash(h)))
          .join(" · ")}
      </span>
    );
  }
  return <span className="font-normal [overflow-wrap:anywhere]">{v}</span>;
}

function FullManifest({ step }: { step: StepView }) {
  const m = step.manifest as Manifest | null;
  const groups = manifestGroups(m);
  const ext = externalInputs(m);
  return (
    <Disclosure title="Full Manifest" meta={m ? `${groups.reduce((a, g) => a + g.rows.length, 0)} fields` : "none recorded"} testid="full-manifest">
      {!m ? (
        <p className="text-caption text-muted">No run manifest was recorded for this run.</p>
      ) : (
        <div className="grid gap-4 desktop:grid-cols-2">
          {groups.map((g) => (
            <div key={g.title} className="min-w-0" data-testid="manifest-group">
              <h3 className="arc-micro-header mb-1">{g.title}</h3>
              <KeyValueList items={g.rows.map((r) => ({ label: r.label, value: <ManifestValue k={r.key} v={r.value} /> }))} />
            </div>
          ))}
          {ext.length > 0 && (
            <div className="min-w-0">
              <h3 className="arc-micro-header mb-1">External Inputs</h3>
              <KeyValueList items={ext.map((e) => ({ label: `${e.name} (${e.source})`, value: e.digest, hint: `as of ${e.asOf} · ${e.count}` }))} />
            </div>
          )}
        </div>
      )}
    </Disclosure>
  );
}

function RunLog({ data }: { data: RunDetail }) {
  const [level, setLevel] = useState<string>("info");
  const filtered = filterLog(data.log, level);
  const log = tailLines(filtered, LOG_TAIL);
  return (
    <Disclosure title="Log" meta={data.log_available ? `last ${log.length} of ${filtered.length}` : "no log file on this host"} testid="log-block">
      <div className="mb-2 flex items-center gap-2">
        <label className="flex items-center gap-2 text-caption text-secondary">
          Level
          <select aria-label="Log level" className={CONTROL} value={level} onChange={(e) => setLevel(e.target.value)}>
            {LOG_LEVELS.map((l) => (
              <option key={l} value={l}>
                {l}
              </option>
            ))}
          </select>
        </label>
        <span className="text-caption text-muted tabular-nums">{data.log.length} lines in the file window</span>
      </div>
      <ol className="font-mono text-micro" data-testid="run-log">
        {log.map((l, i) => (
          <li
            key={i}
            className={`border-b border-line py-1 [overflow-wrap:anywhere] ${l.level === "error" ? "text-neg-text" : l.level === "warning" ? "text-warn" : "text-secondary"}`}
          >
            <span className="text-muted">{l.ts ?? ""}</span> {l.level} <span className="font-semibold">{l.event}</span>{" "}
            {Object.entries(l.fields)
              .map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`)
              .join(" ")}
          </li>
        ))}
      </ol>
    </Disclosure>
  );
}

function StepBody({ step }: { step: StepView }) {
  return (
    <>
      <Card title="Declared vs Actual">
        <ContractTable step={step} />
      </Card>
      <Card title="Context Read / Written" testid="context-refs">
        <div className="grid gap-4 tablet:grid-cols-2">
          <ContextRefs title="Read" items={step.read} testid="ctx-read" />
          <ContextRefs title="Wrote" items={step.wrote} testid="ctx-wrote" />
        </div>
      </Card>
      <LlmCalls step={step} />
      <Outputs step={step} />
      <Links step={step} />
      <FullManifest step={step} />
    </>
  );
}

export function RunDetailPage() {
  const { runId } = useParams();
  const { data, error } = useRun(runId);
  const [params, setParams] = useSearchParams();
  if (!data) {
    return (
      <Card title="Run">
        <Loading error={error} what={`run ${runId}`} />
        <Link to="/ops" className="arc-action">
          ← Ops
        </Link>
      </Card>
    );
  }
  // `?step=<run id>` keeps the chosen chain step deep-linkable (default: the run itself).
  const stepId = params.get("step") ?? data.run_id;
  const selected = data.chain.find((s) => s.run.run_id === stepId) ?? data.step;
  const select = (id: string) => {
    const next = new URLSearchParams(params);
    if (id === data.run_id) next.delete("step");
    else next.set("step", id);
    setParams(next, { replace: true });
  };
  return (
    <div className="grid gap-4 tablet:gap-6" data-testid="run-detail">
      <Summary data={data} step={selected} />
      {data.chain.length > 1 && <ChainSteps data={data} selected={selected.run.run_id} onSelect={select} />}
      <StepBody key={selected.run.run_id} step={selected} />
      <RunLog data={data} />
    </div>
  );
}
