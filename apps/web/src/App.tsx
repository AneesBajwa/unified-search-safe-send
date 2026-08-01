import { NavLink, Route, Routes } from "react-router-dom";
import { ComposePage } from "./routes/ComposePage";
import { ConfirmDialog } from "./routes/ConfirmDialog";
import { ConnectionsPage } from "./routes/ConnectionsPage";
import { HistoryPage } from "./routes/HistoryPage";
import { SearchPage } from "./routes/SearchPage";
import { SendDetailPage } from "./routes/SendDetailPage";
import "./App.css";

/**
 * The console. A **pure consumer** of the documented API: it holds no business
 * rules and has no privileged path.
 *
 * Deliberately unstyled beyond the minimum this phase — the point is that the
 * whole product loop works end to end against fake adapters and a fake
 * provider, with no OAuth anywhere. Polish is phase 5, and it polishes these
 * same components rather than replacing them.
 *
 * A grep of this directory for ranking or error-classification logic should
 * return nothing. Whether a send may proceed, whether an error is retryable and
 * how results rank are all read from API responses.
 */
export default function App() {
  return (
    <div className="app">
      <header className="topbar">
        <h1>Unified Search &amp; Safe Send</h1>
        <nav>
          <NavLink to="/" end>
            Search
          </NavLink>
          <NavLink to="/compose">Compose</NavLink>
          <NavLink to="/history">History</NavLink>
          <NavLink to="/connections">Connections</NavLink>
        </nav>
      </header>

      <main>
        <Routes>
          <Route path="/" element={<SearchPage />} />
          <Route path="/compose" element={<ComposePage />} />
          <Route path="/compose/:resultId" element={<ComposePage />} />
          <Route path="/confirm/:draftId" element={<ConfirmDialog />} />
          <Route path="/history" element={<HistoryPage />} />
          <Route path="/history/sends/:sendId" element={<SendDetailPage />} />
          <Route path="/connections" element={<ConnectionsPage />} />
        </Routes>
      </main>
    </div>
  );
}
