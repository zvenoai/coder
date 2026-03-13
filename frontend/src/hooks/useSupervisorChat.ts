import { useCallback, useEffect, useRef, useState } from "react";
import type { ChannelId, ChatMessage, ChatProgress, ChatSessionInfo, ConnectionStatus } from "../types";
import { useWebSocket } from "./useWebSocket";

export function useSupervisorChat(channel: ChannelId = "chat") {
  const apiBase = `/api/supervisor/channels/${channel}`;
  const wsUrl = `/ws/supervisor/channels/${channel}`;
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [sessionInfo, setSessionInfo] = useState<ChatSessionInfo | null>(null);
  const [generating, setGenerating] = useState(false);
  const [sessionLoading, setSessionLoading] = useState(false);
  const [progress, setProgress] = useState<ChatProgress>({ type: "idle" });
  const streamingTextRef = useRef("");
  const generatingRef = useRef(false);

  // Keep ref in sync with state
  useEffect(() => {
    generatingRef.current = generating;
  }, [generating]);

  // Fetch existing session info and history on mount
  useEffect(() => {
    let cancelled = false;
    fetch(`${apiBase}/session`)
      .then((r) => (r.ok ? r.json() : null))
      .then((info: ChatSessionInfo | null) => {
        if (cancelled || !info) return;
        setSessionInfo(info);
        setGenerating(info.generating);
        // Fetch history
        return fetch(`${apiBase}/history`)
          .then((r) => (r.ok ? r.json() : []))
          .then((history: ChatMessage[]) => {
            if (!cancelled && history.length > 0) {
              setMessages(history);
            }
          });
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [apiBase]);

  const onWsMessage = useCallback(
    (event: { type: string; data: Record<string, unknown> }) => {
      if (event.type === "supervisor_chat_user") {
        // Skip — user messages are added optimistically on send()
        return;
      }
      if (event.type === "supervisor_chat_thinking") {
        const thinking = String(event.data.thinking ?? "");
        setProgress({ type: "thinking", detail: thinking.slice(0, 120) });
        return;
      }
      if (event.type === "supervisor_chat_tool_use") {
        const tool = String(event.data.tool ?? "");
        setProgress({ type: "tool_use", detail: tool });
        return;
      }
      if (event.type === "supervisor_chat_chunk") {
        const text = String(event.data.text ?? "");
        // Detect start of a new stream before accumulating. On auto_send
        // channels (tasks/heartbeat), supervisor_chat_user is skipped so the
        // last message in state may be an old assistant message from a prior
        // turn. Without this guard the chunk handler would overwrite it.
        const isNewStream = streamingTextRef.current === "";
        streamingTextRef.current += text;
        const currentText = streamingTextRef.current;
        setProgress({ type: "idle" });
        setMessages((prev) => {
          const last = prev[prev.length - 1];
          if (!isNewStream && last && last.role === "assistant") {
            return [
              ...prev.slice(0, -1),
              { ...last, content: currentText },
            ];
          }
          return [
            ...prev,
            { role: "assistant" as const, content: currentText, timestamp: Date.now() },
          ];
        });
      }
      if (event.type === "supervisor_chat_done") {
        streamingTextRef.current = "";
        setGenerating(false);
        setProgress({ type: "idle" });
        setSessionInfo((prev) =>
          prev ? { ...prev, generating: false, message_count: prev.message_count + 1 } : prev,
        );
        // Fetch session info — also covers backend-created sessions (tasks /
        // heartbeat channels) where sessionInfo was null at mount because the
        // session didn't exist yet when the hook first loaded.
        fetch(`${apiBase}/session`)
          .then((r) => (r.ok ? r.json() : null))
          .then((info: ChatSessionInfo | null) => {
            if (info) setSessionInfo((prev) => prev ?? info);
          })
          .catch(() => {});
        // Defensive resync: fetch full history to recover from missed events
        fetch(`${apiBase}/history`)
          .then((r) => (r.ok ? r.json() : null))
          .then((history: ChatMessage[] | null) => {
            if (history && history.length > 0) {
              setMessages((prev) => (history.length > prev.length ? history : prev));
            }
          })
          .catch(() => {});
      }
      if (event.type === "supervisor_chat_error") {
        const error = String(event.data.error ?? "Unknown error");
        streamingTextRef.current = "";
        setGenerating(false);
        setProgress({ type: "idle" });
        setMessages((prev) => [
          ...prev,
          { role: "assistant", content: `Error: ${error}`, timestamp: Date.now() },
        ]);
      }
      if (event.type === "supervisor_task_created") {
        const taskKey = String(event.data.task_key ?? "");
        if (taskKey) {
          setMessages((prev) => [
            ...prev,
            { role: "assistant", content: `✅ Created task: ${taskKey}`, timestamp: Date.now() },
          ]);
        }
      }
    },
    [apiBase],
  );

  const { status } = useWebSocket({
    url: wsUrl,
    onMessage: onWsMessage,
  });

  // Resync history on WebSocket reconnect
  const prevStatusRef = useRef<ConnectionStatus>("disconnected");
  useEffect(() => {
    if (status === "connected" && prevStatusRef.current === "disconnected") {
      fetch(`${apiBase}/history`)
        .then((r) => (r.ok ? r.json() : null))
        .then((history: ChatMessage[] | null) => {
          if (history && history.length > 0) {
            setMessages((prev) => (history.length > prev.length ? history : prev));
          }
        })
        .catch(() => {});
    }
    prevStatusRef.current = status as ConnectionStatus;
  }, [status, apiBase]);

  const send = useCallback(
    async (text: string) => {
      if (!text.trim() || generatingRef.current) return;
      // Optimistically add user message
      setMessages((prev) => [
        ...prev,
        { role: "user", content: text, timestamp: Date.now() },
      ]);
      setGenerating(true);
      streamingTextRef.current = "";
      try {
        const res = await fetch(`${apiBase}/send`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text }),
        });
        if (!res.ok) {
          const errorText = await res.text().catch(() => "Failed to send message");
          setGenerating(false);
          setMessages((prev) => [
            ...prev,
            { role: "assistant", content: `Error: ${errorText}`, timestamp: Date.now() },
          ]);
        }
      } catch {
        setGenerating(false);
        setMessages((prev) => [
          ...prev,
          { role: "assistant", content: "Error: Network error", timestamp: Date.now() },
        ]);
      }
    },
    [apiBase],
  );

  const abort = useCallback(async () => {
    try {
      await fetch(`${apiBase}/abort`, { method: "POST" });
    } catch {
      // ignore network errors
    }
    // Always reset generating — backend CancelledError doesn't publish done/error events
    setGenerating(false);
    streamingTextRef.current = "";
  }, [apiBase]);

  const createSession = useCallback(async () => {
    setSessionLoading(true);
    try {
      const res = await fetch(`${apiBase}/session`, { method: "POST" });
      if (res.ok) {
        const info = (await res.json()) as ChatSessionInfo;
        setSessionInfo(info);
        setMessages([]);
        setGenerating(false);
        streamingTextRef.current = "";
      }
    } catch {
      // ignore
    } finally {
      setSessionLoading(false);
    }
  }, [apiBase]);

  const closeSession = useCallback(async () => {
    setSessionLoading(true);
    try {
      await fetch(`${apiBase}/session`, { method: "DELETE" });
      setSessionInfo(null);
      setMessages([]);
      setGenerating(false);
      streamingTextRef.current = "";
    } catch {
      // ignore
    } finally {
      setSessionLoading(false);
    }
  }, [apiBase]);

  return {
    messages,
    sessionInfo,
    generating,
    sessionLoading,
    progress,
    status: status as ConnectionStatus,
    send,
    abort,
    createSession,
    closeSession,
  };
}
