import { useCallback, useEffect, useState } from "react";
import type { ConnectionStatus, WsEvent } from "../types";
import { useWebSocket } from "./useWebSocket";

const MAX_EVENTS = 500;

export function useGlobalEvents() {
  const [events, setEvents] = useState<WsEvent[]>([]);

  // Fetch historical events on mount so the dashboard shows past state.
  // Uses functional update to merge with any WS events that arrived during fetch.
  useEffect(() => {
    let cancelled = false;
    fetch("/api/events")
      .then((res) => (res.ok ? res.json() : null))
      .then((data: WsEvent[] | null) => {
        if (!cancelled && data && data.length > 0) {
          setEvents((prev) => {
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
  }, []);

  const onMessage = useCallback((event: WsEvent) => {
    setEvents((prev) => {
      const next = [...prev, event];
      return next.length > MAX_EVENTS ? next.slice(-MAX_EVENTS) : next;
    });
  }, []);

  const { status } = useWebSocket({
    url: "/ws/events",
    onMessage,
  });

  return { events, status: status as ConnectionStatus };
}
