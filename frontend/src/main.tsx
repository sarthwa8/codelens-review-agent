import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Link, Route, Routes } from "react-router";
import { RepoListPage, RepoPage, ReviewPage } from "./pages";
import "./styles.css";

function App() {
  return (
    <BrowserRouter>
      <div className="shell">
        <header className="topbar">
          <Link to="/" className="brand">
            <span className="brand-mark" aria-hidden>
              ◎
            </span>
            CodeLens
          </Link>
          <span className="muted small">repo-aware code review, streamed live</span>
        </header>
        <main className="content">
          <Routes>
            <Route path="/" element={<RepoListPage />} />
            <Route path="/repos/:owner/:name" element={<RepoPage />} />
            <Route path="/reviews/:id" element={<ReviewPage />} />
            <Route path="*" element={<p className="empty">Page not found.</p>} />
          </Routes>
        </main>
      </div>
    </BrowserRouter>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
