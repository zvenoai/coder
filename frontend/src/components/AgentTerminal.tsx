import { useEffect, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import { X, ArrowDown, Send } from "lucide-react";
import { cn } from "../lib/utils";
import { useTaskStream } from "../hooks/useTaskStream";

interface AgentTerminalProps {
  taskKey: string;
  onClose: () => void;
}

export default function AgentTerminal({ taskKey, onClose }: AgentTerminalProps) {
  const termRef = useRef<HTMLDivElement>(null);
  const terminalRef = useRef<Terminal | null>(null);
  const fitAddonRef = useRef<FitAddon | null>(null);
  const writtenRef = useRef(0);
  const [autoScroll, setAutoScroll] = useState(true);
  const [messageText, setMessageText] = useState("");
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);
  const { lines, status } = useTaskStream(taskKey);

  // Initialize terminal
  useEffect(() => {
    if (!termRef.current) return;

    const terminal = new Terminal({
      theme: {
        background: "#09090b",
        foreground: "#fafafa",
        cursor: "#a1a1aa",
        selectionBackground: "#27272a",
        black: "#09090b",
        red: "#ef4444",
        green: "#22c55e",
        yellow: "#eab308",
        blue: "#3b82f6",
        magenta: "#a855f7",
        cyan: "#06b6d4",
        white: "#fafafa",
      },
      fontSize: 13,
      fontFamily: "'JetBrains Mono', 'Fira Code', monospace",
      convertEol: true,
      scrollback: 5000,
      cursorBlink: false,
      disableStdin: true,
    });

    const fitAddon = new FitAddon();
    terminal.loadAddon(fitAddon);
    terminal.open(termRef.current);
    fitAddon.fit();

    terminalRef.current = terminal;
    fitAddonRef.current = fitAddon;
    writtenRef.current = 0;

    const onResize = () => fitAddon.fit();
    window.addEventListener("resize", onResize);

    terminal.onScroll(() => {
      const t = terminalRef.current;
      if (!t) return;
      const atBottom = t.buffer.active.viewportY >= t.buffer.active.baseY;
      setAutoScroll(atBottom);
    });

    return () => {
      window.removeEventListener("resize", onResize);
      terminal.dispose();
      terminalRef.current = null;
      fitAddonRef.current = null;
    };
  }, [taskKey]);

  // Write new lines to terminal
  useEffect(() => {
    const terminal = terminalRef.current;
    if (!terminal) return;

    for (let i = writtenRef.current; i < lines.length; i++) {
      const event = lines[i];
      if (event.type === "agent_output") {
        terminal.write(String(event.data.text ?? ""));
        terminal.write("\r\n");
      } else if (event.type === "agent_result") {
        const cost = event.data.cost as number | undefined;
        terminal.write(
          `\r\n\x1b[32m--- Result (cost: $${cost?.toFixed(2) ?? "?"}) ---\x1b[0m\r\n`,
        );
      } else if (event.type === "review_sent") {
        const count = event.data.thread_count ?? "?";
        const prUrl = event.data.pr_url ?? "";
        terminal.write(
          `\r\n\x1b[1;33m` +
          `╔══════════════════════════════════════════════╗\r\n` +
          `║  📝 REVIEW: ${count} unresolved conversation(s)    ║\r\n` +
          `║  PR: ${String(prUrl).slice(0, 40).padEnd(40)}║\r\n` +
          `║  Agent is fixing review comments...          ║\r\n` +
          `╚══════════════════════════════════════════════╝\x1b[0m\r\n\r\n`,
        );
      } else if (event.type === "pipeline_failed") {
        const count = event.data.check_count ?? "?";
        const checks = (event.data.checks as string[] | undefined) ?? [];
        terminal.write(
          `\r\n\x1b[1;31m` +
          `╔══════════════════════════════════════════════╗\r\n` +
          `║  🔴 CI FAILED: ${String(count)} check(s) failed${" ".repeat(Math.max(0, 14 - String(count).length))}║\r\n` +
          (checks.length > 0
            ? `║  ${checks.join(", ").slice(0, 43).padEnd(44)}║\r\n`
            : "") +
          `║  Agent is fixing pipeline failures...        ║\r\n` +
          `╚══════════════════════════════════════════════╝\x1b[0m\r\n\r\n`,
        );
      } else if (event.type === "user_message") {
        const text = String(event.data.text ?? "");
        terminal.write(
          `\r\n\x1b[1;36m` +
          `╔══════════════════════════════════════════════╗\r\n` +
          `║  USER MESSAGE                                ║\r\n` +
          `║  ${text.slice(0, 43).padEnd(44)}║\r\n` +
          `╚══════════════════════════════════════════════╝\x1b[0m\r\n\r\n`,
        );
      } else if (event.type === "task_proposed") {
        const summary = String(event.data.summary ?? "");
        const component = String(event.data.component ?? "");
        const category = String(event.data.category ?? "");
        terminal.write(
          `\r\n\x1b[1;35m` +
          `╔══════════════════════════════════════════════╗\r\n` +
          `║  IMPROVEMENT PROPOSED                        ║\r\n` +
          `║  ${summary.slice(0, 43).padEnd(44)}║\r\n` +
          `║  [${component}] [${category}]${" ".repeat(Math.max(0, 39 - component.length - category.length))}║\r\n` +
          `╚══════════════════════════════════════════════╝\x1b[0m\r\n\r\n`,
        );
      } else if (event.type === "model_selected") {
        const choice = String(event.data.model_choice ?? "");
        const model = String(event.data.model ?? "");
        terminal.write(
          `\r\n\x1b[1;36m[MODEL] ${choice.toUpperCase()} → ${model}\x1b[0m\r\n`,
        );
      } else if (event.type === "supervisor_started") {
        terminal.write(
          `\r\n\x1b[1;32m` +
          `╔══════════════════════════════════════════════╗\r\n` +
          `║  SUPERVISOR STARTED                          ║\r\n` +
          `║  Analyzing agent results...                  ║\r\n` +
          `╚══════════════════════════════════════════════╝\x1b[0m\r\n\r\n`,
        );
      } else if (event.type === "supervisor_completed") {
        terminal.write(
          `\r\n\x1b[1;32m` +
          `╔══════════════════════════════════════════════╗\r\n` +
          `║  SUPERVISOR COMPLETED                        ║\r\n` +
          `╚══════════════════════════════════════════════╝\x1b[0m\r\n\r\n`,
        );
      } else if (event.type === "supervisor_task_created") {
        const issueKey = String(event.data.issue_key ?? "");
        terminal.write(
          `\r\n\x1b[1;32m` +
          `╔══════════════════════════════════════════════╗\r\n` +
          `║  SUPERVISOR TASK CREATED                     ║\r\n` +
          `║  ${issueKey.slice(0, 43).padEnd(44)}║\r\n` +
          `╚══════════════════════════════════════════════╝\x1b[0m\r\n\r\n`,
        );
      } else if (event.type === "supervisor_failed") {
        const error = String(event.data.error ?? "");
        terminal.write(
          `\r\n\x1b[1;31m` +
          `╔══════════════════════════════════════════════╗\r\n` +
          `║  SUPERVISOR FAILED                           ║\r\n` +
          `║  ${error.slice(0, 43).padEnd(44)}║\r\n` +
          `╚══════════════════════════════════════════════╝\x1b[0m\r\n\r\n`,
        );
      } else if (event.type === "merge_conflict") {
        const prUrl = String(event.data.pr_url ?? "");
        terminal.write(
          `\r\n\x1b[1;31m` +
          `╔══════════════════════════════════════════════╗\r\n` +
          `║  MERGE CONFLICT                              ║\r\n` +
          `║  ${prUrl.slice(0, 43).padEnd(44)}║\r\n` +
          `║  Agent is resolving merge conflict...        ║\r\n` +
          `╚══════════════════════════════════════════════╝\x1b[0m\r\n\r\n`,
        );
      } else if (event.type === "compaction_triggered") {
        const cycle = event.data.cycle ?? "?";
        const tokens = event.data.tokens ?? "?";
        terminal.write(
          `\x1b[36m[COMPACTION] cycle ${cycle}, ${tokens} tokens\x1b[0m\r\n`,
        );
      } else if (event.type === "task_deferred") {
        const blockers = (event.data.blockers as string[] | undefined) ?? [];
        terminal.write(
          `\r\n\x1b[1;33m` +
          `╔══════════════════════════════════════════════╗\r\n` +
          `║  TASK DEFERRED                               ║\r\n` +
          `║  Blocked by: ${blockers.join(", ").slice(0, 32).padEnd(32)}║\r\n` +
          `╚══════════════════════════════════════════════╝\x1b[0m\r\n\r\n`,
        );
      } else if (event.type === "task_unblocked") {
        terminal.write(
          `\r\n\x1b[1;32m` +
          `╔══════════════════════════════════════════════╗\r\n` +
          `║  TASK UNBLOCKED                              ║\r\n` +
          `║  Dependencies resolved, resuming...          ║\r\n` +
          `╚══════════════════════════════════════════════╝\x1b[0m\r\n\r\n`,
        );
      } else if (event.type === "task_skipped") {
        const reason = String(event.data.reason ?? "");
        terminal.write(
          `\r\n\x1b[1;33m` +
          `╔══════════════════════════════════════════════╗\r\n` +
          `║  TASK SKIPPED                                ║\r\n` +
          `║  ${reason.slice(0, 43).padEnd(44)}║\r\n` +
          `╚══════════════════════════════════════════════╝\x1b[0m\r\n\r\n`,
        );
      } else if (event.type === "heartbeat") {
        const total = Number(event.data.total ?? 0);
        const healthy = Number(event.data.healthy ?? 0);
        const stuck = Number(event.data.stuck ?? 0);
        const longRunning = Number(event.data.long_running ?? 0);
        const staleReviews = Number(event.data.stale_reviews ?? 0);
        if (stuck > 0 || longRunning > 0 || staleReviews > 0) {
          terminal.write(
            `\x1b[90m[HEARTBEAT] ${total} agent(s), ${healthy} healthy\x1b[0m\r\n`,
          );
        }
      } else if (event.type === "pr_auto_merge_enabled") {
        const prUrl = String(event.data.pr_url ?? "");
        terminal.write(
          `\r\n\x1b[1;32m` +
          `\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\r\n` +
          `  AUTO-MERGE ENABLED\r\n` +
          `  ${prUrl.slice(0, 44)}\r\n` +
          `\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\x1b[0m\r\n\r\n`,
        );
      } else if (event.type === "pr_direct_merged") {
        const prUrl = String(event.data.pr_url ?? "");
        terminal.write(
          `\r\n\x1b[1;32m` +
          `\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\r\n` +
          `  DIRECT MERGE SUCCEEDED\r\n` +
          `  ${prUrl.slice(0, 44)}\r\n` +
          `\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\x1b[0m\r\n\r\n`,
        );
      } else if (event.type === "pr_auto_merge_failed") {
        const reason = String(event.data.reason ?? "Unknown");
        terminal.write(
          `\r\n\x1b[1;31m` +
          `\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\r\n` +
          `  AUTO-MERGE FAILED\r\n` +
          `  ${reason.slice(0, 44)}\r\n` +
          `\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\x1b[0m\r\n\r\n`,
        );
      } else if (event.type === "pr_review_started") {
        const prUrl = String(event.data.pr_url ?? "");
        terminal.write(
          `\x1b[36m[REVIEW] Pre-merge review started: ${prUrl.slice(0, 40)}\x1b[0m\r\n`,
        );
      } else if (event.type === "pr_review_completed") {
        const decision = String(event.data.decision ?? "");
        const summary = String(event.data.summary ?? "");
        const issueCount = Number(event.data.issue_count ?? 0);
        const color = decision === "approve" ? "32" : "31";
        terminal.write(
          `\r\n\x1b[1;${color}m` +
          `\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\r\n` +
          `  PRE-MERGE REVIEW: ${decision.toUpperCase()}\r\n` +
          `  ${summary.slice(0, 44)}\r\n` +
          (issueCount > 0 ? `  ${issueCount} issue(s) found\r\n` : "") +
          `\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\x1b[0m\r\n\r\n`,
        );
      } else if (
        event.type === "epic_detected" ||
        event.type === "epic_child_ready" ||
        event.type === "epic_awaiting_plan" ||
        event.type === "epic_completed" ||
        event.type === "epic_child_reset" ||
        event.type === "epic_needs_decomposition"
      ) {
        const key = String(
          event.data.child_key ?? event.data.summary ?? event.data.epic_summary ?? "",
        );
        terminal.write(
          `\x1b[35m[EPIC] ${event.type}: ${key}\x1b[0m\r\n`,
        );
      } else if (event.type === "agent_message_sent") {
        const target = String(event.data.target ?? "");
        terminal.write(
          `\x1b[36m[MSG] \u2192 ${target} (${event.data.message_id})\x1b[0m\r\n`,
        );
      } else if (event.type === "agent_message_replied") {
        const sender = String(event.data.sender ?? "");
        terminal.write(
          `\x1b[36m[MSG] \u2190 ${sender} (${event.data.message_id})\x1b[0m\r\n`,
        );
      } else if (event.type === "orchestrator_decision") {
        const ok = event.data.success ? "SUCCESS" : "FAIL";
        const pr = event.data.has_pr ? " +PR" : "";
        terminal.write(
          `\x1b[32m[DECISION] ${ok}${pr}\x1b[0m\r\n`,
        );
      } else if (event.type.startsWith("supervisor_chat_")) {
        // Silent — handled by SupervisorChat component
      } else {
        terminal.write(
          `\x1b[36m[${event.type}]\x1b[0m ${JSON.stringify(event.data)}\r\n`,
        );
      }
    }
    writtenRef.current = lines.length;

    if (autoScroll) {
      terminal.scrollToBottom();
    }
  }, [lines, autoScroll]);

  const scrollToBottom = () => {
    terminalRef.current?.scrollToBottom();
    setAutoScroll(true);
  };

  // Derive whether there is an active agent session from events
  const hasActiveSession = (() => {
    const terminalEvents = new Set([
      "task_completed", "task_failed", "pr_merged",
      "task_skipped", "task_deferred",
    ]);
    const activeEvents = new Set([
      "task_started", "agent_output", "pr_tracked",
      "needs_info", "review_sent", "pipeline_failed",
      "needs_info_response", "user_message",
      "merge_conflict", "task_unblocked",
    ]);
    for (let i = lines.length - 1; i >= 0; i--) {
      const t = lines[i].type;
      if (terminalEvents.has(t)) return false;
      if (activeEvents.has(t)) return true;
    }
    return false;
  })();

  const sendMessage = async () => {
    const text = messageText.trim();
    if (!text || sending) return;
    setSending(true);
    try {
      const res = await fetch(`/api/tasks/${taskKey}/message`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      if (res.ok) {
        setMessageText("");
        setSendError(null);
      } else {
        setSendError("Failed to send");
      }
    } catch {
      setSendError("Network error");
    } finally {
      setSending(false);
    }
  };

  return (
    <div className="h-full flex flex-col bg-card">
      {/* macOS-style header */}
      <div className="flex items-center gap-3 px-4 py-2.5 border-b border-border shrink-0">
        <span className="font-mono text-xs text-muted-foreground">{taskKey}</span>

        <div className={cn(
          "flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-full font-medium",
          status === "connected" && "bg-emerald-500/10 text-emerald-400",
          status === "connecting" && "bg-yellow-500/10 text-yellow-400",
          status === "disconnected" && "bg-red-500/10 text-red-400",
        )}>
          <div aria-hidden="true" className={cn(
            "w-1.5 h-1.5 rounded-full",
            status === "connected" && "bg-emerald-400",
            status === "connecting" && "bg-yellow-400",
            status === "disconnected" && "bg-red-400",
          )} />
          {status}
        </div>

        <span className="ml-auto text-[10px] text-muted-foreground font-mono">
          {lines.length} events
        </span>

        <button
          onClick={onClose}
          aria-label="Close terminal"
          className="text-muted-foreground hover:text-foreground transition-colors p-0.5 rounded hover:bg-accent"
        >
          <X className="w-4 h-4" />
        </button>
      </div>

      {/* Terminal body */}
      <div className="flex-1 relative">
        <div ref={termRef} className="absolute inset-0 p-1" />

        {/* Scroll-to-bottom indicator */}
        {!autoScroll && (
          <button
            onClick={scrollToBottom}
            aria-label="Scroll to bottom"
            className="absolute bottom-3 right-3 p-1.5 rounded-md bg-accent/80 text-muted-foreground hover:text-foreground transition-colors backdrop-blur-sm border border-border"
          >
            <ArrowDown className="w-3.5 h-3.5" />
          </button>
        )}
      </div>

      {/* Message input bar */}
      <div className="border-t border-border shrink-0">
        {sendError && (
          <div className="px-3 pt-2 text-xs text-destructive">{sendError}</div>
        )}
        <div className="flex items-center gap-2 px-3 py-2">
          <input
            type="text"
            value={messageText}
            onChange={(e) => { setMessageText(e.target.value); setSendError(null); }}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                sendMessage();
              }
            }}
            disabled={!hasActiveSession || sending}
            placeholder={
              hasActiveSession
                ? "Send a message to the agent..."
                : "No active session"
            }
            className={cn(
              "flex-1 bg-background border border-border rounded-md px-3 py-1.5 text-sm font-mono",
              "placeholder:text-muted-foreground/50 focus:outline-none focus:ring-1 focus:ring-ring",
              (!hasActiveSession || sending) && "opacity-50 cursor-not-allowed",
            )}
          />
          <button
            onClick={sendMessage}
            disabled={!hasActiveSession || sending || !messageText.trim()}
            aria-label="Send message"
            className={cn(
              "p-1.5 rounded-md border border-border transition-colors",
              hasActiveSession && messageText.trim() && !sending
                ? "bg-primary text-primary-foreground hover:bg-primary/90"
                : "bg-accent text-muted-foreground opacity-50 cursor-not-allowed",
            )}
          >
            <Send className="w-4 h-4" />
          </button>
        </div>
      </div>
    </div>
  );
}
