/**
 * Smoke-test the canonical formatters.
 *
 * Per DESIGN.md "Observability discipline": every metric on the dashboard
 * routes through these formatters, so a regression here drifts the entire
 * UI's numeric vocabulary at once. We pin the edge cases — the breakpoints
 * between unit suffixes — so a well-meaning tweak ("just shave a digit")
 * has to update the test alongside.
 */
import { describe, expect, it } from 'vitest';
import { fmt } from './fmt';

describe('fmt.usd', () => {
  it('handles null and zero', () => {
    expect(fmt.usd(null)).toBe('—');
    expect(fmt.usd(undefined)).toBe('—');
    expect(fmt.usd(0)).toBe('$0.00');
  });
  it('honors the sub-cent floor', () => {
    expect(fmt.usd(0.001)).toBe('<$0.01');
  });
  it('formats normal dollar amounts to two decimals', () => {
    expect(fmt.usd(1.5)).toBe('$1.50');
    expect(fmt.usd(24.18)).toBe('$24.18');
  });
  it('drops decimals between $1k and $10k, then switches to "k"', () => {
    expect(fmt.usd(1234)).toBe('$1234');
    expect(fmt.usd(9_999)).toBe('$9999');
    expect(fmt.usd(10_000)).toBe('$10.0k');
    expect(fmt.usd(24_180)).toBe('$24.2k');
  });
});

describe('fmt.duration', () => {
  it('handles null / NaN / negative as gracefully as it can', () => {
    expect(fmt.duration(null)).toBe('—');
    expect(fmt.duration(undefined)).toBe('—');
    expect(fmt.duration(NaN)).toBe('—');
    expect(fmt.duration(-5)).toBe('0s');
  });
  it('renders seconds, then minutes, then hours, then days', () => {
    expect(fmt.duration(0)).toBe('0s');
    expect(fmt.duration(45)).toBe('45s');
    expect(fmt.duration(60)).toBe('1m');
    expect(fmt.duration(272)).toBe('4m 32s');
    expect(fmt.duration(3600)).toBe('1h');
    expect(fmt.duration(8280)).toBe('2h 18m');
    expect(fmt.duration(86400)).toBe('1d 0h');
    expect(fmt.duration(273600)).toBe('3d 4h');
  });
});

describe('fmt.age', () => {
  it('handles missing dates', () => {
    expect(fmt.age(null)).toBe('—');
    expect(fmt.age(undefined)).toBe('—');
  });
  it('rounds wall-clock age into the duration vocabulary', () => {
    const d = new Date(Date.now() - 12 * 60_000);
    expect(fmt.age(d)).toMatch(/^11m \d+s$|^12m$/);
  });
});

describe('fmt.tokens', () => {
  it('switches units cleanly at the breakpoints', () => {
    expect(fmt.tokens(null)).toBe('—');
    expect(fmt.tokens(0)).toBe('0');
    expect(fmt.tokens(999)).toBe('999');
    expect(fmt.tokens(1_500)).toBe('1.5k');
    expect(fmt.tokens(100_000)).toBe('100k');
    expect(fmt.tokens(1_842_103)).toBe('1.84M');
  });
});

describe('fmt.sha and fmt.id', () => {
  it('shortens to the configured width', () => {
    expect(fmt.sha('abcdef0123456789')).toBe('abcdef0');
    expect(fmt.sha('abcdef0123456789', 4)).toBe('abcd');
    expect(fmt.id('tsk_8f3a2b1c_extra')).toBe('tsk_8f3a2b1c');
    expect(fmt.id(null)).toBe('—');
  });
});
