import { Suspense, lazy } from 'react';
import { BrowserRouter, Link, Navigate, Route, Routes } from 'react-router-dom';

const Home = lazy(() => import('./pages/Home'));
const Avatar = lazy(() => import('./pages/Avatar'));
const Pulse = lazy(() => import('./pages/Pulse'));
const Settings = lazy(() => import('./pages/Settings'));

export default function App() {
  return (
    <BrowserRouter>
      <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
        <Nav />
        <div style={{ flex: 1, minHeight: 0 }}>
          <Suspense fallback={<Loader />}>
            <Routes>
              <Route path="/" element={<Home />} />
              <Route path="/avatar" element={<Avatar />} />
              <Route path="/pulse" element={<Pulse />} />
              <Route path="/settings" element={<Settings />} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Routes>
          </Suspense>
        </div>
      </div>
    </BrowserRouter>
  );
}

function Nav() {
  return (
    <nav
      style={{
        display: 'flex',
        gap: 16,
        // Slightly more vertical padding so the title doesn't touch the
        // OS title bar in environments where the system extends chrome
        // into the content rect (Windows 11 Mica, some Tauri setups).
        padding: '14px 24px',
        borderBottom: '1px solid #1f2227',
        background: '#0e1014',
        alignItems: 'center',
        flexShrink: 0,
      }}
    >
      <Link to="/" style={{ color: '#fff', fontWeight: 600, textDecoration: 'none', fontSize: 14 }}>
        zeroclaw·companion
      </Link>
      <span style={{ flex: 1 }} />
      <NavLink to="/" label="Home" />
      <NavLink to="/avatar" label="Avatar" />
      <NavLink to="/pulse" label="Pulse" />
      <NavLink to="/settings" label="Settings" />
    </nav>
  );
}

function NavLink({ to, label }: { to: string; label: string }) {
  return (
    <Link to={to} style={{ color: '#aaa', textDecoration: 'none', fontSize: 14 }}>
      {label}
    </Link>
  );
}

function Loader() {
  return (
    <div style={{ height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
      <div
        style={{
          width: 32,
          height: 32,
          border: '2px solid #2a2d33',
          borderTopColor: '#3b82f6',
          borderRadius: '50%',
          animation: 'spin 0.8s linear infinite',
        }}
      />
      <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
    </div>
  );
}
