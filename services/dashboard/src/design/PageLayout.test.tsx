/**
 * Regression test for triage finding 82463a9a: Tasks sidebar nav link
 * was navigating to dead /tasks route (App.tsx has no /tasks route,
 * only /tasks/:taskId). Fix adds optional href field to nav entries;
 * Tasks now has href=/ so clicking navigates to Overview, while path=/tasks
 * still drives startsWith active-state detection for /tasks/:id pages.
 */
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { PageLayout } from './PageLayout';

describe('PageLayout nav fix (triage finding 82463a9a)', () => {
  it('Tasks nav link has href=/ when navigating to /tasks/:taskId', () => {
    render(
      <MemoryRouter initialEntries={['/tasks/abc123']}>
        <PageLayout title="Task ABC123">Test content</PageLayout>
      </MemoryRouter>,
    );

    const tasksLink = screen.getByRole('link', { name: /Tasks/i });
    expect(tasksLink).toHaveAttribute('href', '/');
  });

  it('Overview nav link has href=/', () => {
    render(
      <MemoryRouter initialEntries={['/']}>
        <PageLayout title="Overview">Test content</PageLayout>
      </MemoryRouter>,
    );

    const overviewLink = screen.getByRole('link', { name: /Overview/i });
    expect(overviewLink).toHaveAttribute('href', '/');
  });

  it('Tasks nav item is highlighted when at /tasks/:taskId', () => {
    render(
      <MemoryRouter initialEntries={['/tasks/abc123']}>
        <PageLayout title="Task ABC123">Test content</PageLayout>
      </MemoryRouter>,
    );

    const tasksLink = screen.getByRole('link', { name: /Tasks/i });
    expect(tasksLink).toHaveStyle('color: var(--tm-t1)');
    expect(tasksLink).toHaveStyle('background: var(--tm-surface-2)');
    expect(tasksLink).toHaveStyle('borderLeft: 2px solid var(--tm-warn)');
  });

  it('Tasks nav item is not highlighted when at /', () => {
    render(
      <MemoryRouter initialEntries={['/']}>
        <PageLayout title="Overview">Test content</PageLayout>
      </MemoryRouter>,
    );

    const tasksLink = screen.getByRole('link', { name: /Tasks/i });
    expect(tasksLink).toHaveStyle('color: var(--tm-t3)');
    expect(tasksLink).toHaveStyle('background: transparent');
    expect(tasksLink).toHaveStyle('borderLeft: 2px solid transparent');
  });
});
