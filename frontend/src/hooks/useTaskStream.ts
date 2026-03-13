import { useCallback, useEffect, useRef, useState } from "react";
import type { ConnectionStatus, WsEvent } from "../types";
import { useWebSocket } from "./useWebSocket";

const MAX_LINES = 2000;

export function useTaskStream(taskKey: string) {
  const [lines, setLines] = useState<WsEvent[]>([]);
  const prevKeyRef = useRef(taskKey);

  // Reset lines if taskKey changes (defensive — normally the component
  // is unmounted/remounted via key prop, but this handles edge cases).
  useEffect(() => {
    if (prevKeyRef.current !== taskKey) {
      setLines([]);
      prevKeyRef.current = taskKey;
    }
  }, [taskKey]);

  // Fetch historical events for this task so the terminal shows past output.
  // Uses functional update to merge with any WS events that arrived during fetch.
  useEffect(() => {
    let cancelled = false;
    fetch(`/api/tasks/${taskKey}/events`)
      .then((res) => (res.ok ? res.json() : null))
      .then((data: WsEvent[] | null) => {
        if (!cancelled && data && data.length > 0) {
          setLines((prev) => {
            if (prev.length === 0) return data;
            const lastHistTs = data[data.length - 1].ts;
            const liveEvents = prev.filter((e) => e.ts > lastHistTs);
            return [...data, ...liveEvents];
          });
        }
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [taskKey]);

  const onMessage = useCallback((event: WsEvent) => {
    setLines((prev) => {
      const next = [...prev, event];
      return next.length > MAX_LINES ? next.slice(-MAX_LINES) : next;
    });
  }, []);

  const { status } = useWebSocket({
    url: `/ws/tasks/${taskKey}/stream`,
    onMessage,
  });

  return { lines, status: status as ConnectionStatus };
}
