import { useEffect, useRef, useState, useCallback } from "react";
import type { ConnectionStatus, WsEvent } from "../types";

interface UseWebSocketOptions {
  url: string;
  onMessage?: (event: WsEvent) => void;
}

export function useWebSocket({ url, onMessage }: UseWebSocketOptions) {
  const [status, setStatus] = useState<ConnectionStatus>("disconnected");
  const wsRef = useRef<WebSocket | null>(null);
  const retryRef = useRef(0);
  const onMessageRef = useRef(onMessage);
  onMessageRef.current = onMessage;

  const connect = useCallback(() => {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${protocol}//${window.location.host}${url}`;

    // Track whether this specific connection attempt is still valid.
    // When the effect cleanup runs (URL change or unmount), disposed is set
    // to true so the onclose handler won't schedule a zombie reconnect.
    let disposed = false;

    setStatus("connecting");
    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;

    ws.onopen = () => {
      if (disposed) {
        ws.close();
        return;
      }
      setStatus("connected");
      retryRef.current = 0;
    };

    ws.onmessage = (e) => {
      if (disposed) return;
      try {
        const data = JSON.parse(e.data) as WsEvent;
        onMessageRef.current?.(data);
      } catch {
        // ignore parse errors
      }
    };

    ws.onclose = () => {
      if (disposed) return;
      setStatus("disconnected");
      wsRef.current = null;
      // Exponential backoff: 1s, 2s, 4s, 8s, max 30s
      const delay = Math.min(1000 * 2 ** retryRef.current, 30000);
      retryRef.current++;
      setTimeout(() => {
        if (!disposed) connect();
      }, delay);
    };

    ws.onerror = () => {
      ws.close();
    };

    // Return a dispose function for effect cleanup
    return () => {
      disposed = true;
      ws.close();
    };
  }, [url]);

  useEffect(() => {
    retryRef.current = 0;
    const dispose = connect();
    return () => {
      dispose();
      wsRef.current = null;
    };
  }, [connect]);

  return { status };
}
