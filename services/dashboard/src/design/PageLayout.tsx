/**
 * <PageLayout> — the single page wrapper.
 *
 * Per DESIGN.md mandatory rule #3: "One PageLayout wrapper for every
 * page. It owns its own loading / error / not-found states. Detail pages
 * do not reinvent the wrapper, ever." This is the bunkhouse antipattern
 * we are explicitly not inheriting (16 occurrences of hand-rolled
 * `min-h-screen bg-gray-100 p-8` chrome that drifted across detail pages).
 */

import type { ReactNode } from 'react';
import { Activity, Box, Search } from 'lucide-react';
import { Link, useLocation } from 'react-router-dom';

interface NavEntry {
  path: string;
  label: string;
  icon: ReactNode;
  href?: string;
}

interface PageLayoutProps {
  title?: ReactNode;
  breadcrumb?: ReactNode;
  actions?: ReactNode;
  /** Freshness chip (<ConnectionAffordance>). Rendered in the top bar. */
  freshness?: ReactNode;
  children: ReactNode;
  /** When set, replaces children with a skeleton. */
  loading?: boolean;
  /** When set, replaces children with an error panel. */
  error?: Error | null;
}

export function PageLayout({
  title,
  breadcrumb,
  actions,
  freshness,
  children,
  loading,
  error,
}: PageLayoutProps) {
  return (
    <div
      className="tm"
      style={{
        display: 'grid',
        gridTemplateColumns: '176px 1fr',
        height: '100%',
        minHeight: '100%',
        background: 'var(--tm-bg)',
      }}
    >
      <Sidebar />
      <div style={{ display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
        <TopBar freshness={freshness} />
        <div style={{ flex: 1, overflow: 'auto' }} className="tm-scroll">
          <header
            style={{
              padding: '14px 24px 12px',
              borderBottom: '1px solid var(--tm-border)',
              display: 'flex',
              alignItems: 'flex-end',
              justifyContent: 'space-between',
              gap: 16,
              flexWrap: 'wrap',
            }}
          >
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              {breadcrumb}
              {title && (
                <h1
                  style={{
                    margin: 0,
                    fontSize: 18,
                    fontWeight: 500,
                    letterSpacing: 0.5,
                    textTransform: 'uppercase',
                    fontFamily: 'var(--tm-mono)',
                    color: 'var(--tm-t1)',
                  }}
                >
                  {title}
                </h1>
              )}
            </div>
            {actions}
          </header>
          <main style={{ padding: '16px 24px 32px' }}>
            {error ? <ErrorPanel error={error} /> : loading ? <Skeleton /> : children}
          </main>
        </div>
      </div>
    </div>
  );
}

function ErrorPanel({ error }: { error: Error }) {
  return (
    <div
      style={{
        border: '1px solid var(--tm-danger-edge)',
        background: 'var(--tm-danger-bg)',
        color: 'var(--tm-danger-fg)',
        padding: 16,
        fontFamily: 'var(--tm-mono)',
        fontSize: 12,
        borderRadius: 2,
      }}
    >
      <div style={{ fontWeight: 600, marginBottom: 4 }}>// error</div>
      <div>{error.message}</div>
    </div>
  );
}

function Skeleton() {
  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        gap: 12,
        opacity: 0.55,
      }}
    >
      {Array.from({ length: 6 }).map((_, i) => (
        <div
          key={i}
          style={{
            height: 38,
            background: 'var(--tm-surface)',
            border: '1px solid var(--tm-border)',
            borderRadius: 2,
            animation: 'tm-pulse-soft 1.6s ease-in-out infinite',
          }}
        />
      ))}
    </div>
  );
}

const NAV: NavEntry[] = [
  { path: '/', label: 'Overview', icon: <Activity size={14} /> },
  { path: '/tasks', label: 'Tasks', icon: <Box size={14} />, href: '/' },
];

function Sidebar() {
  const location = useLocation();
  // Match exact for "/", prefix for everything else.
  const isActive = (path: string) =>
    path === '/' ? location.pathname === '/' : location.pathname.startsWith(path);
  return (
    <aside
      style={{
        background: 'var(--tm-bg)',
        borderRight: '1px solid var(--tm-border)',
        padding: '14px 8px',
        display: 'flex',
        flexDirection: 'column',
        gap: 2,
      }}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 9,
          padding: '6px 10px 14px',
          borderBottom: '1px solid var(--tm-border)',
          marginBottom: 8,
        }}
      >
        <div
          style={{
            width: 22,
            height: 22,
            borderRadius: 4,
            background: 'var(--tm-t1)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            color: 'var(--tm-bg)',
            fontWeight: 700,
            fontFamily: 'var(--tm-mono)',
            fontSize: 13,
          }}
        >
          T
        </div>
        <div style={{ display: 'flex', flexDirection: 'column' }}>
          <span style={{ fontSize: 13, fontWeight: 600, letterSpacing: 0.2 }}>Treadmill</span>
          <span
            style={{
              fontSize: 9.5,
              color: 'var(--tm-t4)',
              fontFamily: 'var(--tm-mono)',
              letterSpacing: 0.5,
              textTransform: 'uppercase',
            }}
          >
            operator
          </span>
        </div>
      </div>
      {NAV.map((n) => {
        const active = isActive(n.path);
        return (
          <Link
            key={n.path}
            to={n.href ?? n.path}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 10,
              padding: '7px 10px',
              borderRadius: 2,
              color: active ? 'var(--tm-t1)' : 'var(--tm-t3)',
              background: active ? 'var(--tm-surface-2)' : 'transparent',
              fontSize: 13,
              textDecoration: 'none',
              fontFamily: 'var(--tm-mono)',
              letterSpacing: 0.2,
              borderLeft: active ? '2px solid var(--tm-warn)' : '2px solid transparent',
            }}
          >
            <span style={{ opacity: active ? 1 : 0.7 }}>{n.icon}</span>
            {n.label}
          </Link>
        );
      })}
      <div style={{ flex: 1 }} />
      <div
        style={{
          padding: '8px 10px',
          fontSize: 10.5,
          fontFamily: 'var(--tm-mono)',
          color: 'var(--tm-t4)',
          borderTop: '1px solid var(--tm-border)',
          marginTop: 8,
        }}
      >
        <div style={{ display: 'flex', justifyContent: 'space-between' }}>
          <span>v0.1.0</span>
          <span>local</span>
        </div>
      </div>
    </aside>
  );
}

function TopBar({ freshness }: { freshness?: ReactNode }) {
  return (
    <div
      style={{
        height: 44,
        borderBottom: '1px solid var(--tm-border)',
        display: 'flex',
        alignItems: 'center',
        padding: '0 24px',
        gap: 16,
        background: 'var(--tm-bg)',
      }}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          flex: 1,
          maxWidth: 380,
          background: 'var(--tm-surface-2)',
          borderRadius: 2,
          border: '1px solid var(--tm-border)',
          padding: '5px 10px',
          color: 'var(--tm-t3)',
          fontSize: 12.5,
        }}
      >
        <Search size={13} />
        <span style={{ color: 'var(--tm-t4)' }}>jump to task / plan / repo</span>
        <span
          style={{
            marginLeft: 'auto',
            fontFamily: 'var(--tm-mono)',
            color: 'var(--tm-t4)',
            fontSize: 11,
          }}
        >
          ⌘K
        </span>
      </div>
      <div style={{ flex: 1 }} />
      {freshness}
    </div>
  );
}
