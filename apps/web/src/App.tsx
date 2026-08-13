import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { NavLink, Route, Routes, useLocation } from "react-router-dom";
import { clearKey, storedIdentity, storedKey } from "./api/client";
import { ComposePage } from "./routes/ComposePage";
import { ConfirmDialog } from "./routes/ConfirmDialog";
import { ConnectionsPage } from "./routes/ConnectionsPage";
import { HistoryPage } from "./routes/HistoryPage";
import { SearchPage } from "./routes/SearchPage";
import { SendDetailPage } from "./routes/SendDetailPage";
import { SignInPage } from "./routes/SignInPage";

/**
 * The console. A **pure consumer** of the documented API: it holds no business
 * rules and has no privileged path.
 *
 * A grep of this directory for ranking, error classification or any local
 * `canSend` derivation returns nothing, and that is not a promise —
 * `tests/test_web_boundary.py` greps this tree and fails on a hit, mirroring the
 * source-agnosticism test on the backend. Whether a send may proceed, whether an
 * error is retryable and how results rank are all read from API responses.
 *
 * Navigation is a bottom bar on a phone and a header row from 40rem up. That is
 * a thumb decision rather than a fashion: every primary destination has to be
 * reachable one-handed, and the confirm sheet deliberately covers the bar —
 * while the gate is open, there is nowhere else to be.
 */
export default function App() {
  const [signedIn, setSignedIn] = useState(() => Boolean(storedKey()));
  const queryClient = useQueryClient();
  const location = useLocation();

  if (!signedIn) {
    return (
      <div className="app">
        <header className="topbar topbar-bare">
          <Brand />
        </header>
        <main className="app-main">
          <SignInPage onSignedIn={() => setSignedIn(true)} />
        </main>
      </div>
    );
  }

  // The gate takes the whole screen on a phone, so the chrome around it would
  // only be somewhere else to tap.
  const gateOpen = location.pathname.startsWith("/confirm/");

  return (
    <div className="app" data-gate={gateOpen ? "open" : "closed"}>
      <header className="topbar">
        <Brand />
        <div className="topbar-right">
          <span className="identity" title="Signed in as">
            {storedIdentity() ?? "signed in"}
          </span>
          <button
            type="button"
            className="button button-quiet"
            onClick={() => {
              clearKey();
              queryClient.clear();
              setSignedIn(false);
            }}
          >
            Sign out
          </button>
        </div>
        <nav className="topnav">
          <Nav />
        </nav>
      </header>

      <main className="app-main">
        <Routes>
          <Route path="/" element={<SearchPage />} />
          <Route path="/search/:searchId" element={<SearchPage />} />
          <Route path="/compose" element={<ComposePage />} />
          <Route path="/confirm/:draftId" element={<ConfirmDialog />} />
          <Route path="/history" element={<HistoryPage />} />
          <Route path="/history/sends/:sendId" element={<SendDetailPage />} />
          <Route path="/connections" element={<ConnectionsPage />} />
        </Routes>
      </main>

      <nav className="tabbar" aria-label="Primary">
        <Nav />
      </nav>
    </div>
  );
}

function Brand() {
  return (
    <div className="brand">
      <span className="brand-mark" aria-hidden="true" />
      <span className="brand-name">Unified Search &amp; Safe Send</span>
    </div>
  );
}

const LINKS = [
  { to: "/", label: "Search", end: true },
  { to: "/compose", label: "Compose", end: false },
  { to: "/history", label: "History", end: false },
  { to: "/connections", label: "Accounts", end: false },
];

function Nav() {
  return (
    <>
      {LINKS.map((link) => (
        <NavLink
          key={link.to}
          to={link.to}
          end={link.end}
          className={({ isActive }) => (isActive ? "navlink navlink-on" : "navlink")}
        >
          {link.label}
        </NavLink>
      ))}
    </>
  );
}
