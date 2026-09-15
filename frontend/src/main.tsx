import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Link, Route, Routes } from "react-router";
import { api, signInUrl, type Me } from "./api";
import { MeContext } from "./me";
import { RepoListPage, RepoPage, ReviewPage } from "./pages";
import "./styles.css";

function UserMenu({ me }: { me: Me | null }) {
  if (!me || me.auth_mode === "none") return <span className="muted small">local mode · sign-in disabled</span>;
  if (!me.authenticated) {
    return (
      <a className="button" href={signInUrl()}>
        Sign in with GitHub
      </a>
    );
  }
  return (
    <div className="user">
      {me.install_url && (
        <a className="muted small" href={me.install_url} target="_blank" rel="noreferrer">
          Add repositories
        </a>
      )}
      {me.avatar_url && <img className="avatar" src={me.avatar_url} alt="" />}
      <span className="small">{me.login}</span>
      {/* POST form: logout isn't triggerable by a cross-site link, and the cookie is SameSite=Lax. */}
      <form method="post" action="/auth/logout">
        <button className="link-button" type="submit">
          Sign out
        </button>
      </form>
    </div>
  );
}

function App() {
  const [me, setMe] = useState<Me | null>(null);
  useEffect(() => {
    api.me().then(setMe, () => setMe(null));
  }, []);

  return (
    <BrowserRouter>
      <MeContext.Provider value={me}>
        <div className="shell">
          <header className="topbar">
            <Link to="/" className="brand">
              <span className="brand-mark" aria-hidden>
                ◎
              </span>
              CodeLens
            </Link>
            <span className="muted small tagline">repo-aware code review, streamed live</span>
            <UserMenu me={me} />
          </header>
          <main className="content">
            {me && me.auth_mode === "github" && !me.authenticated ? (
              <div className="empty">
                <h2>Sign in to see reviews</h2>
                <p>CodeLens shows the reviews for repositories your GitHub account can read.</p>
                <a className="button" href={signInUrl()}>
                  Sign in with GitHub
                </a>
              </div>
            ) : (
              <Routes>
                <Route path="/" element={<RepoListPage />} />
                <Route path="/repos/:owner/:name" element={<RepoPage />} />
                <Route path="/reviews/:id" element={<ReviewPage />} />
                <Route path="*" element={<p className="empty">Page not found.</p>} />
              </Routes>
            )}
          </main>
        </div>
      </MeContext.Provider>
    </BrowserRouter>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
