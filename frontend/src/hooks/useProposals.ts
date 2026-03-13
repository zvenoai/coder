import { useCallback, useEffect, useRef, useState } from "react";
import type { Proposal, WsEvent } from "../types";

export function useProposals(events: WsEvent[]) {
  const [proposals, setProposals] = useState<Proposal[]>([]);
  const lastSeenEventCount = useRef(0);

  const fetchProposals = useCallback(async () => {
    try {
      const res = await fetch("/api/proposals");
      if (res.ok) {
        setProposals(await res.json());
      }
    } catch {
      // ignore
    }
  }, []);

  // Initial load + periodic refresh
  useEffect(() => {
    fetchProposals();
    const interval = setInterval(fetchProposals, 15000);
    return () => clearInterval(interval);
  }, [fetchProposals]);

  // Refetch only on genuinely new proposal-related events
  useEffect(() => {
    const proposalEvents = events.filter(
      (e) =>
        e.type === "task_proposed" ||
        e.type === "proposal_approved" ||
        e.type === "proposal_rejected",
    );
    if (proposalEvents.length > lastSeenEventCount.current) {
      lastSeenEventCount.current = proposalEvents.length;
      fetchProposals();
    }
  }, [events, fetchProposals]);

  const pendingCount = proposals.filter((p) => p.status === "pending").length;

  const approve = useCallback(
    async (id: string) => {
      const res = await fetch(`/api/proposals/${id}/approve`, { method: "POST" });
      if (res.ok) {
        await fetchProposals();
      }
      return res;
    },
    [fetchProposals],
  );

  const reject = useCallback(
    async (id: string) => {
      const res = await fetch(`/api/proposals/${id}/reject`, { method: "POST" });
      if (res.ok) {
        await fetchProposals();
      }
      return res;
    },
    [fetchProposals],
  );

  return { proposals, pendingCount, approve, reject };
}
