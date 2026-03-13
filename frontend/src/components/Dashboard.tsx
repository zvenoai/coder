import { useEffect, useMemo, useState } from "react";
import { Inbox, Loader2 } from "lucide-react";
import { cn } from "../lib/utils";
import type { OrchestratorStatus, Proposal, WsEvent } from "../types";
import TaskCard, { derivePhase, type TaskPhase } from "./TaskCard";
import ProposalCard from "./ProposalCard";

interface DashboardProps {
  events: WsEvent[];
  onSelectTask: (key: string) => void;
  selectedTask: string | null;
  proposals: Proposal[];
  onApproveProposal: (id: string) => Promise<Response>;
  onRejectProposal: (id: string) => Promise<Response>;
}

type FilterTab = "all" | "running" | "review" | "needs_info" | "deferred" | "completed" | "failed" | "proposals";

const filterTabs: { key: FilterTab; label: string }[] = [
  { key: "all", label: "All" },
  { key: "running", label: "Running" },
  { key: "review", label: "In Review" },
  { key: "needs_info", label: "Needs Info" },
  { key: "deferred", label: "Deferred" },
  { key: "completed", label: "Completed" },
  { key: "failed", label: "Failed" },
  { key: "proposals", label: "Proposals" },
];

function matchesFilter(phase: TaskPhase, filter: FilterTab): boolean {
  if (filter === "all") return true;
  if (filter === "proposals") return false;
  if (filter === "completed") return phase === "completed" || phase === "merged" || phase === "skipped";
  if (filter === "deferred") return phase === "deferred";
  return phase === filter;
}

export default function Dashboard({ events, onSelectTask, selectedTask, proposals, onApproveProposal, onRejectProposal }: DashboardProps) {
  const [status, setStatus] = useState<OrchestratorStatus | null>(null);
  const [filter, setFilter] = useState<FilterTab>("all");
  const [editingMaxAgents, setEditingMaxAgents] = useState(false);
  const [draftMaxAgents, setDraftMaxAgents] = useState<number>(2);
  const [maxAgentsError, setMaxAgentsError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [fetchError, setFetchError] = useState(false);

  useEffect(() => {
    const fetchStatus = async () => {
      try {
        const res = await fetch("/api/status");
        if (res.ok) {
          setStatus(await res.json());
          setFetchError(false);
        } else {
          setFetchError(true);
        }
      } catch {
        setFetchError(true);
      } finally {
        setLoading(false);
      }
    };

    fetchStatus();
    const interval = setInterval(fetchStatus, 10000);
    return () => clearInterval(interval);
  }, []);

  // Pre-group events by task_key once (O(n) instead of O(n*m))
  const eventsByTask = useMemo(() => {
    const map = new Map<string, WsEvent[]>();
    for (const e of events) {
      if (e.task_key) {
        let arr = map.get(e.task_key);
        if (!arr) {
          arr = [];
          map.set(e.task_key, arr);
        }
        arr.push(e);
      }
    }
    return map;
  }, [events]);

  // Derive task keys, phases, counts — memoized to avoid recomputation on unrelated renders
  const { taskKeys, counts, filteredKeys } = useMemo(() => {
    const keys = new Set<string>();
    if (status) {
      for (const key of status.dispatched) keys.add(key);
      for (const key of Object.keys(status.tracked_prs)) keys.add(key);
      if (status.tracked_needs_info) {
        for (const key of Object.keys(status.tracked_needs_info)) keys.add(key);
      }
    }
    for (const key of eventsByTask.keys()) {
      keys.add(key);
    }

    const phaseMap = new Map<string, TaskPhase>();
    for (const key of keys) {
      phaseMap.set(key, derivePhase(eventsByTask.get(key) ?? []));
    }

    const c: Record<FilterTab, number> = {
      all: keys.size, running: 0, review: 0, needs_info: 0, deferred: 0, completed: 0, failed: 0, proposals: 0,
    };
    for (const phase of phaseMap.values()) {
      if (phase === "running") c.running++;
      if (phase === "review") c.review++;
      if (phase === "needs_info") c.needs_info++;
      if (phase === "deferred") c.deferred++;
      if (phase === "completed" || phase === "merged" || phase === "skipped") c.completed++;
      if (phase === "failed") c.failed++;
    }

    c.proposals = proposals.length;

    const filtered = [...keys]
      .filter((key) => matchesFilter(phaseMap.get(key) ?? "running", filter))
      .sort();

    return { taskKeys: keys, counts: c, filteredKeys: filtered };
  }, [status, eventsByTask, filter, proposals.length]);

  // Split proposals once for rendering
  const { pendingProposals, processedProposals } = useMemo(() => {
    const pending = proposals.filter((p) => p.status === "pending");
    const processed = proposals.filter((p) => p.status !== "pending");
    return { pendingProposals: pending, processedProposals: processed };
  }, [proposals]);

  return (
    <div className="p-4 sm:p-6">
      {/* Header */}
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          <h2 className="text-sm font-semibold text-foreground">Tasks</h2>
          <span className="text-xs font-mono text-muted-foreground bg-muted px-1.5 py-0.5 rounded-md">
            {taskKeys.size}
          </span>
        </div>
        {status && (
          <span className="text-xs text-muted-foreground font-mono flex items-center gap-1">
            {status.config.queue} / {status.config.tag} / max{" "}
            {editingMaxAgents ? (
              <span className="flex items-center gap-1">
                <input
                  type="number"
                  min={1}
                  aria-label="Max concurrent agents"
                  value={draftMaxAgents}
                  onChange={(e) => {
                    const n = parseInt(e.target.value, 10);
                    if (!Number.isNaN(n)) setDraftMaxAgents(n);
                  }}
                  className="w-12 px-1 py-0.5 rounded border border-border bg-background text-foreground text-xs font-mono"
                  autoFocus
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      e.currentTarget.blur();
                    } else if (e.key === "Escape") {
                      setDraftMaxAgents(status.config.max_agents);
                      setEditingMaxAgents(false);
                      setMaxAgentsError(null);
                    }
                  }}
                />
                <button
                  type="button"
                  onClick={async () => {
                    if (draftMaxAgents < 1) {
                      setMaxAgentsError("Min 1");
                      return;
                    }
                    setMaxAgentsError(null);
                    try {
                      const res = await fetch("/api/config", {
                        method: "PUT",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ max_agents: draftMaxAgents }),
                      });
                      if (res.ok) {
                        const data = await res.json();
                        setStatus((s) =>
                          s ? { ...s, config: { ...s.config, max_agents: data.max_agents } } : null,
                        );
                        setEditingMaxAgents(false);
                      } else {
                        const err = await res.json().catch(() => ({}));
                        setMaxAgentsError(err.error ?? "Failed");
                      }
                    } catch {
                      setMaxAgentsError("Request failed");
                    }
                  }}
                  className="text-xs px-1.5 py-0.5 rounded bg-accent text-accent-foreground hover:opacity-90"
                >
                  Save
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setDraftMaxAgents(status.config.max_agents);
                    setEditingMaxAgents(false);
                    setMaxAgentsError(null);
                  }}
                  className="text-xs px-1.5 py-0.5 rounded hover:bg-muted text-muted-foreground"
                >
                  Cancel
                </button>
                {maxAgentsError && (
                  <span className="text-[10px] text-destructive">{maxAgentsError}</span>
                )}
              </span>
            ) : (
              <button
                type="button"
                onClick={() => {
                  setDraftMaxAgents(status.config.max_agents);
                  setEditingMaxAgents(true);
                  setMaxAgentsError(null);
                }}
                className="hover:underline hover:text-foreground"
                title="Change max concurrent agents"
              >
                {status.config.max_agents}
              </button>
            )}
          </span>
        )}
      </div>

      {/* Filter tabs */}
      <div role="tablist" aria-label="Filter tasks" className="flex items-center gap-1 mb-5 overflow-x-auto pb-1">
        {filterTabs.map((tab) => (
          <button
            key={tab.key}
            role="tab"
            aria-selected={filter === tab.key}
            onClick={() => setFilter(tab.key)}
            className={cn(
              "inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium transition-colors whitespace-nowrap",
              filter === tab.key
                ? "bg-accent text-accent-foreground"
                : "text-muted-foreground hover:text-foreground hover:bg-accent/50",
            )}
          >
            {tab.label}
            {counts[tab.key] > 0 && (
              <span className={cn(
                "text-[10px] font-mono px-1 py-px rounded",
                filter === tab.key ? "bg-foreground/10" : "bg-muted",
              )}>
                {counts[tab.key]}
              </span>
            )}
          </button>
        ))}
      </div>

      {/* Fetch error banner */}
      {fetchError && (
        <div className="mb-4 px-3 py-2 rounded-md bg-destructive/10 text-destructive text-xs">
          Failed to load status from server. Retrying...
        </div>
      )}

      {/* Task / Proposal grid */}
      {filter === "proposals" ? (
        proposals.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-20 text-muted-foreground">
            <Inbox className="w-10 h-10 mb-3 opacity-40" />
            <p className="text-sm font-medium mb-1">No proposals</p>
            <p className="text-xs text-muted-foreground/60">
              Agents will propose improvements as they work
            </p>
          </div>
        ) : (
          <div className="grid gap-3 grid-cols-1 md:grid-cols-2 xl:grid-cols-3">
            {pendingProposals.map((p) => (
              <ProposalCard
                key={p.id}
                proposal={p}
                onSelectTask={onSelectTask}
                onApprove={onApproveProposal}
                onReject={onRejectProposal}
              />
            ))}
            {processedProposals.map((p) => (
              <ProposalCard
                key={p.id}
                proposal={p}
                onSelectTask={onSelectTask}
                onApprove={onApproveProposal}
                onReject={onRejectProposal}
              />
            ))}
          </div>
        )
      ) : loading && taskKeys.size === 0 ? (
        <div className="flex flex-col items-center justify-center py-20 text-muted-foreground">
          <Loader2 className="w-8 h-8 mb-3 animate-spin opacity-40" />
          <p className="text-sm font-medium">Loading tasks...</p>
        </div>
      ) : filteredKeys.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-20 text-muted-foreground">
          <Inbox className="w-10 h-10 mb-3 opacity-40" />
          <p className="text-sm font-medium mb-1">
            {filter === "all" ? "No tasks yet" : `No ${filterTabs.find((t) => t.key === filter)?.label.toLowerCase()} tasks`}
          </p>
          <p className="text-xs text-muted-foreground/60">
            {filter === "all"
              ? "Waiting for tasks with tag ai-task..."
              : "Try a different filter"}
          </p>
        </div>
      ) : (
        <div className="grid gap-3 grid-cols-1 md:grid-cols-2 xl:grid-cols-3">
          {filteredKeys.map((key) => (
            <TaskCard
              key={key}
              taskKey={key}
              taskEvents={eventsByTask.get(key) ?? []}
              selected={selectedTask === key}
              onSelect={() => onSelectTask(key)}
            />
          ))}
        </div>
      )}
    </div>
  );
}
