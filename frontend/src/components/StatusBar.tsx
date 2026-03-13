import { useMemo } from "react";
import { Wifi, WifiOff, Loader2, MessageCircle, BarChart3 } from "lucide-react";
import { cn } from "../lib/utils";
import type { ConnectionStatus, WsEvent } from "../types";
import logoWhite from "../assets/logo_white.svg";

interface StatusBarProps {
  connectionStatus: ConnectionStatus;
  events: WsEvent[];
  pendingProposalCount?: number;
  supervisorRunning?: boolean;
  onChatToggle?: () => void;
  chatOpen?: boolean;
  onStatsToggle?: () => void;
  statsOpen?: boolean;
}

export default function StatusBar({ connectionStatus, events, pendingProposalCount = 0, supervisorRunning, onChatToggle, chatOpen, onStatsToggle, statsOpen }: StatusBarProps) {
  const { activeTasks, completedTasks, reviewTasks, needsInfoTasks, fixingTasks, deferredTasks } = useMemo(() => {
    const active = new Set<string>();
    const completed = new Set<string>();
    const review = new Set<string>();
    const needsInfo = new Set<string>();
    const fixing = new Set<string>();
    const deferred = new Set<string>();

    for (const e of events) {
      if (e.type === "task_started") {
        active.add(e.task_key);
        review.delete(e.task_key);
        needsInfo.delete(e.task_key);
        fixing.delete(e.task_key);
        deferred.delete(e.task_key);
      }
      if (e.type === "pr_tracked") {
        active.delete(e.task_key);
        fixing.delete(e.task_key);
        review.add(e.task_key);
      }
      if (e.type === "review_sent" || e.type === "pipeline_failed") {
        active.delete(e.task_key);
        review.delete(e.task_key);
        fixing.add(e.task_key);
      }
      if (e.type === "merge_conflict") {
        review.delete(e.task_key);
        fixing.add(e.task_key);
      }
      if (e.type === "agent_result") {
        if (fixing.has(e.task_key)) {
          fixing.delete(e.task_key);
          review.add(e.task_key);
        }
      }
      if (e.type === "needs_info") {
        active.delete(e.task_key);
        needsInfo.add(e.task_key);
        fixing.delete(e.task_key);
      }
      if (e.type === "needs_info_response") {
        needsInfo.delete(e.task_key);
        active.add(e.task_key);
      }
      if (e.type === "task_deferred") {
        active.delete(e.task_key);
        deferred.add(e.task_key);
      }
      if (e.type === "task_unblocked") {
        deferred.delete(e.task_key);
      }
      if (e.type === "task_skipped") {
        active.delete(e.task_key);
        deferred.delete(e.task_key);
        completed.add(e.task_key);
      }
      if (e.type === "task_completed" || e.type === "pr_merged" || e.type === "epic_completed") {
        active.delete(e.task_key);
        review.delete(e.task_key);
        needsInfo.delete(e.task_key);
        fixing.delete(e.task_key);
        deferred.delete(e.task_key);
        completed.add(e.task_key);
      }
      if (e.type === "task_failed") {
        active.delete(e.task_key);
        fixing.delete(e.task_key);
        review.delete(e.task_key);
        needsInfo.delete(e.task_key);
        deferred.delete(e.task_key);
      }
    }

    return { activeTasks: active, completedTasks: completed, reviewTasks: review, needsInfoTasks: needsInfo, fixingTasks: fixing, deferredTasks: deferred };
  }, [events]);

  return (
    <header className="h-14 bg-background border-b border-border px-4 flex items-center justify-between shrink-0">
      <div className="flex items-center gap-3">
        <img
          src={logoWhite}
          alt="ZvenoAI"
          className="h-6"
        />
        <div className="w-px h-4 bg-border" />
        <span className="text-xs text-muted-foreground font-medium">Orchestrator</span>
      </div>

      <div className="flex items-center gap-2">
        <MetricBadge label="Active" value={activeTasks.size} pulse={activeTasks.size > 0} variant="blue" />
        <MetricBadge label="Fixing" value={fixingTasks.size} pulse={fixingTasks.size > 0} variant="orange" />
        <MetricBadge label="Review" value={reviewTasks.size} variant="yellow" />
        <MetricBadge label="Needs Info" value={needsInfoTasks.size} variant="amber" />
        {deferredTasks.size > 0 && (
          <MetricBadge label="Deferred" value={deferredTasks.size} variant="slate" />
        )}
        {pendingProposalCount > 0 && (
          <MetricBadge label="Proposals" value={pendingProposalCount} variant="violet" />
        )}
        {supervisorRunning && (
          <MetricBadge label="Supervisor" value={1} pulse variant="green" />
        )}
        <MetricBadge label="Completed" value={completedTasks.size} variant="green" />

        {onStatsToggle && (
          <>
            <div className="w-px h-4 bg-border ml-1" />
            <button
              onClick={onStatsToggle}
              className={cn(
                "flex items-center gap-1.5 px-2 py-1 rounded-md text-xs transition-colors",
                statsOpen
                  ? "bg-primary/10 text-primary"
                  : "text-muted-foreground hover:text-foreground hover:bg-accent",
              )}
            >
              <BarChart3 className="w-3.5 h-3.5" />
              <span>Statistics</span>
            </button>
          </>
        )}

        {onChatToggle && (
          <>
            <div className="w-px h-4 bg-border ml-1" />
            <button
              onClick={onChatToggle}
              className={cn(
                "flex items-center gap-1.5 px-2 py-1 rounded-md text-xs transition-colors",
                chatOpen
                  ? "bg-primary/10 text-primary"
                  : "text-muted-foreground hover:text-foreground hover:bg-accent",
              )}
            >
              <MessageCircle className="w-3.5 h-3.5" />
              <span>Супервизор</span>
            </button>
          </>
        )}

        <div className="w-px h-4 bg-border ml-1" />

        <div className={cn(
          "flex items-center gap-1.5 px-2 py-1 rounded-md text-xs",
          connectionStatus === "connected" && "text-emerald-400",
          connectionStatus === "connecting" && "text-yellow-400",
          connectionStatus === "disconnected" && "text-red-400",
        )}>
          {connectionStatus === "connected" ? (
            <Wifi className="w-3.5 h-3.5" />
          ) : connectionStatus === "connecting" ? (
            <Loader2 className="w-3.5 h-3.5 animate-spin" />
          ) : (
            <WifiOff className="w-3.5 h-3.5" />
          )}
          <span className="hidden sm:inline">{connectionStatus}</span>
        </div>
      </div>
    </header>
  );
}

function MetricBadge({
  label,
  value,
  pulse,
  variant,
}: {
  label: string;
  value: number;
  pulse?: boolean;
  variant: "blue" | "orange" | "yellow" | "amber" | "green" | "violet" | "slate";
}) {
  const colors = {
    blue: "text-blue-400",
    orange: "text-orange-400",
    yellow: "text-yellow-400",
    amber: "text-amber-400",
    green: "text-emerald-400",
    violet: "text-violet-400",
    slate: "text-slate-400",
  };

  const dotColors = {
    blue: "bg-blue-400",
    orange: "bg-orange-400",
    yellow: "bg-yellow-400",
    amber: "bg-amber-400",
    green: "bg-emerald-400",
    violet: "bg-violet-400",
    slate: "bg-slate-400",
  };

  return (
    <div className="flex items-center gap-1.5 px-2 py-1 rounded-md text-xs text-muted-foreground">
      {pulse && (
        <span className="relative flex h-2 w-2">
          <span className={cn("absolute inline-flex h-full w-full rounded-full opacity-75 animate-ping", dotColors[variant])} />
          <span className={cn("relative inline-flex rounded-full h-2 w-2", dotColors[variant])} />
        </span>
      )}
      <span>{label}</span>
      <span className={cn("font-mono font-medium", colors[variant])}>{value}</span>
    </div>
  );
}
