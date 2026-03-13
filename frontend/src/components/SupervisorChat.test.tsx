import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useSupervisorChat } from "../hooks/useSupervisorChat";
import SupervisorChat from "./SupervisorChat";

// ---------------------------------------------------------------------------
// Mock useSupervisorChat so the component renders without real WS / fetch
// ---------------------------------------------------------------------------

vi.mock("../hooks/useSupervisorChat");

// jsdom does not implement scrollIntoView
window.HTMLElement.prototype.scrollIntoView = vi.fn();

const makeHook = (overrides: Record<string, unknown> = {}) => ({
  messages: [],
  sessionInfo: { session_id: "s1", created_at: 1, message_count: 0, generating: false },
  generating: false,
  sessionLoading: false,
  progress: { type: "idle" as const },
  status: "connected" as const,
  send: vi.fn(),
  abort: vi.fn(),
  createSession: vi.fn(),
  closeSession: vi.fn(),
  ...overrides,
});

const mockHook = vi.mocked(useSupervisorChat);

beforeEach(() => {
  mockHook.mockReturnValue(makeHook());
});

afterEach(() => {
  cleanup();
});

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function switchToChannel(label: string) {
  fireEvent.click(screen.getByText(label));
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("SupervisorChat — session management on read-only channels", () => {
  it("shows 'New session' button on chat channel (read-write)", () => {
    render(<SupervisorChat onClose={vi.fn()} />);
    // chat is the default active channel
    expect(screen.queryByTitle("New session")).not.toBeNull();
  });

  it("hides 'New session' button on tasks channel (read-only)", () => {
    render(<SupervisorChat onClose={vi.fn()} />);
    switchToChannel("Задачи");
    // Button must be absent — clicking it would wipe epic planning messages
    expect(screen.queryByTitle("New session")).toBeNull();
  });

  it("hides 'New session' button on heartbeat channel (read-only)", () => {
    render(<SupervisorChat onClose={vi.fn()} />);
    switchToChannel("Мониторинг");
    expect(screen.queryByTitle("New session")).toBeNull();
  });

  it("hides 'Start conversation' button on tasks channel when sessionInfo is null", () => {
    mockHook.mockReturnValue(makeHook({ sessionInfo: null }));

    render(<SupervisorChat onClose={vi.fn()} />);
    switchToChannel("Задачи");
    // Clicking "Start conversation" on a read-only channel would destroy the
    // backend-created session and wipe all messages.
    expect(screen.queryByText("Start conversation")).toBeNull();
  });

  it("shows 'Start conversation' button on chat channel when sessionInfo is null", () => {
    mockHook.mockReturnValue(makeHook({ sessionInfo: null }));

    render(<SupervisorChat onClose={vi.fn()} />);
    // chat is default — button must appear so users can start a session
    expect(screen.queryByText("Start conversation")).not.toBeNull();
  });
});
