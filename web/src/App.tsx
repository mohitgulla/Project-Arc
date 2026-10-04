import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";
import { BrowserRouter, Route, Routes } from "react-router-dom";

import { Shell } from "./components/Shell";
import { SettingsPanel } from "./components/Shell";
import { Card } from "./components/Card";
import { SettingsProvider } from "./lib/settings";
import { KitchenSink } from "./pages/KitchenSink";
import { OverviewPage } from "./pages/Overview";
import { PositionsPage } from "./pages/Positions";
import { TradeDetailRoute } from "./pages/TradeDetail";
import { TradesPage } from "./pages/Trades";
import { ContextEntryPage, OpsConfigPage, OpsPage, RunDetailPage } from "./pages/Ops";
import { NotFoundPage } from "./routes/pages";
import { PerformancePage } from "./pages/Performance";
import { ExperimentsPage } from "./pages/Experiments";

function makeClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 15_000,
        retry: 1,
        refetchIntervalInBackground: false,
        refetchOnWindowFocus: true,
      },
    },
  });
}

const SettingsPage = () => (
  <Card title="Settings">
    <SettingsPanel />
  </Card>
);

export function App() {
  const [client] = useState(makeClient);
  return (
    <QueryClientProvider client={client}>
      <SettingsProvider>
        <BrowserRouter>
          <Routes>
            <Route element={<Shell />}>
              <Route index element={<OverviewPage />} />
              <Route path="trades" element={<TradesPage />}>
                <Route path=":hash" element={<TradeDetailRoute />} />
              </Route>
              <Route path="positions/*" element={<PositionsPage />} />
              <Route path="performance/*" element={<PerformancePage />} />
              <Route path="experiments/:experimentId?" element={<ExperimentsPage />} />
              <Route path="ops" element={<OpsPage />} />
              <Route path="ops/config" element={<OpsConfigPage />} />
              <Route path="ops/runs/:runId" element={<RunDetailPage />} />
              <Route path="ops/context/:entryId" element={<ContextEntryPage />} />
              <Route path="settings" element={<SettingsPage />} />
              <Route path="kitchen-sink" element={<KitchenSink />} />
              <Route path="*" element={<NotFoundPage />} />
            </Route>
          </Routes>
        </BrowserRouter>
      </SettingsProvider>
    </QueryClientProvider>
  );
}
