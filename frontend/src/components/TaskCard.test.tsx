import { describe, it, expect } from "vitest";
import type { WsEvent } from "../types";

import {
  extractCostDuration,
  derivePhase,
  deriveSubState,
} from "./TaskCard";

function makeEvent(type: string, data: Record<string, unknown> = {}, ts = 1): WsEvent {
  return { type, task_key: "QR-1", data, ts };
}

describe("extractCostDuration", () => {
  it("reads cost and duration from task_completed event", () => {
    const events = [
      makeEvent("task_started", { summary: "test" }),
      makeEvent("task_completed", { cost: 1.5, duration: 120 }),
    ];
    const { cost, durationMs } = extractCostDuration(events);
    expect(cost).toBe(1.5);
    expect(durationMs).toBe(120000); // duration (seconds) * 1000
  });

  it("reads cost and duration from task_failed event", () => {
    const events = [
      makeEvent("task_started", { summary: "test" }),
      makeEvent("task_failed", { cost: 0.3, duration: 60, error: "timeout" }),
    ];
    const { cost, durationMs } = extractCostDuration(events);
    expect(cost).toBe(0.3);
    expect(durationMs).toBe(60000);
  });

  it("reads cost and duration from pr_tracked event", () => {
    const events = [
      makeEvent("task_started", { summary: "test" }),
      makeEvent("pr_tracked", { cost: 2.0, duration: 200, pr_url: "https://..." }),
    ];
    const { cost, durationMs } = extractCostDuration(events);
    expect(cost).toBe(2.0);
    expect(durationMs).toBe(200000);
  });

  it("falls back to agent_result for in-progress tasks", () => {
    const events = [
      makeEvent("task_started", { summary: "test" }),
      makeEvent("agent_result", { cost: 0.5, duration_ms: 30000 }),
    ];
    const { cost, durationMs } = extractCostDuration(events);
    expect(cost).toBe(0.5);
    expect(durationMs).toBe(30000);
  });

  it("returns undefined when no cost/duration data available", () => {
    const events = [makeEvent("task_started", { summary: "test" })];
    const { cost, durationMs } = extractCostDuration(events);
    expect(cost).toBeUndefined();
    expect(durationMs).toBeUndefined();
  });

  it("prefers terminal event over agent_result", () => {
    const events = [
      makeEvent("task_started", { summary: "test" }),
      makeEvent("agent_result", { cost: 0.2, duration_ms: 10000 }),
      makeEvent("task_completed", { cost: 1.5, duration: 120 }),
    ];
    const { cost, durationMs } = extractCostDuration(events);
    expect(cost).toBe(1.5);
    expect(durationMs).toBe(120000);
  });

  it("shows live agent_result for resumed run after previous completion", () => {
    const events = [
      makeEvent("task_started", { summary: "test" }, 1),
      makeEvent("task_completed", { cost: 1.5, duration: 120 }, 2),
      makeEvent("needs_info_response", { text: "clarification" }, 3),
      makeEvent("agent_result", { cost: 0.8, duration_ms: 45000 }, 4),
    ];
    const { cost, durationMs } = extractCostDuration(events);
    // Should show current live run (0.8, 45000), not stale completed (1.5, 120000)
    expect(cost).toBe(0.8);
    expect(durationMs).toBe(45000);
  });

  it("shows live agent_result for retry after previous failure", () => {
    const events = [
      makeEvent("task_started", { summary: "test" }, 1),
      makeEvent("task_failed", { cost: 0.3, duration: 60, error: "timeout" }, 2),
      makeEvent("task_started", { summary: "test" }, 3),
      makeEvent("agent_result", { cost: 0.5, duration_ms: 30000 }, 4),
    ];
    const { cost, durationMs } = extractCostDuration(events);
    // Should show current retry (0.5, 30000), not previous failure (0.3, 60000)
    expect(cost).toBe(0.5);
    expect(durationMs).toBe(30000);
  });
});

describe("derivePhase", () => {
  it("returns deferred for task_deferred event", () => {
    const events = [
      makeEvent("task_started"),
      makeEvent("task_deferred", { blockers: ["QR-10"] }),
    ];
    expect(derivePhase(events)).toBe("deferred");
  });

  it("returns running after task_unblocked (was deferred)", () => {
    const events = [
      makeEvent("task_started"),
      makeEvent("task_deferred", { blockers: ["QR-10"] }),
      makeEvent("task_unblocked"),
    ];
    expect(derivePhase(events)).toBe("running");
  });

  it("returns skipped for task_skipped event", () => {
    const events = [
      makeEvent("task_started"),
      makeEvent("task_skipped", { reason: "duplicate" }),
    ];
    expect(derivePhase(events)).toBe("skipped");
  });

  it("returns running when task is restarted after being skipped", () => {
    const events = [
      makeEvent("task_started"),
      makeEvent("task_skipped", { reason: "preflight" }),
      makeEvent("epic_child_reset"),
      makeEvent("task_started"),
      makeEvent("agent_output", { text: "working..." }),
    ];
    expect(derivePhase(events)).toBe("running");
  });

  it("returns running when task is restarted after failure", () => {
    const events = [
      makeEvent("task_started"),
      makeEvent("task_failed", { error: "timeout" }),
      makeEvent("task_started"),
      makeEvent("agent_output", { text: "retrying..." }),
    ];
    expect(derivePhase(events)).toBe("running");
  });

  it("returns review when restarted task reaches PR", () => {
    const events = [
      makeEvent("task_started"),
      makeEvent("task_skipped", { reason: "preflight" }),
      makeEvent("task_started"),
      makeEvent("pr_tracked", { pr_url: "https://..." }),
    ];
    expect(derivePhase(events)).toBe("review");
  });

  it("returns completed for epic_completed event", () => {
    const events = [
      makeEvent("epic_detected", { children: 3 }),
      makeEvent("epic_completed", { children_total: 3, cancelled: 0 }),
    ];
    expect(derivePhase(events)).toBe("completed");
  });

  it("returns completed for epic_completed even with prior running events", () => {
    const events = [
      makeEvent("epic_detected", { children: 3 }),
      makeEvent("epic_awaiting_plan"),
      makeEvent("epic_child_ready", { child_key: "QR-10" }),
      makeEvent("epic_completed", { children_total: 3, cancelled: 1 }),
    ];
    expect(derivePhase(events)).toBe("completed");
  });

  it("returns running for epic in progress (no epic_completed)", () => {
    const events = [
      makeEvent("epic_detected", { children: 3 }),
      makeEvent("epic_awaiting_plan"),
      makeEvent("epic_child_ready", { child_key: "QR-10" }),
    ];
    expect(derivePhase(events)).toBe("running");
  });
});

describe("deriveSubState", () => {
  it("returns fixing_merge for merge_conflict event", () => {
    const events = [
      makeEvent("pr_tracked"),
      makeEvent("merge_conflict"),
    ];
    expect(deriveSubState(events)).toBe("fixing_merge");
  });

  it("returns idle for epic_completed event", () => {
    const events = [
      makeEvent("epic_detected"),
      makeEvent("epic_completed", { children_total: 3, cancelled: 0 }),
    ];
    expect(deriveSubState(events)).toBe("idle");
  });

  it("returns idle after agent_result following merge_conflict", () => {
    const events = [
      makeEvent("pr_tracked"),
      makeEvent("merge_conflict"),
      makeEvent("agent_output"),
      makeEvent("agent_result"),
    ];
    expect(deriveSubState(events)).toBe("idle");
  });
});
