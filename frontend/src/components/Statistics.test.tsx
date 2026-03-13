import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  render,
  screen,
  fireEvent,
  waitFor,
  cleanup,
} from "@testing-library/react";
import Statistics from "./Statistics";

beforeEach(() => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  vi.useFakeTimers({ shouldAdvanceTime: true });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

const SUMMARY = {
  total_tasks: 42,
  success_count: 38,
  success_rate: 90.48,
  total_cost: 12.34,
  avg_duration: 345.6,
  avg_cost: 0.2938,
  days: 7,
};

const COSTS_MODEL = [
  { group: "claude-opus-4", total_cost: 8.5, count: 20 },
  { group: "claude-sonnet-4", total_cost: 3.84, count: 22 },
];

const COSTS_DAY = [
  { group: "2026-03-04", total_cost: 2.1, count: 5 },
  { group: "2026-03-03", total_cost: 3.4, count: 8 },
];

const TASKS = [
  {
    task_key: "QR-100",
    model: "claude-opus-4",
    cost_usd: 0.5,
    duration_seconds: 120,
    success: true,
    error_category: null,
    pr_url: "https://github.com/org/repo/pull/1",
    needs_info: false,
    resumed: false,
    started_at: 1709500000,
    finished_at: 1709500120,
  },
];

const ERRORS = [
  { category: "rate_limit", count: 5, retryable_count: 5 },
  { category: "permanent", count: 2, retryable_count: 0 },
];

function mockFetchSuccess() {
  vi.spyOn(globalThis, "fetch").mockImplementation(
    async (input: string | URL | Request) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.includes("/api/stats/summary")) {
        return Response.json(SUMMARY);
      }
      if (url.includes("group_by=model")) {
        return Response.json(COSTS_MODEL);
      }
      if (url.includes("group_by=day")) {
        return Response.json(COSTS_DAY);
      }
      if (url.includes("/api/stats/tasks")) {
        return Response.json(TASKS);
      }
      if (url.includes("/api/stats/errors")) {
        return Response.json(ERRORS);
      }
      return Response.json({});
    },
  );
}

describe("Statistics", () => {
  it("renders KPI cards with data", async () => {
    mockFetchSuccess();
    render(<Statistics />);

    await waitFor(() => {
      expect(screen.getByText("42")).toBeInTheDocument();
    });
    expect(screen.getByText("90.48%")).toBeInTheDocument();
    expect(screen.getByText("$12.34")).toBeInTheDocument();
    expect(screen.getByText("$0.2938")).toBeInTheDocument();
  });

  it("time window selector triggers refetch with correct days", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(
      async (input: string | URL | Request) => {
        const url = typeof input === "string" ? input : input.toString();
        if (url.includes("/api/stats/summary")) {
          return Response.json(SUMMARY);
        }
        return Response.json([]);
      },
    );

    render(<Statistics />);

    await waitFor(() => {
      expect(screen.getByText("42")).toBeInTheDocument();
    });

    fetchMock.mockClear();

    fireEvent.click(screen.getByText("14d"));

    await waitFor(() => {
      const calls = fetchMock.mock.calls.map((c) =>
        typeof c[0] === "string" ? c[0] : "",
      );
      expect(calls.some((u) => u.includes("days=14"))).toBe(true);
    });
  });

  it("handles fetch errors gracefully", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(
      new Error("Network error"),
    );

    render(<Statistics />);

    await waitFor(() => {
      expect(
        screen.getByText("Failed to load statistics"),
      ).toBeInTheDocument();
    });
  });

  it("renders empty data without crash", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      Response.json(null),
    );

    render(<Statistics />);

    await waitFor(() => {
      expect(
        screen.queryByText("Loading statistics..."),
      ).not.toBeInTheDocument();
    });
  });

  it("renders recent tasks table", async () => {
    mockFetchSuccess();
    render(<Statistics />);

    await waitFor(() => {
      expect(screen.getByText("QR-100")).toBeInTheDocument();
    });
    expect(screen.getByText("success")).toBeInTheDocument();
  });

  it("renders error breakdown", async () => {
    mockFetchSuccess();
    render(<Statistics />);

    await waitFor(() => {
      expect(screen.getByText("rate_limit")).toBeInTheDocument();
    });
    expect(screen.getByText("permanent")).toBeInTheDocument();
    expect(screen.getByText("(5 retryable)")).toBeInTheDocument();
  });
});
