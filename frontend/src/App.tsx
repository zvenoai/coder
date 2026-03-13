import { useState, useCallback, lazy, Suspense } from "react";
import { cn } from "./lib/utils";
import Dashboard from "./components/Dashboard";
import AgentTerminal from "./components/AgentTerminal";
import SupervisorChat from "./components/SupervisorChat";
const Statistics = lazy(() => import("./components/Statistics"));
import StatusBar from "./components/StatusBar";
import ErrorBoundary from "./components/ErrorBoundary";
import { useGlobalEvents } from "./hooks/useGlobalEvents";
import { useProposals } from "./hooks/useProposals";

function PanelErrorFallback(error: Error, reset: () => void) {
  return (
    <div className="h-full flex flex-col items-center justify-center p-4 text-muted-foreground gap-2">
      <p className="text-sm font-medium">Panel crashed</p>
      <p className="text-xs text-center max-w-xs">{error.message}</p>
      <button
        onClick={reset}
        className="mt-2 px-3 py-1.5 rounded-md bg-accent text-accent-foreground text-xs hover:opacity-90 transition-opacity"
      >
        Retry
      </button>
    </div>
  );
}

export default function App() {
  const [selectedTask, setSelectedTask] = useState<string | null>(null);
  const [chatOpen, setChatOpen] = useState(false);
  const [statsOpen, setStatsOpen] = useState(false);
  const { events, status: wsStatus } = useGlobalEvents();
  const { proposals, pendingCount, approve, reject } = useProposals(events);

  // Derive supervisor running state from events
  const supervisorRunning = (() => {
    for (let i = events.length - 1; i >= 0; i--) {
      const t = events[i].type;
      if (t === "supervisor_started") return true;
      if (t === "supervisor_completed" || t === "supervisor_failed") return false;
    }
    return false;
  })();

  // Mutual exclusion: opening chat closes terminal, opening terminal closes chat
  const handleSelectTask = useCallback((key: string | null) => {
    setSelectedTask(key);
    if (key) setChatOpen(false);
  }, []);

  const handleChatToggle = useCallback(() => {
    setChatOpen((prev) => {
      if (!prev) setSelectedTask(null); // close terminal when opening chat
      return !prev;
    });
  }, []);

  const handleStatsToggle = useCallback(() => {
    setStatsOpen((prev) => !prev);
  }, []);

  const rightPanelOpen = selectedTask !== null || chatOpen;

  return (
    <div className="h-screen flex flex-col bg-background text-foreground overflow-hidden">
      <StatusBar
        connectionStatus={wsStatus}
        events={events}
        pendingProposalCount={pendingCount}
        supervisorRunning={supervisorRunning}
        onChatToggle={handleChatToggle}
        chatOpen={chatOpen}
        onStatsToggle={handleStatsToggle}
        statsOpen={statsOpen}
      />
      <main className="flex-1 flex min-h-0">
        <div className={cn(
          "overflow-y-auto transition-all duration-200",
          rightPanelOpen ? "hidden lg:block lg:w-1/2" : "w-full",
        )}>
          {statsOpen ? (
            <Suspense fallback={
              <div className="flex items-center justify-center h-full text-muted-foreground">
                <span className="text-sm">Loading statistics...</span>
              </div>
            }>
              <Statistics />
            </Suspense>
          ) : (
            <Dashboard
              events={events}
              onSelectTask={handleSelectTask}
              selectedTask={selectedTask}
              proposals={proposals}
              onApproveProposal={approve}
              onRejectProposal={reject}
            />
          )}
        </div>
        {selectedTask && (
          <div className="w-full lg:w-1/2 border-l border-border flex flex-col min-h-0">
            <ErrorBoundary fallback={PanelErrorFallback}>
              <AgentTerminal
                key={selectedTask}
                taskKey={selectedTask}
                onClose={() => setSelectedTask(null)}
              />
            </ErrorBoundary>
          </div>
        )}
        {chatOpen && (
          <div className="w-full lg:w-1/2 border-l border-border flex flex-col min-h-0">
            <ErrorBoundary fallback={PanelErrorFallback}>
              <SupervisorChat onClose={() => setChatOpen(false)} />
            </ErrorBoundary>
          </div>
        )}
      </main>
    </div>
  );
}
