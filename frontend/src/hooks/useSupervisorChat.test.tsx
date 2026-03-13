import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useSupervisorChat } from "./useSupervisorChat";

// ---------------------------------------------------------------------------
// Minimal WebSocket mock
// ---------------------------------------------------------------------------

class MockWebSocket {
  static instance: MockWebSocket | null = null;
  onmessage: ((e: MessageEvent) => void) | null = null;
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(_url: string) {
    MockWebSocket.instance = this;
    // Trigger open asynchronously so the hook can register handlers first
    setTimeout(() => this.onopen?.(), 0);
  }

  send(_data: string) {}

  close() {
    this.onclose?.();
  }

  dispatchMessage(data: object) {
    this.onmessage?.(new MessageEvent("message", { data: JSON.stringify(data) }));
  }
}

// ---------------------------------------------------------------------------
// Test suite
// ---------------------------------------------------------------------------

describe("useSupervisorChat", () => {
  beforeEach(() => {
    MockWebSocket.instance = null;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    vi.stubGlobal("WebSocket", MockWebSocket as any);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("populates sessionInfo on supervisor_chat_done when backend created the session", async () => {
    // tasks / heartbeat channels: session is created by backend via auto_send.
    // At mount, GET /session returns 404 (no session yet) → sessionInfo stays null.
    // When supervisor_chat_done fires, the hook must fetch /session and populate
    // sessionInfo so the "No active session" overlay disappears.

    const mockSession = {
      session_id: "backend-session-1",
      created_at: 1000,
      message_count: 1,
      generating: false,
    };

    let sessionFetchCount = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (String(url).includes("/session")) {
          sessionFetchCount++;
          if (sessionFetchCount === 1) {
            // Mount-time fetch: no session yet
            return { ok: false, json: async () => null } as Response;
          }
          // Subsequent fetch (triggered by supervisor_chat_done): session exists
          return { ok: true, json: async () => mockSession } as Response;
        }
        if (String(url).includes("/history")) {
          return { ok: true, json: async () => [] } as Response;
        }
        return { ok: false, json: async () => null } as Response;
      }),
    );

    const { result } = renderHook(() => useSupervisorChat("tasks"));

    // Wait for mount-time session fetch to complete — sessionInfo should be null
    await waitFor(() => expect(sessionFetchCount).toBeGreaterThanOrEqual(1));
    expect(result.current.sessionInfo).toBeNull();

    // Backend sends a message and fires supervisor_chat_done
    act(() => {
      MockWebSocket.instance?.dispatchMessage({
        type: "supervisor_chat_chunk",
        task_key: "supervisor-tasks",
        data: { text: "Hello from backend" },
        ts: Date.now(),
      });
    });
    act(() => {
      MockWebSocket.instance?.dispatchMessage({
        type: "supervisor_chat_done",
        task_key: "supervisor-tasks",
        data: {},
        ts: Date.now(),
      });
    });

    // sessionInfo must be populated — overlay should NOT show
    await waitFor(() => {
      expect(result.current.sessionInfo).not.toBeNull();
    });
    expect(result.current.sessionInfo?.session_id).toBe("backend-session-1");
  });

  it("appends new assistant message instead of overwriting on auto_send channels", async () => {
    // tasks/heartbeat: supervisor_chat_user is skipped, so messages may end
    // with an old assistant message. When a new supervisor_chat_chunk arrives,
    // the chunk handler must NOT overwrite the old message — it must append.

    const existingHistory = [
      { role: "assistant", content: "Previous answer", timestamp: 1000 },
    ];

    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (String(url).includes("/session")) {
          return {
            ok: true,
            json: async () => ({
              session_id: "s1",
              created_at: 1,
              message_count: 1,
              generating: false,
            }),
          } as Response;
        }
        if (String(url).includes("/history")) {
          return { ok: true, json: async () => existingHistory } as Response;
        }
        return { ok: false, json: async () => null } as Response;
      }),
    );

    const { result } = renderHook(() => useSupervisorChat("tasks"));

    // Wait for history to load — messages should contain the existing assistant msg
    await waitFor(() =>
      expect(result.current.messages).toHaveLength(1),
    );
    expect(result.current.messages[0].content).toBe("Previous answer");

    // Backend fires supervisor_chat_chunk (new auto_send stream, no user event)
    act(() => {
      MockWebSocket.instance?.dispatchMessage({
        type: "supervisor_chat_chunk",
        task_key: "supervisor-tasks",
        data: { text: "New response" },
        ts: Date.now(),
      });
    });

    // Must have 2 messages: old one intact + new streaming assistant message
    expect(result.current.messages).toHaveLength(2);
    expect(result.current.messages[0].content).toBe("Previous answer");
    expect(result.current.messages[1].content).toBe("New response");
  });

  it("does not overwrite existing sessionInfo on supervisor_chat_done", async () => {
    // chat channel: session was created by user — sessionInfo is already set at mount.
    // supervisor_chat_done must update it optimistically, not replace it with API data.

    const existingSession = {
      session_id: "user-session-1",
      created_at: 2000,
      message_count: 5,
      generating: true,
    };

    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (String(url).includes("/session")) {
          return { ok: true, json: async () => existingSession } as Response;
        }
        if (String(url).includes("/history")) {
          return { ok: true, json: async () => [] } as Response;
        }
        return { ok: false, json: async () => null } as Response;
      }),
    );

    const { result } = renderHook(() => useSupervisorChat("chat"));

    await waitFor(() => expect(result.current.sessionInfo).not.toBeNull());

    act(() => {
      MockWebSocket.instance?.dispatchMessage({
        type: "supervisor_chat_done",
        task_key: "supervisor-chat",
        data: {},
        ts: Date.now(),
      });
    });

    await waitFor(() => {
      // message_count incremented optimistically (+1)
      expect(result.current.sessionInfo?.message_count).toBe(6);
    });
    // session_id must not change
    expect(result.current.sessionInfo?.session_id).toBe("user-session-1");
  });
});
