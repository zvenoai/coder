import { useState } from "react";
import { Check, X, Loader2, ExternalLink } from "lucide-react";
import { cn, relativeTimeUnix } from "../lib/utils";
import type { Proposal } from "../types";

interface ProposalCardProps {
  proposal: Proposal;
  onSelectTask: (key: string) => void;
  onApprove: (id: string) => Promise<Response>;
  onReject: (id: string) => Promise<Response>;
}

const categoryColors: Record<string, string> = {
  tooling: "bg-violet-500/10 text-violet-400 border-violet-500/20",
  documentation: "bg-sky-500/10 text-sky-400 border-sky-500/20",
  process: "bg-teal-500/10 text-teal-400 border-teal-500/20",
  testing: "bg-amber-500/10 text-amber-400 border-amber-500/20",
  infrastructure: "bg-rose-500/10 text-rose-400 border-rose-500/20",
};

const categoryBorderColors: Record<string, string> = {
  tooling: "border-l-violet-500",
  documentation: "border-l-sky-500",
  process: "border-l-teal-500",
  testing: "border-l-amber-500",
  infrastructure: "border-l-rose-500",
};

export default function ProposalCard({
  proposal,
  onSelectTask,
  onApprove,
  onReject,
}: ProposalCardProps) {
  const [approving, setApproving] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const handleApprove = async () => {
    if (approving || rejecting) return;
    setApproving(true);
    setActionError(null);
    try {
      const res = await onApprove(proposal.id);
      if (!res.ok) setActionError("Failed to approve");
    } catch {
      setActionError("Network error");
    } finally {
      setApproving(false);
    }
  };

  const handleReject = async () => {
    if (approving || rejecting) return;
    setRejecting(true);
    setActionError(null);
    try {
      const res = await onReject(proposal.id);
      if (!res.ok) setActionError("Failed to reject");
    } catch {
      setActionError("Network error");
    } finally {
      setRejecting(false);
    }
  };

  const borderClass = categoryBorderColors[proposal.category] ?? "border-l-violet-500";
  const badgeClass = categoryColors[proposal.category] ?? categoryColors.tooling;

  return (
    <div
      className={cn(
        "rounded-lg border border-border bg-card p-3 border-l-4",
        borderClass,
      )}
    >
      {/* Top row: source task + badges */}
      <div className="flex items-center justify-between mb-1.5">
        <button
          onClick={() => onSelectTask(proposal.source_task_key)}
          className="text-xs font-mono text-muted-foreground hover:text-foreground transition-colors flex items-center gap-1"
        >
          {proposal.source_task_key}
          <ExternalLink className="w-3 h-3" />
        </button>
        <div className="flex items-center gap-1.5">
          <span
            className={cn(
              "text-[10px] font-medium px-1.5 py-0.5 rounded border",
              badgeClass,
            )}
          >
            {proposal.category}
          </span>
          <span className="text-[10px] font-medium px-1.5 py-0.5 rounded bg-muted text-muted-foreground">
            {proposal.component}
          </span>
        </div>
      </div>

      {/* Summary */}
      <p className="text-sm text-foreground mb-2 line-clamp-2">{proposal.summary}</p>

      {/* Bottom row: time + actions */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-[10px] text-muted-foreground">
            {relativeTimeUnix(proposal.created_at)}
          </span>
          {actionError && (
            <span className="text-[10px] text-destructive">{actionError}</span>
          )}
        </div>

        {proposal.status === "pending" && (
          <div className="flex items-center gap-1.5">
            <button
              onClick={handleReject}
              disabled={approving || rejecting}
              className={cn(
                "inline-flex items-center gap-1 px-2 py-1 rounded text-xs font-medium transition-colors",
                "text-muted-foreground hover:text-foreground hover:bg-accent border border-border",
                (approving || rejecting) && "opacity-50 cursor-not-allowed",
              )}
            >
              {rejecting ? (
                <Loader2 className="w-3 h-3 animate-spin" />
              ) : (
                <X className="w-3 h-3" />
              )}
              Reject
            </button>
            <button
              onClick={handleApprove}
              disabled={approving || rejecting}
              className={cn(
                "inline-flex items-center gap-1 px-2 py-1 rounded text-xs font-medium transition-colors",
                "bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 hover:bg-emerald-500/20",
                (approving || rejecting) && "opacity-50 cursor-not-allowed",
              )}
            >
              {approving ? (
                <Loader2 className="w-3 h-3 animate-spin" />
              ) : (
                <Check className="w-3 h-3" />
              )}
              Approve
            </button>
          </div>
        )}

        {proposal.status === "approved" && proposal.tracker_issue_key && (
          <a
            href={`https://tracker.yandex.ru/${proposal.tracker_issue_key}`}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1 text-xs text-emerald-400 hover:text-emerald-300 transition-colors"
          >
            {proposal.tracker_issue_key}
            <ExternalLink className="w-3 h-3" />
          </a>
        )}

        {proposal.status === "rejected" && (
          <span className="text-xs text-muted-foreground/60">Rejected</span>
        )}
      </div>
    </div>
  );
}
