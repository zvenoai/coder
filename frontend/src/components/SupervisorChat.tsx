import { useCallback, useEffect, useRef, useState } from "react";
import { X, Send, Square, RotateCcw, ArrowDown, Loader2 } from "lucide-react";
import { cn } from "../lib/utils";
import type { ChannelId } from "../types";
import { useSupervisorChat } from "../hooks/useSupervisorChat";
import ChatMarkdown from "./ChatMarkdown";

interface SupervisorChatProps {
  onClose: () => void;
}

const CHANNEL_LABELS: Record<ChannelId, string> = {
  chat: "Чат",
  tasks: "Задачи",
  heartbeat: "Мониторинг",
};

const CHANNELS: ChannelId[] = ["chat", "tasks", "heartbeat"];

export default function SupervisorChat({ onClose }: SupervisorChatProps) {
  const [activeChannel, setActiveChannel] = useState<ChannelId>("chat");
  const [inputText, setInputText] = useState("");
  const [autoScroll, setAutoScroll] = useState(true);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // Instantiate all three hooks unconditionally (React rules of hooks)
  const chatHook = useSupervisorChat("chat");
  const tasksHook = useSupervisorChat("tasks");
  const heartbeatHook = useSupervisorChat("heartbeat");

  const hookByChannel = {
    chat: chatHook,
    tasks: tasksHook,
    heartbeat: heartbeatHook,
  };
  const active = hookByChannel[activeChannel];

  // Track unread counts for inactive channels
  const [seenCounts, setSeenCounts] = useState<Record<ChannelId, number>>({
    chat: 0,
    tasks: 0,
    heartbeat: 0,
  });

  // Switching channels; seenCounts sync is handled by the useEffect below
  const handleChannelSwitch = useCallback((channel: ChannelId) => {
    setActiveChannel(channel);
    setAutoScroll(true);
  }, []);

  // Track new messages in inactive channels
  useEffect(() => {
    setSeenCounts((prev) => ({
      ...prev,
      [activeChannel]: hookByChannel[activeChannel].messages.length,
    }));
  // Only run when active channel changes or its message count changes
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeChannel, hookByChannel[activeChannel].messages.length]);

  const unreadFor = (channel: ChannelId): number => {
    if (channel === activeChannel) return 0;
    return Math.max(
      0,
      hookByChannel[channel].messages.length - seenCounts[channel],
    );
  };

  // Auto-scroll to bottom on new messages (when enabled)
  useEffect(() => {
    if (autoScroll) {
      messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [active.messages, autoScroll]);

  // Detect manual scroll-up to disable auto-scroll
  const handleScroll = useCallback(() => {
    const el = scrollContainerRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    setAutoScroll(atBottom);
  }, []);

  // Focus input on mount and when session is created
  useEffect(() => {
    if (activeChannel === "chat") {
      inputRef.current?.focus();
    }
  }, [active.sessionInfo, activeChannel]);

  const handleSend = () => {
    const text = inputText.trim();
    if (!text) return;
    active.send(text);
    setInputText("");
  };

  const handleNewSession = useCallback(async () => {
    await active.closeSession();
    await active.createSession();
  }, [active]);

  const hasSession = active.sessionInfo !== null;
  const canInput = activeChannel === "chat";

  return (
    <div className="h-full flex flex-col bg-card">
      {/* Header */}
      <div className="flex items-center gap-3 px-4 py-2.5 border-b border-border shrink-0">
        <span className="font-mono text-xs text-muted-foreground">
          Supervisor
        </span>

        <div
          className={cn(
            "flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-full font-medium",
            active.status === "connected" && "bg-emerald-500/10 text-emerald-400",
            active.status === "connecting" && "bg-yellow-500/10 text-yellow-400",
            active.status === "disconnected" && "bg-red-500/10 text-red-400",
          )}
        >
          <div
            aria-hidden="true"
            className={cn(
              "w-1.5 h-1.5 rounded-full",
              active.status === "connected" && "bg-emerald-400",
              active.status === "connecting" && "bg-yellow-400",
              active.status === "disconnected" && "bg-red-400",
            )}
          />
          {active.status}
        </div>

        {hasSession && (
          <span className="text-[10px] text-muted-foreground font-mono">
            {active.messages.length} msgs
          </span>
        )}

        <div className="ml-auto flex items-center gap-1">
          {hasSession && canInput && (
            <button
              onClick={handleNewSession}
              disabled={active.sessionLoading}
              title="New session"
              className={cn(
                "text-muted-foreground hover:text-foreground transition-colors p-0.5 rounded hover:bg-accent",
                active.sessionLoading && "opacity-50 cursor-not-allowed",
              )}
            >
              {active.sessionLoading ? (
                <Loader2 className="w-3.5 h-3.5 animate-spin" />
              ) : (
                <RotateCcw className="w-3.5 h-3.5" />
              )}
            </button>
          )}
          <button
            onClick={onClose}
            aria-label="Close chat"
            className="text-muted-foreground hover:text-foreground transition-colors p-0.5 rounded hover:bg-accent"
          >
            <X className="w-4 h-4" />
          </button>
        </div>
      </div>

      {/* Tab bar */}
      <div className="flex border-b border-border shrink-0">
        {CHANNELS.map((channel) => {
          const unread = unreadFor(channel);
          return (
            <button
              key={channel}
              onClick={() => handleChannelSwitch(channel)}
              className={cn(
                "flex items-center gap-1.5 px-4 py-1.5 text-xs border-b-2 transition-colors",
                channel === activeChannel
                  ? "border-primary text-foreground"
                  : "border-transparent text-muted-foreground hover:text-foreground",
              )}
            >
              {CHANNEL_LABELS[channel]}
              {unread > 0 && (
                <span className="flex items-center justify-center min-w-[16px] h-4 px-1 rounded-full bg-primary/20 text-primary text-[10px] font-medium">
                  {unread}
                </span>
              )}
            </button>
          );
        })}
      </div>

      {/* Message area */}
      <div
        ref={scrollContainerRef}
        onScroll={handleScroll}
        className="flex-1 overflow-y-auto p-4 space-y-3 min-h-0"
      >
        {!hasSession && canInput && (
          <div className="flex flex-col items-center justify-center h-full gap-3 text-muted-foreground">
            <p className="text-sm">No active supervisor session</p>
            <button
              onClick={active.createSession}
              disabled={active.sessionLoading}
              className={cn(
                "px-3 py-1.5 rounded-md bg-primary text-primary-foreground text-sm hover:bg-primary/90 transition-colors",
                active.sessionLoading && "opacity-50 cursor-not-allowed",
              )}
            >
              {active.sessionLoading ? (
                <span className="flex items-center gap-1.5">
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                  Starting...
                </span>
              ) : (
                "Start conversation"
              )}
            </button>
          </div>
        )}

        {hasSession && active.messages.length === 0 && !active.generating && (
          <div className="flex items-center justify-center h-full text-muted-foreground text-sm">
            {canInput
              ? "Send a message to start chatting with the supervisor"
              : "No messages yet"}
          </div>
        )}

        {active.messages.map((msg, i) => (
          <div
            key={`${msg.timestamp}-${i}`}
            className={cn(
              "flex",
              msg.role === "user" ? "justify-end" : "justify-start",
            )}
          >
            <div
              className={cn(
                "max-w-[85%] rounded-lg px-3 py-2 text-sm",
                msg.role === "user"
                  ? "bg-primary text-primary-foreground"
                  : "bg-accent text-foreground",
              )}
            >
              {msg.role === "assistant" ? (
                <ChatMarkdown content={msg.content} />
              ) : (
                <pre className="whitespace-pre-wrap font-mono text-xs break-words">
                  {msg.content}
                </pre>
              )}
            </div>
          </div>
        ))}

        {active.generating &&
          active.messages.length > 0 &&
          active.messages[active.messages.length - 1].role === "user" && (
            <div className="flex justify-start">
              <div className="bg-accent text-foreground rounded-lg px-3 py-2 text-sm">
                <span className="animate-pulse text-muted-foreground text-xs">
                  {active.progress.type === "tool_use" && active.progress.detail
                    ? `Using: ${active.progress.detail}`
                    : "Thinking..."}
                </span>
                {active.progress.type === "thinking" && active.progress.detail && (
                  <p className="text-muted-foreground/50 text-[10px] mt-0.5 leading-snug line-clamp-2">
                    {active.progress.detail}
                  </p>
                )}
              </div>
            </div>
          )}

        <div ref={messagesEndRef} />
      </div>

      {/* Scroll-to-bottom button */}
      {!autoScroll && (
        <div className="flex justify-center -mt-10 mb-1 relative z-10">
          <button
            onClick={() => {
              setAutoScroll(true);
              messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
            }}
            aria-label="Scroll to bottom"
            className="p-1.5 rounded-full bg-accent/90 text-muted-foreground hover:text-foreground border border-border shadow-sm transition-colors"
          >
            <ArrowDown className="w-3.5 h-3.5" />
          </button>
        </div>
      )}

      {/* Input bar — only functional in "chat" channel */}
      <div className="flex items-center gap-2 px-3 py-2 border-t border-border shrink-0">
        <input
          ref={inputRef}
          type="text"
          value={inputText}
          onChange={(e) => setInputText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              handleSend();
            }
          }}
          disabled={!canInput || !hasSession || active.generating}
          placeholder={
            !canInput
              ? `${CHANNEL_LABELS[activeChannel]} — read only`
              : hasSession
                ? active.generating
                  ? "Supervisor is responding..."
                  : "Ask the supervisor..."
                : "Start a session first"
          }
          className={cn(
            "flex-1 bg-background border border-border rounded-md px-3 py-1.5 text-sm font-mono",
            "placeholder:text-muted-foreground/50 focus:outline-none focus:ring-1 focus:ring-ring",
            (!canInput || !hasSession || active.generating) &&
              "opacity-50 cursor-not-allowed",
          )}
        />
        {canInput && active.generating ? (
          <button
            onClick={active.abort}
            aria-label="Stop generation"
            className="p-1.5 rounded-md border border-border bg-red-500/10 text-red-400 hover:bg-red-500/20 transition-colors"
            title="Stop generation"
          >
            <Square className="w-4 h-4" />
          </button>
        ) : (
          <button
            onClick={handleSend}
            disabled={!canInput || !hasSession || !inputText.trim()}
            aria-label="Send message"
            className={cn(
              "p-1.5 rounded-md border border-border transition-colors",
              canInput && hasSession && inputText.trim()
                ? "bg-primary text-primary-foreground hover:bg-primary/90"
                : "bg-accent text-muted-foreground opacity-50 cursor-not-allowed",
            )}
          >
            <Send className="w-4 h-4" />
          </button>
        )}
      </div>
    </div>
  );
}
