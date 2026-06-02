/**
 * <Button> — the canonical action affordance.
 *
 * Three kinds: `default` (neutral), `primary` (inverted), `destructive`
 * (danger tone, e.g. Cancel-task). Destructive actions are visually
 * distinguished per DESIGN.md.
 */

import type { CSSProperties, MouseEvent, ReactNode } from 'react';

const HEIGHTS = { sm: 26, md: 30, lg: 34 } as const;
type ButtonSize = keyof typeof HEIGHTS;

interface ButtonProps {
  children: ReactNode;
  kind?: 'default' | 'primary' | 'destructive';
  size?: ButtonSize;
  onClick?: (e: MouseEvent<HTMLButtonElement>) => void;
  disabled?: boolean;
  iconLeft?: ReactNode;
  iconRight?: ReactNode;
  title?: string;
  style?: CSSProperties;
  type?: 'button' | 'submit' | 'reset';
  'aria-label'?: string;
}

export function Button({
  children,
  kind = 'default',
  size = 'md',
  onClick,
  disabled,
  iconLeft,
  iconRight,
  title,
  style,
  type = 'button',
  'aria-label': ariaLabel,
}: ButtonProps) {
  const isDestructive = kind === 'destructive';
  const isPrimary = kind === 'primary';
  const h = HEIGHTS[size];

  const base: CSSProperties = {
    height: h,
    padding: `0 ${size === 'sm' ? 10 : 12}px`,
    fontFamily: 'var(--tm-mono)',
    fontSize: size === 'sm' ? 11.5 : 12.5,
    fontWeight: 500,
    letterSpacing: 0.3,
    borderRadius: 2,
    border: '1px solid var(--tm-border-2)',
    background: 'var(--tm-surface-2)',
    color: 'var(--tm-t1)',
    display: 'inline-flex',
    alignItems: 'center',
    gap: 6,
    cursor: disabled ? 'not-allowed' : 'pointer',
    opacity: disabled ? 0.5 : 1,
    textTransform: 'uppercase',
    transition: 'background 0.12s, border-color 0.12s',
  };

  if (isPrimary) {
    base.background = 'var(--tm-t1)';
    base.color = 'var(--tm-bg)';
    base.borderColor = 'var(--tm-t1)';
  }
  if (isDestructive) {
    base.background = 'transparent';
    base.color = 'var(--tm-danger-fg)';
    base.borderColor = 'var(--tm-danger-edge)';
  }

  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      title={title}
      aria-label={ariaLabel}
      style={{ ...base, ...style }}
      onMouseEnter={(e) => {
        if (disabled) return;
        e.currentTarget.style.background = isPrimary
          ? 'var(--tm-t1)'
          : isDestructive
            ? 'var(--tm-danger-bg)'
            : 'var(--tm-hover)';
      }}
      onMouseLeave={(e) => {
        if (disabled) return;
        e.currentTarget.style.background = isPrimary
          ? 'var(--tm-t1)'
          : isDestructive
            ? 'transparent'
            : 'var(--tm-surface-2)';
      }}
    >
      {iconLeft}
      {children}
      {iconRight}
    </button>
  );
}
