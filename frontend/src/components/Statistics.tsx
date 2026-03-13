import { useCallback, useEffect, useMemo, useState } from "react";
import { cn, formatDuration, formatDateTime } from "../lib/utils";
import type {
  StatsSummary,
  CostEntry,
  TaskRunEntry,
  ErrorEntry,
} from "../types";
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  LineChart,
  Line,
  CartesianGrid,
} from "recharts";

type TimeWindow = 7 | 14 | 30;

type SortField = "finished_at" | "cost_usd" | "duration_seconds";

interface StatsData {
  summary: StatsSummary | null;
  costsByModel: CostEntry[];
  costsByDay: CostEntry[];
  tasks: TaskRunEntry[];
  errors: ErrorEntry[];
}

const EMPTY: StatsData = {
  summary: null,
  costsByModel: [],
  costsByDay: [],
  tasks: [],
  errors: [],
};

const TOOLTIP_STYLE: React.CSSProperties = {
  backgroundColor: "hsl(var(--card))",
  border: "1px solid hsl(var(--border))",
  borderRadius: 8,
  fontSize: 12,
};

const AXIS_TICK = { fontSize: 11, fill: "hsl(var(--muted-foreground))" };

const COST_FORMATTER = (v: number) => [`$${v.toFixed(4)}`, "Cost"];

const TICK_FORMAT_USD = (v: number) => `$${v.toFixed(2)}`;

export default function Statistics() {
  const [days, setDays] = useState<TimeWindow>(7);
  const [data, setData] = useState<StatsData>(EMPTY);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [sortField, setSortField] = useState<SortField>("finished_at");
  const [sortAsc, setSortAsc] = useState(false);

  const fetchData = useCallback(async (d: TimeWindow, signal: AbortSignal) => {
    setLoading(true);
    setError(false);
    try {
      const [summaryRes, costsModelRes, costsDayRes, tasksRes, errorsRes] =
        await Promise.all([
          fetch(`/api/stats/summary?days=${d}`, { signal }),
          fetch(`/api/stats/costs?group_by=model&days=${d}`, { signal }),
          fetch(`/api/stats/costs?group_by=day&days=${d}`, { signal }),
          fetch("/api/stats/tasks?limit=50", { signal }),
          fetch(`/api/stats/errors?days=${d}`, { signal }),
        ]);

      const [summary, costsModel, costsDay, tasks, errors] =
        await Promise.all([
          summaryRes.ok ? summaryRes.json() : null,
          costsModelRes.ok ? costsModelRes.json() : [],
          costsDayRes.ok ? costsDayRes.json() : [],
          tasksRes.ok ? tasksRes.json() : [],
          errorsRes.ok ? errorsRes.json() : [],
        ]);

      if (!signal.aborted) {
        setData({
          summary,
          costsByModel: costsModel,
          costsByDay: costsDay,
          tasks,
          errors,
        });
      }
    } catch (e) {
      if (!signal.aborted) {
        // AbortError is expected on cleanup — only flag real errors
        if (e instanceof DOMException && e.name === "AbortError") return;
        setError(true);
      }
    } finally {
      if (!signal.aborted) {
        setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();

    fetchData(days, controller.signal);
    const interval = setInterval(
      () => fetchData(days, controller.signal),
      30_000,
    );

    return () => {
      controller.abort();
      clearInterval(interval);
    };
  }, [days, fetchData]);

  const handleSort = useCallback(
    (field: SortField) => {
      if (sortField === field) {
        setSortAsc((prev) => !prev);
      } else {
        setSortField(field);
        setSortAsc(false);
      }
    },
    [sortField],
  );

  const sortedTasks = useMemo(
    () =>
      [...data.tasks].sort((a, b) => {
        const va = a[sortField] ?? 0;
        const vb = b[sortField] ?? 0;
        return sortAsc
          ? (va as number) - (vb as number)
          : (vb as number) - (va as number);
      }),
    [data.tasks, sortField, sortAsc],
  );

  const maxErrorCount = useMemo(
    () => Math.max(...data.errors.map((e) => e.count), 1),
    [data.errors],
  );

  if (loading && data.summary === null) {
    return (
      <div className="flex items-center justify-center h-full text-muted-foreground">
        <span className="text-sm">Loading statistics...</span>
      </div>
    );
  }

  if (error && data.summary === null) {
    return (
      <div className="flex items-center justify-center h-full text-muted-foreground">
        <span className="text-sm">
          Failed to load statistics
        </span>
      </div>
    );
  }

  return (
    <div className="p-4 space-y-5 max-w-5xl mx-auto">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold">Statistics</h1>
        <TimeWindowSelector value={days} onChange={setDays} />
      </div>

      {/* KPI cards */}
      {data.summary && (
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
          <KpiCard
            label="Total Tasks"
            value={String(data.summary.total_tasks)}
            color="text-blue-400"
          />
          <KpiCard
            label="Success Rate"
            value={`${data.summary.success_rate}%`}
            color="text-emerald-400"
          />
          <KpiCard
            label="Total Cost"
            value={`$${data.summary.total_cost.toFixed(2)}`}
            color="text-violet-400"
          />
          <KpiCard
            label="Avg Duration"
            value={formatDuration(data.summary.avg_duration)}
            color="text-amber-400"
          />
          <KpiCard
            label="Avg Cost"
            value={`$${data.summary.avg_cost.toFixed(4)}`}
            color="text-orange-400"
          />
        </div>
      )}

      {/* Charts */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {data.costsByModel.length > 0 && (
          <CostByModelChart data={data.costsByModel} />
        )}
        {data.costsByDay.length > 0 && (
          <CostByDayChart data={data.costsByDay} />
        )}
      </div>

      {/* Errors */}
      {data.errors.length > 0 && (
        <ErrorBreakdown errors={data.errors} maxCount={maxErrorCount} />
      )}

      {/* Tasks table */}
      {data.tasks.length > 0 && (
        <RecentTasksTable
          tasks={sortedTasks}
          sortField={sortField}
          sortAsc={sortAsc}
          onSort={handleSort}
        />
      )}
    </div>
  );
}

/* ---------- Sub-components ---------- */

function TimeWindowSelector({
  value,
  onChange,
}: {
  value: TimeWindow;
  onChange: (v: TimeWindow) => void;
}) {
  const options: TimeWindow[] = [7, 14, 30];
  return (
    <div className="flex gap-1 bg-accent/50 rounded-lg p-0.5">
      {options.map((d) => (
        <button
          key={d}
          onClick={() => onChange(d)}
          className={cn(
            "px-3 py-1 rounded-md text-xs font-medium transition-colors",
            value === d
              ? "bg-background text-foreground shadow-sm"
              : "text-muted-foreground hover:text-foreground",
          )}
        >
          {d}d
        </button>
      ))}
    </div>
  );
}

function KpiCard({
  label,
  value,
  color,
}: {
  label: string;
  value: string;
  color: string;
}) {
  return (
    <div className="rounded-lg border border-border bg-card p-3">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className={cn("text-xl font-mono font-semibold mt-1", color)}>
        {value}
      </p>
    </div>
  );
}

function CostByModelChart({ data }: { data: CostEntry[] }) {
  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <h3 className="text-sm font-medium mb-3">Cost by Model</h3>
      <ResponsiveContainer width="100%" height={Math.max(data.length * 32, 120)}>
        <BarChart data={data} layout="vertical" margin={{ left: 8, right: 16 }}>
          <XAxis type="number" tick={AXIS_TICK} tickFormatter={TICK_FORMAT_USD} />
          <YAxis type="category" dataKey="group" tick={AXIS_TICK} width={140} />
          <Tooltip contentStyle={TOOLTIP_STYLE} formatter={COST_FORMATTER} />
          <Bar dataKey="total_cost" fill="#60a5fa" radius={[0, 4, 4, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

function CostByDayChart({ data }: { data: CostEntry[] }) {
  const chronological = [...data].reverse();
  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <h3 className="text-sm font-medium mb-3">Cost by Day</h3>
      <ResponsiveContainer width="100%" height={200}>
        <LineChart data={chronological} margin={{ left: 8, right: 16, top: 8 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
          <XAxis dataKey="group" tick={AXIS_TICK} />
          <YAxis tick={AXIS_TICK} tickFormatter={TICK_FORMAT_USD} />
          <Tooltip contentStyle={TOOLTIP_STYLE} formatter={COST_FORMATTER} />
          <Line type="monotone" dataKey="total_cost" stroke="#a78bfa" strokeWidth={2} dot={{ r: 3 }} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

function RecentTasksTable({
  tasks,
  sortField,
  sortAsc,
  onSort,
}: {
  tasks: TaskRunEntry[];
  sortField: SortField;
  sortAsc: boolean;
  onSort: (f: SortField) => void;
}) {
  const arrow = (f: SortField) =>
    sortField === f ? (sortAsc ? " \u2191" : " \u2193") : "";

  return (
    <div className="rounded-lg border border-border bg-card overflow-hidden">
      <h3 className="text-sm font-medium p-4 pb-2">Recent Tasks</h3>
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-border text-muted-foreground">
              <th className="text-left px-4 py-2 font-medium">Task</th>
              <th className="text-left px-4 py-2 font-medium">Model</th>
              <th className="text-left px-4 py-2 font-medium">Status</th>
              <th
                className="text-right px-4 py-2 font-medium cursor-pointer hover:text-foreground"
                onClick={() => onSort("cost_usd")}
              >
                Cost{arrow("cost_usd")}
              </th>
              <th
                className="text-right px-4 py-2 font-medium cursor-pointer hover:text-foreground"
                onClick={() => onSort("duration_seconds")}
              >
                Duration{arrow("duration_seconds")}
              </th>
              <th
                className="text-right px-4 py-2 font-medium cursor-pointer hover:text-foreground"
                onClick={() => onSort("finished_at")}
              >
                Finished{arrow("finished_at")}
              </th>
            </tr>
          </thead>
          <tbody>
            {tasks.map((t) => (
              <tr
                key={`${t.task_key}-${t.started_at}`}
                className="border-b border-border/50 hover:bg-accent/30"
              >
                <td className="px-4 py-2 font-mono">{t.task_key}</td>
                <td className="px-4 py-2 text-muted-foreground truncate max-w-[140px]">
                  {t.model}
                </td>
                <td className="px-4 py-2">
                  <TaskStatusBadge success={t.success} errorCategory={t.error_category} />
                </td>
                <td className="px-4 py-2 text-right font-mono">
                  ${t.cost_usd.toFixed(4)}
                </td>
                <td className="px-4 py-2 text-right font-mono">
                  {formatDuration(t.duration_seconds)}
                </td>
                <td className="px-4 py-2 text-right text-muted-foreground">
                  {formatDateTime(t.finished_at)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function TaskStatusBadge({
  success,
  errorCategory,
}: {
  success: boolean;
  errorCategory: string | null;
}) {
  if (success) {
    return (
      <span className="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium bg-emerald-400/10 text-emerald-400">
        success
      </span>
    );
  }
  return (
    <span
      className="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium bg-red-400/10 text-red-400"
      title={errorCategory ?? undefined}
    >
      failed
    </span>
  );
}

function ErrorBreakdown({
  errors,
  maxCount,
}: {
  errors: ErrorEntry[];
  maxCount: number;
}) {
  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <h3 className="text-sm font-medium mb-3">Error Breakdown</h3>
      <div className="space-y-2">
        {errors.map((e) => (
          <div key={e.category} className="flex items-center gap-3">
            <span className="text-xs text-muted-foreground w-28 shrink-0 truncate">
              {e.category}
            </span>
            <div className="flex-1 h-5 bg-accent/30 rounded overflow-hidden">
              <div
                className="h-full bg-red-400/60 rounded"
                style={{
                  width: `${(e.count / maxCount) * 100}%`,
                }}
              />
            </div>
            <span className="text-xs font-mono w-8 text-right">
              {e.count}
            </span>
            {e.retryable_count > 0 && (
              <span className="text-[10px] text-muted-foreground">
                ({e.retryable_count} retryable)
              </span>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
