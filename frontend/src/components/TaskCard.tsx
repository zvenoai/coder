import { useState, useCallback } from "react";
import {
  Circle,
  GitPullRequest,
  CheckCircle2,
  XCircle,
  GitMerge,
  HelpCircle,
  ExternalLink,
  DollarSign,
  Clock,
  MessageSquare,
  AlertTriangle,
  Wrench,
  PauseCircle,
  SkipForward,
  Eye,
} from "lucide-react";
import { cn, relativeTime } from "../lib/utils";
import type { WsEvent } from "../types";

interface TaskCardProps {
  taskKey: string;
  taskEvents: WsEvent[];
  selected: boolean;
  onSelect: () => void;
}

export type TaskPhase = "running" | "review" | "completed" | "failed" | "merged" | "needs_info" | "deferred" | "skipped";

export type TaskSubState = "idle" | "fixing_reviews" | "fixing_ci" | "fixing_merge" | "reviewing";

export function derivePhase(events: WsEvent[]): TaskPhase {
  // Find the last task_started — only consider events from the
  // current run (handles restart after skip/failure).
  let start = 0;
  for (let i = events.length - 1; i >= 0; i--) {
    if (events[i].type === "task_started") { start = i; break; }
  }
  for (let i = events.length - 1; i >= start; i--) {
    const e = events[i];
    if (e.type === "pr_merged") return "merged";
    if (e.type === "task_completed" || e.type === "epic_completed") return "completed";
    if (e.type === "task_failed") return "failed";
    if (e.type === "task_skipped") return "skipped";
    if (e.type === "task_deferred") return "deferred";
    if (e.type === "task_unblocked") return "running";
    if (e.type === "needs_info") return "needs_info";
    if (e.type === "needs_info_response") return "running";
    if (e.type === "pr_tracked" || e.type === "review_sent") return "review";
  }
  return "running";
}

/** Detect if the agent is currently fixing reviews or CI within a review phase. */
export function deriveSubState(events: WsEvent[]): TaskSubState {
  // Walk backwards: find the latest actionable event
  for (let i = events.length - 1; i >= 0; i--) {
    const t = events[i].type;
    // Agent finished a round of work — back to idle
    if (t === "agent_result") return "idle";
    // Agent is producing output after a review_sent / pipeline_failed
    if (t === "agent_output") continue;
    if (t === "pr_review_started") return "reviewing";
    if (t === "pr_review_completed") return "idle";
    if (t === "merge_conflict") return "fixing_merge";
    if (t === "pipeline_failed") return "fixing_ci";
    if (t === "review_sent") return "fixing_reviews";
    // Any other terminal event — idle
    if (t === "pr_tracked" || t === "pr_merged" || t === "task_completed" || t === "epic_completed" || t === "task_failed" || t === "task_skipped") return "idle";
  }
  return "idle";
}

const TERMINAL_TYPES = new Set(["task_completed", "task_failed", "pr_tracked", "task_skipped", "epic_completed"]);

export function extractCostDuration(taskEvents: WsEvent[]): {
  cost: number | undefined;
  durationMs: number | undefined;
} {
  // Find indices of most recent terminal and agent_result events
  let lastTerminalIdx = -1;
  let lastAgentResultIdx = -1;

  for (let i = taskEvents.length - 1; i >= 0; i--) {
    const e = taskEvents[i];
    if (lastTerminalIdx === -1 && TERMINAL_TYPES.has(e.type)) {
      lastTerminalIdx = i;
    }
    if (lastAgentResultIdx === -1 && e.type === "agent_result") {
      lastAgentResultIdx = i;
    }
    if (lastTerminalIdx !== -1 && lastAgentResultIdx !== -1) {
      break; // Found both, stop searching
    }
  }

  // If agent_result is more recent than terminal event, use it (resumed/retry run)
  if (lastAgentResultIdx > lastTerminalIdx) {
    const e = taskEvents[lastAgentResultIdx];
    return {
      cost: e.data?.cost as number | undefined,
      durationMs: e.data?.duration_ms as number | undefined,
    };
  }

  // Otherwise use terminal event (persisted, survives restart)
  if (lastTerminalIdx !== -1) {
    const e = taskEvents[lastTerminalIdx];
    const cost = e.data?.cost as number | undefined;
    const duration = e.data?.duration as number | undefined;
    return {
      cost,
      durationMs: duration != null ? duration * 1000 : undefined,
    };
  }

  // Fallback to agent_result if no terminal event
  if (lastAgentResultIdx !== -1) {
    const e = taskEvents[lastAgentResultIdx];
    return {
      cost: e.data?.cost as number | undefined,
      durationMs: e.data?.duration_ms as number | undefined,
    };
  }

  return { cost: undefined, durationMs: undefined };
}

const phaseConfig: Record<TaskPhase, {
  label: string;
  color: string;
  bg: string;
  border: string;
  Icon: typeof Circle;
}> = {
  running:    { label: "Running",    color: "text-blue-400",    bg: "bg-blue-500/10",    border: "border-l-blue-500",    Icon: Circle },
  review:     { label: "In Review",  color: "text-yellow-400",  bg: "bg-yellow-500/10",  border: "border-l-yellow-500",  Icon: GitPullRequest },
  completed:  { label: "Completed",  color: "text-emerald-400", bg: "bg-emerald-500/10", border: "border-l-emerald-500", Icon: CheckCircle2 },
  failed:     { label: "Failed",     color: "text-red-400",     bg: "bg-red-500/10",     border: "border-l-red-500",     Icon: XCircle },
  merged:     { label: "Merged",     color: "text-purple-400",  bg: "bg-purple-500/10",  border: "border-l-purple-500",  Icon: GitMerge },
  needs_info: { label: "Needs Info", color: "text-amber-400",   bg: "bg-amber-500/10",   border: "border-l-amber-500",   Icon: HelpCircle },
  deferred:   { label: "Deferred",   color: "text-slate-400",   bg: "bg-slate-500/10",   border: "border-l-slate-500",   Icon: PauseCircle },
  skipped:    { label: "Skipped",    color: "text-gray-400",    bg: "bg-gray-500/10",    border: "border-l-gray-500",    Icon: SkipForward },
};

type CostMode = "last_run" | "total";

export default function TaskCard({ taskKey, taskEvents, selected, onSelect }: TaskCardProps) {
  const phase = derivePhase(taskEvents);
  const subState = deriveSubState(taskEvents);
  const config = phaseConfig[phase];
  const PhaseIcon = config.Icon;

  const { cost, durationMs } = extractCostDuration(taskEvents);

  const [costMode, setCostMode] = useState<CostMode>("last_run");
  const [totalCost, setTotalCost] = useState<number | null>(null);
  const [totalRunCount, setTotalRunCount] = useState<number>(0);
  const [totalCostLoading, setTotalCostLoading] = useState(false);

  const handleCostClick = useCallback(
    (e: { stopPropagation(): void }) => {
      e.stopPropagation();
      if (costMode === "last_run") {
        if (totalCost !== null) {
          setCostMode("total");
          return;
        }
        if (totalCostLoading) return;
        setTotalCostLoading(true);
        fetch(`/api/tasks/${encodeURIComponent(taskKey)}/cost-summary`)
          .then((r) => (r.ok ? r.json() : null))
          .then((data) => {
            if (data && typeof data.total_cost_usd === "number") {
              setTotalCost(data.total_cost_usd);
              setTotalRunCount(
                typeof data.run_count === "number" ? data.run_count : 0,
              );
              setCostMode("total");
            }
          })
          .catch(() => {})
          .finally(() => setTotalCostLoading(false));
      } else {
        setCostMode("last_run");
      }
    },
    [taskKey, costMode, totalCost, totalCostLoading],
  );

  // Single pass for all event-derived values
  let prUrl: string | undefined;
  let summary: string | undefined;
  let blockers: string[] | undefined;
  let outputCount = 0;
  let reviewsSentCount = 0;
  let pipelineFailCount = 0;
  for (let i = taskEvents.length - 1; i >= 0; i--) {
    const e = taskEvents[i];
    if (e.type === "pr_tracked" && !prUrl) prUrl = e.data?.pr_url as string | undefined;
    if (e.type === "task_started" && !summary) summary = e.data?.summary as string | undefined;
    if (e.type === "task_deferred" && !blockers) blockers = e.data?.blockers as string[] | undefined;
    if (e.type === "agent_output") outputCount++;
    if (e.type === "review_sent") reviewsSentCount++;
    if (e.type === "pipeline_failed") pipelineFailCount++;
  }

  const lastEventTs = taskEvents.length > 0 ? taskEvents[taskEvents.length - 1].ts : 0;

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onSelect}
      onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onSelect(); } }}
      className={cn(
        "group relative bg-card rounded-lg border border-l-4 cursor-pointer transition-all duration-150",
        config.border,
        selected
          ? "ring-2 ring-ring border-border"
          : "border-border hover:border-muted-foreground/30",
      )}
    >
      <div className="p-4">
        <div className="flex items-center justify-between mb-2">
          <a
            href={`https://tracker.yandex.ru/${taskKey}`}
            target="_blank"
            rel="noopener noreferrer"
            onClick={(e) => e.stopPropagation()}
            className="font-mono font-semibold text-sm text-card-foreground hover:text-blue-400 transition-colors inline-flex items-center gap-1"
          >
            {taskKey}
            <ExternalLink className="w-3 h-3 opacity-0 group-hover:opacity-60 transition-opacity" />
          </a>
          <div className="flex items-center gap-1.5">
            {/* Sub-state badge: agent actively fixing */}
            {subState === "fixing_reviews" && (
              <span className="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-full font-medium bg-blue-500/10 text-blue-400 animate-pulse">
                <Wrench className="w-2.5 h-2.5" />
                Fixing reviews
              </span>
            )}
            {subState === "fixing_ci" && (
              <span className="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-full font-medium bg-orange-500/10 text-orange-400 animate-pulse">
                <Wrench className="w-2.5 h-2.5" />
                Fixing CI
              </span>
            )}
            {subState === "fixing_merge" && (
              <span className="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-full font-medium bg-red-500/10 text-red-400 animate-pulse">
                <Wrench className="w-2.5 h-2.5" />
                Fixing merge
              </span>
            )}
            {subState === "reviewing" && (
              <span className="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-full font-medium bg-cyan-500/10 text-cyan-400 animate-pulse">
                <Eye className="w-2.5 h-2.5" />
                Reviewing
              </span>
            )}
            {/* Main phase badge */}
            <span className={cn(
              "inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full font-medium",
              config.bg,
              config.color,
            )}>
              <PhaseIcon className="w-3 h-3" />
              {config.label}
            </span>
          </div>
        </div>

        {summary && (
          <p className="text-sm text-muted-foreground mb-3 line-clamp-2 leading-relaxed">
            {summary}
          </p>
        )}

        {phase === "deferred" && blockers && blockers.length > 0 && (
          <p className="text-xs text-slate-400 mb-3">
            Blocked by: {blockers.join(", ")}
          </p>
        )}

        <div className="flex items-center justify-between text-xs text-muted-foreground">
          <div className="flex items-center gap-3">
            {prUrl && (
              <a
                href={prUrl}
                target="_blank"
                rel="noopener noreferrer"
                onClick={(e) => e.stopPropagation()}
                className="inline-flex items-center gap-1 text-blue-400 hover:text-blue-300 transition-colors"
              >
                <ExternalLink className="w-3 h-3" />
                PR
              </a>
            )}
            {cost != null && (
              <span
                role="button"
                tabIndex={0}
                onClick={handleCostClick}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    handleCostClick(e);
                  }
                }}
                title={
                  costMode === "last_run"
                    ? "Click to show total cost (all runs)"
                    : "Click to show last run cost"
                }
                className="inline-flex items-center gap-1 cursor-pointer hover:text-foreground transition-colors rounded px-0.5 -mx-0.5"
              >
                <DollarSign className="w-3 h-3" />
                {totalCostLoading ? (
                  "..."
                ) : costMode === "total" && totalCost !== null ? (
                  <span title={`Total across ${totalRunCount} run(s)`}>
                    {totalCost.toFixed(2)}
                    <span className="text-[10px] opacity-70 ml-0.5">all</span>
                  </span>
                ) : (
                  cost.toFixed(2)
                )}
              </span>
            )}
            {durationMs != null && (
              <span className="inline-flex items-center gap-1">
                <Clock className="w-3 h-3" />
                {Math.round(durationMs / 1000)}s
              </span>
            )}
            <span className="inline-flex items-center gap-1">
              <MessageSquare className="w-3 h-3" />
              {outputCount}
            </span>
            {reviewsSentCount > 0 && (
              <span className="inline-flex items-center gap-1 text-blue-400" title="Review rounds sent to agent">
                <GitPullRequest className="w-3 h-3" />
                {reviewsSentCount}
              </span>
            )}
            {pipelineFailCount > 0 && (
              <span className="inline-flex items-center gap-1 text-orange-400" title="CI pipeline failures sent to agent">
                <AlertTriangle className="w-3 h-3" />
                {pipelineFailCount}
              </span>
            )}
          </div>

          {lastEventTs > 0 && (
            <span className="text-muted-foreground/60 tabular-nums">
              {relativeTime(lastEventTs * 1000)}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
