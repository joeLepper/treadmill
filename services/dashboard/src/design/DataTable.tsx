/**
 * <DataTable> — the ONE table for the whole UI.
 *
 * Per DESIGN.md mandatory rule #2: "Exactly one DataTable component.
 * Delete every hand-rolled `<table>`." Hand-rolled tables are how the
 * bunkhouse dashboard drifted across pages; we eat the slightly heavier
 * abstraction up front.
 *
 * Console-flavored: monospace uppercase column headers, sticky on scroll,
 * row click navigates (no inline edit affordances per rule #9).
 * `flashIds` triggers a 1.4s background flash on rows that just changed.
 */

import type { CSSProperties, ReactNode } from 'react';

export interface Column<R> {
  key: keyof R | string;
  title: ReactNode;
  /** Render override; defaults to `row[key]`. */
  render?: (row: R) => ReactNode;
  align?: 'left' | 'right' | 'center';
  /** Render the cell with the monospace font (tabular numbers, IDs, etc.). */
  mono?: boolean;
  /** Render the cell with `--tm-t3` (secondary). */
  muted?: boolean;
  /** Allow wrapping; default false. */
  wrap?: boolean;
  width?: string | number;
}

interface DataTableProps<R> {
  columns: Column<R>[];
  rows: R[];
  onRowClick?: (row: R) => void;
  density?: 'comfortable' | 'dense';
  emptyState?: ReactNode;
  getRowKey?: (row: R) => string | number;
  /** Row keys whose backgrounds should flash on render — call when WS pushes a change. */
  flashIds?: Set<string | number>;
}

export function DataTable<R>({
  columns,
  rows,
  onRowClick,
  density = 'comfortable',
  emptyState,
  getRowKey,
  flashIds = new Set(),
}: DataTableProps<R>) {
  const rowH = density === 'dense' ? 32 : 38;
  const headPad = density === 'dense' ? '6px 10px' : '8px 12px';
  const cellPad = density === 'dense' ? '6px 10px' : '8px 12px';

  return (
    <div
      style={{
        border: '1px solid var(--tm-border)',
        borderRadius: 2,
        overflow: 'hidden',
        background: 'transparent',
      }}
    >
      <table
        style={{
          width: '100%',
          borderCollapse: 'collapse',
          tableLayout: 'auto',
          fontFamily: 'var(--tm-sans)',
          fontSize: 12.5,
        }}
      >
        <thead>
          <tr
            style={{
              background: 'var(--tm-bg)',
              borderBottom: '1px solid var(--tm-border)',
            }}
          >
            {columns.map((c, i) => (
              <th
                key={i}
                style={{
                  textAlign: c.align ?? 'left',
                  padding: headPad,
                  fontFamily: 'var(--tm-mono)',
                  fontSize: 10.5,
                  fontWeight: 500,
                  letterSpacing: 0.7,
                  color: 'var(--tm-t3)',
                  textTransform: 'uppercase',
                  whiteSpace: 'nowrap',
                  width: c.width,
                  position: 'sticky',
                  top: 0,
                  background: 'var(--tm-bg)',
                  zIndex: 1,
                }}
              >
                {c.title}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 && (
            <tr>
              <td
                colSpan={columns.length}
                style={{ padding: 32, textAlign: 'center', color: 'var(--tm-t3)' }}
              >
                {emptyState ?? 'Nothing here.'}
              </td>
            </tr>
          )}
          {rows.map((r, ri) => {
            const key = getRowKey ? getRowKey(r) : ri;
            const flash = flashIds.has(key);
            return (
              <tr
                key={key}
                onClick={onRowClick ? () => onRowClick(r) : undefined}
                style={{
                  cursor: onRowClick ? 'pointer' : 'default',
                  borderBottom:
                    ri === rows.length - 1 ? 'none' : '1px solid var(--tm-border)',
                  height: rowH,
                  animation: flash ? 'tm-flash-row 1.4s ease-out 1' : 'none',
                  transition: 'background 0.12s',
                }}
                onMouseEnter={(e) => {
                  e.currentTarget.style.background = 'var(--tm-hover)';
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.background = '';
                }}
              >
                {columns.map((c, ci) => {
                  const cellStyle: CSSProperties = {
                    padding: cellPad,
                    textAlign: c.align ?? 'left',
                    color: c.muted ? 'var(--tm-t3)' : 'var(--tm-t1)',
                    fontFamily: c.mono ? 'var(--tm-mono)' : 'var(--tm-sans)',
                    fontSize: c.mono ? 12 : 12.5,
                    whiteSpace: c.wrap ? 'normal' : 'nowrap',
                    verticalAlign: 'middle',
                  };
                  return (
                    <td key={ci} style={cellStyle}>
                      {c.render
                        ? c.render(r)
                        : (r as Record<string, ReactNode>)[c.key as string]}
                    </td>
                  );
                })}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
