export interface WsEvent {
  type: string;
  task_key: string;
  data: Record<string, unknown>;
  ts: number;
}

export interface OrchestratorStatus {
  dispatched: string[];
  active_tasks: string[];
  tracked_prs: Record<
    string,
    { pr_url: string; issue_key: string; last_check: number }
  >;
  tracked_needs_info: Record<
    string,
    { issue_key: string; last_check: number; last_seen_comment_id: number }
  >;
  proposals: Record<
    string,
    Proposal
  >;
  supervisor: {
    enabled: boolean;
    running: boolean;
    last_run_at: number | null;
    queue_size: number;
  } | null;
  config: {
    queue: string;
    tag: string;
    max_agents: number;
  };
}

export interface Proposal {
  id: string;
  source_task_key: string;
  summary: string;
  description: string;
  component: string;
  category: string;
  status: "pending" | "approved" | "rejected";
  created_at: number;
  tracker_issue_key: string | null;
}

export type ConnectionStatus = "connecting" | "connected" | "disconnected";

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  timestamp: number;
}

export interface ChatSessionInfo {
  session_id: string;
  created_at: number;
  message_count: number;
  generating: boolean;
}

export interface ChatProgress {
  type: "thinking" | "tool_use" | "idle";
  detail?: string;
}

export type ChannelId = "chat" | "tasks" | "heartbeat";

// Stats API types

export interface StatsSummary {
  total_tasks: number;
  success_count: number;
  success_rate: number;
  total_cost: number;
  avg_duration: number;
  avg_cost: number;
  days: number;
}

export interface CostEntry {
  group: string;
  total_cost: number;
  count: number;
}

export interface TaskRunEntry {
  task_key: string;
  model: string;
  cost_usd: number;
  duration_seconds: number;
  success: boolean;
  error_category: string | null;
  pr_url: string | null;
  needs_info: boolean;
  resumed: boolean;
  started_at: number;
  finished_at: number;
}

export interface ErrorEntry {
  category: string;
  count: number;
  retryable_count: number;
}
