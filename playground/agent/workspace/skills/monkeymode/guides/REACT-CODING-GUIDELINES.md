# React Coding Guidelines - Enterprise Grade

**Version:** 1.0  
**Last Updated:** 2026  
**Target:** Production React applications requiring high quality, security, and maintainability

---

## Table of Contents

1. [Philosophy](#philosophy)
2. [Code Style](#code-style)
3. [Type Safety](#type-safety)
4. [Documentation](#documentation)
5. [Architecture](#architecture)
6. [Error Handling](#error-handling)
7. [Testing](#testing)
8. [Security](#security)
9. [Performance](#performance)
10. [Dependencies](#dependencies)
11. [Logging](#logging)
12. [Code Review](#code-review)
13. [Tooling](#tooling)

---

## Philosophy

Follow **component-driven architecture** with a Server Components-first mindset:
- Server Components at the root, Client Components at the leaves
- Composition over inheritance — prefer props and children over class hierarchies
- React Compiler handles memoization — write idiomatic code, not optimized code
- Separate server state (data fetching) from client state (UI interactions)
- Colocation — keep related code together by feature, not by type

**Core Principles:**
- Write code for humans first, machines second
- Optimize for maintainability over cleverness
- Test-driven development (TDD) is mandatory
- Security is not optional
- Performance matters, but measure before optimizing
- When in doubt, prefer consistency within a file over strict rule adherence
- Keep components small and focused — extract when complexity grows

---

## Code Style

### Base Standard

Follow the **Airbnb React/JSX Style Guide** conventions adapted for modern React, with **ESLint flat config** and **Prettier** for enforcement:

**Formatting:**
- Line length: 100 characters
- Indentation: 2 spaces (never tabs)
- Encoding: UTF-8
- Line endings: LF (Unix style)
- Semicolons: always required
- Quotes: single quotes for TypeScript/JavaScript, double quotes for JSX attributes
- Trailing commas: always (ES5+)

**File Naming:**
```
# kebab-case for all files
user-profile.tsx            # Component
user-profile.test.tsx       # Unit tests
use-user-data.ts            # Custom hook
user.types.ts               # Type definitions
user.utils.ts               # Utility functions
user.constants.ts           # Constants

# Index files for public API of a feature
index.ts                    # Re-exports public API
```

**Naming Conventions:**
```typescript
// Components - PascalCase
export function UserProfile({ user }: UserProfileProps) { }
export const UserCard: React.FC<UserCardProps> = ({ user }) => { };

// Hooks - camelCase with "use" prefix
export function useUserData(userId: string) { }
export function useDebounce<T>(value: T, delay: number) { }

// Interfaces and Types - PascalCase (no "I" prefix)
export interface UserConfig { }
export type ApiResponse<T> = { data: T; error: string | null };

// Props - PascalCase with "Props" suffix
export interface UserProfileProps {
  user: User;
  onEdit: (user: User) => void;
}

// Variables, functions - camelCase
const userName = 'John';
function calculateTotal(items: CartItem[]): number { return 0; }

// Constants - UPPER_SNAKE_CASE for true compile-time constants
export const MAX_RETRY_COUNT = 3;
export const API_BASE_URL = 'https://api.example.com';

// Enums - PascalCase for name, PascalCase for members
enum UserRole {
  Admin = 'ADMIN',
  User = 'USER',
  Moderator = 'MODERATOR',
}

// Event handlers - "handle" prefix in components, "on" prefix in props
<Button onClick={handleSubmit} />
interface FormProps { onSubmit: (data: FormData) => void; }
```

**Import Ordering:**
```typescript
// 1. React and framework
import { useState, useCallback } from 'react';
import { useRouter } from 'next/navigation';

// 2. Third-party libraries
import { useQuery } from '@tanstack/react-query';
import { z } from 'zod';

// 3. Internal shared modules
import { Button } from '@/components/ui/button';
import { useAuth } from '@/hooks/use-auth';

// 4. Local feature modules
import { UserCard } from './user-card';
import type { User } from './user.types';

// 5. Styles and assets
import styles from './user-profile.module.css';
```

**Component File Structure:**
```typescript
// 1. 'use client' directive (only when needed)
'use client';

// 2. Imports (ordered as above)

// 3. Types/interfaces (if not in separate .types.ts file)
interface UserListProps { /* ... */ }

// 4. Constants local to this module

// 5. Component definition (named export preferred)
export function UserList({ users, onSelect }: UserListProps) {
  // hooks first
  // derived state / computations
  // event handlers
  // effects
  // render
}

// 6. Helper components (private to this file)
function UserRow({ user }: { user: User }) { /* ... */ }
```

**Avoid:**
- Default exports (use named exports for better refactoring and IDE support)
- Wildcard imports: `import * as _ from 'lodash'`
- Barrel files that re-export everything (causes tree-shaking issues and circular deps)
- Overly generic file names: `helpers.ts`, `utils.ts`, `common.ts`
- Using `any` without explicit justification in a comment
- Inline styles for anything beyond one-off dynamic values

---

## Type Safety

### TypeScript Strict Mode

**Enable all strict checks in `tsconfig.json`:**
```json
{
  "compilerOptions": {
    "strict": true,
    "noImplicitAny": true,
    "strictNullChecks": true,
    "strictFunctionTypes": true,
    "noImplicitReturns": true,
    "noFallthroughCasesInSwitch": true,
    "noUncheckedIndexedAccess": true,
    "forceConsistentCasingInFileNames": true,
    "jsx": "react-jsx",
    "moduleResolution": "bundler",
    "target": "ES2022",
    "lib": ["DOM", "DOM.Iterable", "ES2022"]
  }
}
```

**Component Props:**
```typescript
// [GOOD] Typed props with explicit interface
interface UserCardProps {
  user: User;
  variant?: 'compact' | 'detailed';
  onSelect: (user: User) => void;
}

export function UserCard({ user, variant = 'compact', onSelect }: UserCardProps) {
  return (
    <div onClick={() => onSelect(user)}>
      {variant === 'detailed' ? <DetailedView user={user} /> : <CompactView user={user} />}
    </div>
  );
}

// [BAD] Inline prop types for non-trivial components
export function UserCard({ user, variant, onSelect }: {
  user: any; variant: string; onSelect: Function;
}) { /* ... */ }
```

**Generic Components:**
```typescript
// [GOOD] Generic list component
interface ListProps<T> {
  items: T[];
  renderItem: (item: T) => React.ReactNode;
  keyExtractor: (item: T) => string;
  emptyMessage?: string;
}

export function List<T>({ items, renderItem, keyExtractor, emptyMessage }: ListProps<T>) {
  if (items.length === 0) {
    return <p>{emptyMessage ?? 'No items found.'}</p>;
  }

  return (
    <ul>
      {items.map(item => (
        <li key={keyExtractor(item)}>{renderItem(item)}</li>
      ))}
    </ul>
  );
}
```

**Discriminated unions for state:**
```typescript
// [GOOD] Exhaustive state handling
type AsyncState<T> =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'success'; data: T }
  | { status: 'error'; error: string };

function UserList() {
  const [state, setState] = useState<AsyncState<User[]>>({ status: 'idle' });

  switch (state.status) {
    case 'idle':
      return null;
    case 'loading':
      return <Spinner />;
    case 'success':
      return <List items={state.data} /* ... */ />;
    case 'error':
      return <ErrorMessage message={state.error} />;
  }
}
```

**Use `satisfies` for compile-time validation:**
```typescript
// [GOOD] Validates shape while preserving narrow type
const ROUTES = {
  home: '/',
  users: '/users',
  userDetail: '/users/:id',
  settings: '/settings',
} satisfies Record<string, string>;

// Type is preserved as literal union, not widened to string
type Route = (typeof ROUTES)[keyof typeof ROUTES];
```

**Never use `any` without justification:**
```typescript
// [BAD]
function processData(data: any): any { }

// [GOOD] Use unknown for truly unknown data, then narrow
function processData(data: unknown): User {
  if (!isUser(data)) {
    throw new Error('Invalid user data');
  }
  return data;
}

// [GOOD] Type guard
function isUser(value: unknown): value is User {
  return (
    typeof value === 'object' &&
    value !== null &&
    'email' in value &&
    'name' in value
  );
}
```

---

## Documentation

### TSDoc / JSDoc

**Required for:**
- All exported components, hooks, and utility functions
- All exported interfaces, types, and constants
- Complex internal functions

**Format:**
```typescript
/**
 * Custom hook for fetching and caching user data.
 *
 * Wraps TanStack Query with application-specific defaults
 * and error handling. Data is cached for 5 minutes.
 *
 * @param userId - The unique identifier of the user
 * @returns Query result containing user data, loading state, and error
 *
 * @example
 * ```tsx
 * function UserProfile({ userId }: { userId: string }) {
 *   const { data: user, isLoading, error } = useUser(userId);
 *
 *   if (isLoading) return <Spinner />;
 *   if (error) return <ErrorMessage error={error} />;
 *   return <UserCard user={user} />;
 * }
 * ```
 */
export function useUser(userId: string) {
  return useQuery({
    queryKey: userKeys.detail(userId),
    queryFn: () => fetchUser(userId),
    staleTime: 5 * 60 * 1000,
  });
}
```

**Component documentation:**
```typescript
/**
 * Displays a paginated, sortable table of users.
 *
 * Supports filtering by role and searching by name.
 * Handles empty states and loading indicators internally.
 *
 * @example
 * ```tsx
 * <UserTable
 *   users={users}
 *   pageSize={20}
 *   onUserSelect={handleUserSelect}
 * />
 * ```
 */
export function UserTable({ users, pageSize = 10, onUserSelect }: UserTableProps) {
  // ...
}
```

**Prop documentation:**
```typescript
interface DataGridProps<T> {
  /** The data rows to display in the grid. */
  data: T[];

  /** Column definitions including header, accessor, and optional formatter. */
  columns: ColumnDef<T>[];

  /** Number of rows per page. Defaults to 25. */
  pageSize?: number;

  /** Called when a row is clicked. Receives the row data. */
  onRowClick?: (row: T) => void;

  /** Whether to show the loading skeleton. Defaults to false. */
  loading?: boolean;
}
```

---

## Architecture

### Feature-Based Directory Structure

**Organize by feature, not by type:**
```
src/
├── app/                        # Next.js App Router (or route definitions)
│   ├── layout.tsx
│   ├── page.tsx
│   ├── (auth)/
│   │   ├── login/page.tsx
│   │   └── register/page.tsx
│   ├── dashboard/
│   │   ├── layout.tsx
│   │   └── page.tsx
│   └── users/
│       ├── page.tsx            # Server Component — fetches data
│       ├── [id]/page.tsx
│       └── loading.tsx
├── features/                   # Feature modules
│   ├── users/
│   │   ├── components/
│   │   │   ├── user-card.tsx
│   │   │   ├── user-list.tsx
│   │   │   └── user-filters.tsx
│   │   ├── hooks/
│   │   │   └── use-user-data.ts
│   │   ├── actions/            # Server Actions
│   │   │   └── user.actions.ts
│   │   ├── queries/            # TanStack Query hooks
│   │   │   └── user.queries.ts
│   │   ├── stores/             # Zustand stores
│   │   │   └── user-filter.store.ts
│   │   ├── schemas/            # Zod validation schemas
│   │   │   └── user.schema.ts
│   │   ├── user.types.ts
│   │   └── index.ts            # Public API
│   └── dashboard/
│       └── ...
├── components/                 # Shared UI components
│   ├── ui/                     # Primitive components (Button, Input, Modal)
│   │   ├── button.tsx
│   │   ├── input.tsx
│   │   └── modal.tsx
│   └── layout/                 # Layout components (Header, Sidebar)
│       ├── header.tsx
│       └── sidebar.tsx
├── hooks/                      # Shared custom hooks
│   ├── use-debounce.ts
│   └── use-media-query.ts
├── lib/                        # Shared utilities and config
│   ├── api-client.ts
│   ├── logger.ts
│   └── constants.ts
├── types/                      # Shared type definitions
│   └── api.types.ts
└── styles/                     # Global styles
    └── globals.css
```

### Server Components vs Client Components

**Default to Server Components. Add `'use client'` only when you need:**
- Event handlers (`onClick`, `onChange`, etc.)
- Browser APIs (`window`, `localStorage`, `IntersectionObserver`)
- React hooks that use state or effects (`useState`, `useEffect`, `useRef`)
- Third-party libraries that require browser context

```typescript
// Server Component (default) — fetches data, renders structure
// app/users/page.tsx
import { UserList } from '@/features/users/components/user-list';
import { getUsers } from '@/features/users/actions/user.actions';

export default async function UsersPage() {
  const users = await getUsers();
  return (
    <main>
      <h1>Users</h1>
      <UserList users={users} />
    </main>
  );
}

// Client Component — handles interactivity at the leaves
// features/users/components/user-list.tsx
'use client';

import { useState } from 'react';
import { UserCard } from './user-card';
import type { User } from '../user.types';

interface UserListProps {
  users: User[];
}

export function UserList({ users }: UserListProps) {
  const [search, setSearch] = useState('');

  const filtered = users.filter(u =>
    u.name.toLowerCase().includes(search.toLowerCase()),
  );

  return (
    <div>
      <input
        type="search"
        value={search}
        onChange={e => setSearch(e.target.value)}
        placeholder="Search users..."
        aria-label="Search users"
      />
      {filtered.map(user => (
        <UserCard key={user.id} user={user} />
      ))}
    </div>
  );
}
```

**`'use client'` placement rules:**
```typescript
// [GOOD] Place at the module boundary, not on individual components
// This file is the client boundary — everything it imports is also client
'use client';

export function InteractiveWidget() { /* ... */ }

// [BAD] Pushing the boundary too high — makes the entire page client-rendered
// app/users/page.tsx
'use client'; // Avoid: the page should be a Server Component
```

### Server Actions

```typescript
// features/users/actions/user.actions.ts
'use server';

import { revalidatePath } from 'next/cache';
import { z } from 'zod';
import { createUserSchema } from '../schemas/user.schema';

export async function createUser(formData: FormData) {
  const raw = Object.fromEntries(formData);
  const parsed = createUserSchema.safeParse(raw);

  if (!parsed.success) {
    return { error: parsed.error.flatten().fieldErrors };
  }

  await db.user.create({ data: parsed.data });
  revalidatePath('/users');
}

export async function getUsers() {
  return db.user.findMany({ orderBy: { createdAt: 'desc' } });
}
```

### State Management

**Separate client state from server state:**

```typescript
// Server state — TanStack Query
// features/users/queries/user.queries.ts
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';

export const userKeys = {
  all: ['users'] as const,
  lists: () => [...userKeys.all, 'list'] as const,
  list: (filters: UserFilters) => [...userKeys.lists(), filters] as const,
  details: () => [...userKeys.all, 'detail'] as const,
  detail: (id: string) => [...userKeys.details(), id] as const,
};

export function useUsers(filters: UserFilters) {
  return useQuery({
    queryKey: userKeys.list(filters),
    queryFn: () => fetchUsers(filters),
  });
}

export function useCreateUser() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: createUser,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: userKeys.lists() });
    },
  });
}
```

```typescript
// Client state — Zustand
// features/users/stores/user-filter.store.ts
import { create } from 'zustand';

interface UserFilterState {
  search: string;
  role: UserRole | 'all';
  setSearch: (search: string) => void;
  setRole: (role: UserRole | 'all') => void;
  reset: () => void;
}

export const useUserFilterStore = create<UserFilterState>((set) => ({
  search: '',
  role: 'all',
  setSearch: (search) => set({ search }),
  setRole: (role) => set({ role }),
  reset: () => set({ search: '', role: 'all' }),
}));
```

### Component Patterns

**Smart (Container) vs Presentational components:**
```typescript
// Smart component — manages data and orchestrates
'use client';

import { useUsers } from '../queries/user.queries';
import { useUserFilterStore } from '../stores/user-filter.store';
import { UserFilters } from './user-filters';
import { UserTable } from './user-table';

export function UserPage() {
  const { search, role } = useUserFilterStore();
  const { data: users, isLoading, error } = useUsers({ search, role });

  if (error) return <ErrorMessage message={error.message} />;

  return (
    <section>
      <UserFilters />
      <UserTable users={users ?? []} loading={isLoading} />
    </section>
  );
}

// Presentational component — pure rendering, props only
interface UserTableProps {
  users: User[];
  loading: boolean;
}

export function UserTable({ users, loading }: UserTableProps) {
  if (loading) return <TableSkeleton />;
  if (users.length === 0) return <p>No users found.</p>;

  return (
    <table>
      <thead>
        <tr>
          <th scope="col">Name</th>
          <th scope="col">Email</th>
          <th scope="col">Role</th>
        </tr>
      </thead>
      <tbody>
        {users.map(user => (
          <tr key={user.id}>
            <td>{user.name}</td>
            <td>{user.email}</td>
            <td>{user.role}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
```

---

## Error Handling

### Error Boundaries

Error Boundaries catch rendering errors and display fallback UI. They require class component syntax:

```typescript
// components/error-boundary.tsx
import { Component, type ErrorInfo, type ReactNode } from 'react';
import { logger } from '@/lib/logger';

interface ErrorBoundaryProps {
  children: ReactNode;
  fallback: ReactNode | ((error: Error, reset: () => void) => ReactNode);
}

interface ErrorBoundaryState {
  error: Error | null;
}

export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    logger.error('Component error', {
      message: error.message,
      stack: error.stack,
      componentStack: info.componentStack,
    });
  }

  reset = () => {
    this.setState({ error: null });
  };

  render() {
    if (this.state.error) {
      const { fallback } = this.props;
      if (typeof fallback === 'function') {
        return fallback(this.state.error, this.reset);
      }
      return fallback;
    }
    return this.props.children;
  }
}
```

**Strategic placement:**
```typescript
// [GOOD] Granular boundaries around risky features
function DashboardPage() {
  return (
    <main>
      <h1>Dashboard</h1>
      <ErrorBoundary fallback={<p>Failed to load analytics.</p>}>
        <AnalyticsWidget />
      </ErrorBoundary>
      <ErrorBoundary fallback={<p>Failed to load activity feed.</p>}>
        <ActivityFeed />
      </ErrorBoundary>
    </main>
  );
}

// [BAD] Only wrapping at the app root — one error takes down the entire page
```

**Next.js `error.tsx` for route-level error handling:**
```typescript
// app/users/error.tsx
'use client';

interface ErrorPageProps {
  error: Error & { digest?: string };
  reset: () => void;
}

export default function UserError({ error, reset }: ErrorPageProps) {
  return (
    <div role="alert">
      <h2>Something went wrong</h2>
      <p>{error.message}</p>
      <button onClick={reset}>Try again</button>
    </div>
  );
}
```

### React 19+ Error Hooks

```typescript
// Global error reporting via createRoot options
import { createRoot } from 'react-dom/client';

const root = createRoot(document.getElementById('root')!, {
  onUncaughtError(error, errorInfo) {
    logger.error('Uncaught error', {
      message: error instanceof Error ? error.message : String(error),
      componentStack: errorInfo.componentStack,
    });
  },
  onCaughtError(error, errorInfo) {
    logger.warn('Caught error (Error Boundary)', {
      message: error instanceof Error ? error.message : String(error),
      componentStack: errorInfo.componentStack,
    });
  },
});
```

### Event Handler and Async Error Handling

Error Boundaries do not catch errors in event handlers or async code. Use try/catch:

```typescript
// [GOOD] Explicit error handling in event handlers
function SubmitButton() {
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit() {
    try {
      setError(null);
      await submitForm(data);
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Submission failed';
      setError(message);
      logger.error('Form submission failed', { error: err });
    }
  }

  return (
    <>
      {error && <p role="alert" className="error">{error}</p>}
      <button onClick={handleSubmit}>Submit</button>
    </>
  );
}

// [BAD] Unhandled promise rejection
function SubmitButton() {
  async function handleSubmit() {
    await submitForm(data); // Crashes silently on error
  }
  return <button onClick={handleSubmit}>Submit</button>;
}
```

### Domain-Specific Error Types

```typescript
export class AppError extends Error {
  constructor(
    message: string,
    public readonly code: string,
    public readonly originalError?: unknown,
  ) {
    super(message);
    this.name = 'AppError';
  }
}

export class ValidationError extends AppError {
  constructor(message: string, public readonly field?: string) {
    super(message, 'VALIDATION_ERROR');
    this.name = 'ValidationError';
  }
}

export class NotFoundError extends AppError {
  constructor(entity: string, id: string | number) {
    super(`${entity} not found with id: ${id}`, 'NOT_FOUND');
    this.name = 'NotFoundError';
  }
}

export class UnauthorizedError extends AppError {
  constructor(message = 'Authentication required') {
    super(message, 'UNAUTHORIZED');
    this.name = 'UnauthorizedError';
  }
}
```

---

## Testing

### Test-Driven Development (TDD)

**Process:**
1. Write failing test
2. Write minimal code to pass
3. Refactor
4. Repeat

**Coverage Requirements:**
- Minimum: 90% line coverage
- Critical paths: 100% coverage
- All exported hooks and utilities: 100% coverage

### Setup: Vitest + React Testing Library

**Why Vitest:** 4x faster than Jest through parallel execution, native ESM support, seamless Vite integration, and Jest-compatible API for easy migration.

**Installation:**
```bash
npm install -D vitest @testing-library/react @testing-library/jest-dom @testing-library/user-event jsdom msw
```

**Configuration:**
```typescript
// vitest.config.ts
import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    coverage: {
      provider: 'v8',
      reporter: ['text', 'lcov'],
      thresholds: { lines: 90, branches: 85, functions: 90, statements: 90 },
    },
  },
});
```

```typescript
// src/test/setup.ts
import '@testing-library/jest-dom/vitest';
```

### Component Testing

**Query priority order (most accessible first):**
1. `getByRole` — accessible by ARIA role
2. `getByLabelText` — form elements by label
3. `getByPlaceholderText` — input placeholders
4. `getByText` — visible text content
5. `getByTestId` — last resort

```typescript
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { UserCard } from './user-card';

describe('UserCard', () => {
  const mockUser: User = {
    id: '1',
    name: 'Jane Doe',
    email: 'jane@example.com',
    role: UserRole.Admin,
  };

  it('should display user name and email', () => {
    render(<UserCard user={mockUser} />);

    expect(screen.getByText('Jane Doe')).toBeInTheDocument();
    expect(screen.getByText('jane@example.com')).toBeInTheDocument();
  });

  it('should call onSelect when clicked', async () => {
    const user = userEvent.setup();
    const handleSelect = vi.fn();

    render(<UserCard user={mockUser} onSelect={handleSelect} />);

    await user.click(screen.getByRole('button', { name: /jane doe/i }));

    expect(handleSelect).toHaveBeenCalledWith(mockUser);
  });

  it('should show admin badge for admin users', () => {
    render(<UserCard user={mockUser} />);

    expect(screen.getByRole('status')).toHaveTextContent('Admin');
  });
});
```

### Hook Testing

```typescript
import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useUsers } from './user.queries';

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });

  return function Wrapper({ children }: { children: React.ReactNode }) {
    return (
      <QueryClientProvider client={queryClient}>
        {children}
      </QueryClientProvider>
    );
  };
}

describe('useUsers', () => {
  it('should fetch and return users', async () => {
    const { result } = renderHook(() => useUsers({ role: 'all' }), {
      wrapper: createWrapper(),
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(result.current.data).toHaveLength(3);
    expect(result.current.data![0].name).toBe('Jane Doe');
  });
});
```

### API Mocking with MSW

```typescript
// src/test/mocks/handlers.ts
import { http, HttpResponse } from 'msw';

export const handlers = [
  http.get('/api/users', () => {
    return HttpResponse.json([
      { id: '1', name: 'Jane Doe', email: 'jane@example.com', role: 'ADMIN' },
      { id: '2', name: 'John Smith', email: 'john@example.com', role: 'USER' },
    ]);
  }),

  http.post('/api/users', async ({ request }) => {
    const body = await request.json();
    return HttpResponse.json({ id: '3', ...body }, { status: 201 });
  }),
];

// src/test/mocks/server.ts
import { setupServer } from 'msw/node';
import { handlers } from './handlers';

export const server = setupServer(...handlers);

// src/test/setup.ts
import { server } from './mocks/server';

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());
```

### E2E Testing (Playwright)

```typescript
// e2e/users.spec.ts
import { test, expect } from '@playwright/test';

test.describe('User Management', () => {
  test('should display user list', async ({ page }) => {
    await page.goto('/users');

    await expect(page.getByRole('table')).toBeVisible();
    await expect(page.getByRole('row')).toHaveCount.greaterThan(1);
  });

  test('should search users by name', async ({ page }) => {
    await page.goto('/users');

    await page.getByRole('searchbox', { name: /search/i }).fill('Jane');

    await expect(page.getByText('Jane Doe')).toBeVisible();
    await expect(page.getByText('John Smith')).not.toBeVisible();
  });

  test('should create a new user', async ({ page }) => {
    await page.goto('/users');

    await page.getByRole('button', { name: /add user/i }).click();
    await page.getByLabel('Name').fill('New User');
    await page.getByLabel('Email').fill('new@example.com');
    await page.getByRole('button', { name: /save/i }).click();

    await expect(page.getByText('New User')).toBeVisible();
  });
});
```

---

## Security

### React Built-in Protections

React automatically escapes values rendered in JSX using curly braces `{}`, converting dangerous characters to HTML entities:

```typescript
// Safe by default — React escapes the HTML
function SafeComponent({ userInput }: { userInput: string }) {
  return <p>{userInput}</p>; // '<script>' rendered as text, not executed
}
```

### dangerouslySetInnerHTML

**Never use with unsanitized user input.** If you must render raw HTML, sanitize with DOMPurify:

```typescript
// [GOOD] Sanitize before rendering
import DOMPurify from 'dompurify';

function RichContent({ html }: { html: string }) {
  const sanitized = DOMPurify.sanitize(html, {
    ALLOWED_TAGS: ['p', 'b', 'i', 'em', 'strong', 'a', 'ul', 'ol', 'li', 'br'],
    ALLOWED_ATTR: ['href', 'target', 'rel'],
  });

  return <div dangerouslySetInnerHTML={{ __html: sanitized }} />;
}

// [BAD] Raw user input — XSS vulnerability
function UnsafeContent({ html }: { html: string }) {
  return <div dangerouslySetInnerHTML={{ __html: html }} />;
}
```

### Content Security Policy (CSP)

```typescript
// next.config.js — CSP headers
const securityHeaders = [
  {
    key: 'Content-Security-Policy',
    value: [
      "default-src 'self'",
      "script-src 'self'",
      "style-src 'self' 'unsafe-inline'",
      "img-src 'self' data: https:",
      "font-src 'self'",
      "connect-src 'self' https://api.example.com",
    ].join('; '),
  },
  { key: 'X-Frame-Options', value: 'DENY' },
  { key: 'X-Content-Type-Options', value: 'nosniff' },
  { key: 'Referrer-Policy', value: 'strict-origin-when-cross-origin' },
];
```

### Authentication and Authorization

```typescript
// [GOOD] Protect routes with middleware (Next.js)
// middleware.ts
import { NextRequest, NextResponse } from 'next/server';

const PROTECTED_PATHS = ['/dashboard', '/settings', '/users'];

export function middleware(request: NextRequest) {
  const token = request.cookies.get('session')?.value;
  const isProtected = PROTECTED_PATHS.some(path =>
    request.nextUrl.pathname.startsWith(path),
  );

  if (isProtected && !token) {
    return NextResponse.redirect(new URL('/login', request.url));
  }

  return NextResponse.next();
}

export const config = {
  matcher: ['/dashboard/:path*', '/settings/:path*', '/users/:path*'],
};
```

### Accessibility as Security

Accessibility issues can also be security issues (e.g., screen reader users unable to complete authentication flows):

```typescript
// [GOOD] Accessible form with proper ARIA attributes
function LoginForm() {
  const [error, setError] = useState<string | null>(null);

  return (
    <form onSubmit={handleSubmit} aria-label="Login">
      <div>
        <label htmlFor="email">Email</label>
        <input
          id="email"
          type="email"
          required
          autoComplete="email"
          aria-describedby={error ? 'login-error' : undefined}
        />
      </div>
      <div>
        <label htmlFor="password">Password</label>
        <input
          id="password"
          type="password"
          required
          autoComplete="current-password"
        />
      </div>
      {error && (
        <p id="login-error" role="alert" aria-live="assertive">
          {error}
        </p>
      )}
      <button type="submit">Sign in</button>
    </form>
  );
}
```

**Security Checklist:**
- [ ] No secrets in client-side code or version control
- [ ] All user input sanitized before rendering as HTML
- [ ] Never use `dangerouslySetInnerHTML` without DOMPurify
- [ ] HTTPS only for all API communication
- [ ] CSRF protection enabled (SameSite cookies, CSRF tokens)
- [ ] CSP headers configured
- [ ] Route protection via middleware or guards
- [ ] JWT/session tokens stored in HttpOnly cookies (not localStorage)
- [ ] No sensitive data in URL parameters or client-side state
- [ ] Dependencies scanned for vulnerabilities (`npm audit`)
- [ ] `eslint-plugin-jsx-a11y` enabled for accessibility checks
- [ ] Server Actions validate all inputs with Zod schemas
- [ ] `rel="noopener noreferrer"` on external links with `target="_blank"`

---

## Performance

### React Compiler

The React Compiler automatically inserts memoization where beneficial, eliminating the need for manual `useMemo`, `useCallback`, and `React.memo` in most cases:

```typescript
// [GOOD] With React Compiler — write natural code
function UserList({ users, onSelect }: UserListProps) {
  const sorted = users.toSorted((a, b) => a.name.localeCompare(b.name));

  function handleClick(user: User) {
    onSelect(user);
  }

  return (
    <ul>
      {sorted.map(user => (
        <li key={user.id} onClick={() => handleClick(user)}>
          {user.name}
        </li>
      ))}
    </ul>
  );
}

// [AVOID] Manual memoization when React Compiler is enabled
function UserList({ users, onSelect }: UserListProps) {
  const sorted = useMemo(
    () => users.toSorted((a, b) => a.name.localeCompare(b.name)),
    [users],
  );

  const handleClick = useCallback(
    (user: User) => onSelect(user),
    [onSelect],
  );

  return (/* ... */);
}
```

**When manual optimization is still needed:**
- Third-party libraries expecting stable function references
- Functions in `useEffect` dependency arrays where the compiler cannot analyze
- Extremely expensive calculations with rapidly changing external data

### Server Components for Bundle Reduction

Server Components render on the server and send HTML, not JavaScript. This can reduce client JS bundles by ~40%:

```typescript
// [GOOD] Heavy imports stay on the server — zero client JS
// app/reports/page.tsx (Server Component)
import { format } from 'date-fns';
import { generateReport } from '@/lib/reports';

export default async function ReportsPage() {
  const report = await generateReport();

  return (
    <section>
      <h1>Monthly Report</h1>
      <p>Generated: {format(new Date(), 'PPP')}</p>
      <ReportTable data={report} />
    </section>
  );
}
```

### Code Splitting and Lazy Loading

```typescript
import { lazy, Suspense } from 'react';

const HeavyChart = lazy(() => import('./heavy-chart'));

function Dashboard() {
  return (
    <div>
      <h1>Dashboard</h1>
      <Suspense fallback={<ChartSkeleton />}>
        <HeavyChart data={data} />
      </Suspense>
    </div>
  );
}
```

### Parallel Data Fetching

```typescript
// [GOOD] Fetch in parallel — no waterfall
// app/dashboard/page.tsx
export default async function DashboardPage() {
  const [users, analytics, notifications] = await Promise.all([
    getUsers(),
    getAnalytics(),
    getNotifications(),
  ]);

  return (
    <main>
      <UserSummary users={users} />
      <AnalyticsChart data={analytics} />
      <NotificationFeed items={notifications} />
    </main>
  );
}

// [BAD] Sequential fetches — waterfall
export default async function DashboardPage() {
  const users = await getUsers();
  const analytics = await getAnalytics();       // Waits for users
  const notifications = await getNotifications(); // Waits for analytics
  // ...
}
```

### Streaming with Suspense

```typescript
// app/users/page.tsx
import { Suspense } from 'react';

export default function UsersPage() {
  return (
    <main>
      <h1>Users</h1>
      <Suspense fallback={<TableSkeleton />}>
        <UserTable />
      </Suspense>
      <Suspense fallback={<StatsSkeleton />}>
        <UserStats />
      </Suspense>
    </main>
  );
}
```

### Virtualization for Long Lists

```typescript
// [GOOD] Virtualize lists with 100+ items
import { useVirtualizer } from '@tanstack/react-virtual';

function VirtualizedList({ items }: { items: Item[] }) {
  const parentRef = useRef<HTMLDivElement>(null);

  const virtualizer = useVirtualizer({
    count: items.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 48,
  });

  return (
    <div ref={parentRef} style={{ height: '400px', overflow: 'auto' }}>
      <div style={{ height: `${virtualizer.getTotalSize()}px`, position: 'relative' }}>
        {virtualizer.getVirtualItems().map(virtualRow => (
          <div
            key={virtualRow.key}
            style={{
              position: 'absolute',
              top: 0,
              transform: `translateY(${virtualRow.start}px)`,
              height: `${virtualRow.size}px`,
              width: '100%',
            }}
          >
            {items[virtualRow.index].name}
          </div>
        ))}
      </div>
    </div>
  );
}
```

### Bundle Size

```typescript
// [GOOD] Import only what you need
import { format, parseISO } from 'date-fns';
import debounce from 'lodash-es/debounce';

// [BAD] Importing entire libraries
import _ from 'lodash';
import * as dateFns from 'date-fns';
```

### Image Optimization

```typescript
// [GOOD] Use next/image for automatic optimization
import Image from 'next/image';

function Avatar({ src, name }: { src: string; name: string }) {
  return (
    <Image
      src={src}
      alt={`${name}'s avatar`}
      width={48}
      height={48}
      loading="lazy"
    />
  );
}

// Above-the-fold images should use priority
<Image src="/hero.jpg" alt="Hero" width={1200} height={600} priority />
```

---

## Dependencies

### Dependency Management

**Principles:**
- Use lockfile (`package-lock.json` or `pnpm-lock.yaml`)
- Pin exact versions for production dependencies
- Regular security updates with `npm audit`
- Minimize third-party dependencies — every dependency is a liability

**package.json (typical React/Next.js project):**
```json
{
  "dependencies": {
    "next": "~15.0.0",
    "react": "~19.0.0",
    "react-dom": "~19.0.0",
    "@tanstack/react-query": "~5.60.0",
    "zustand": "~5.0.0",
    "zod": "~3.23.0"
  },
  "devDependencies": {
    "typescript": "~5.6.0",
    "@types/react": "~19.0.0",
    "@types/react-dom": "~19.0.0",
    "eslint": "~9.0.0",
    "@typescript-eslint/eslint-plugin": "~8.0.0",
    "eslint-plugin-jsx-a11y": "~6.10.0",
    "prettier": "~3.4.0",
    "vitest": "~2.1.0",
    "@testing-library/react": "~16.0.0",
    "@testing-library/jest-dom": "~6.6.0",
    "@testing-library/user-event": "~14.5.0",
    "msw": "~2.6.0",
    "@playwright/test": "~1.48.0"
  }
}
```

**Security Scanning:**
- Run `npm audit` regularly
- Update dependencies with `npx npm-check-updates`
- Review dependency licenses
- Audit transitive dependencies
- Use `npm audit fix` for automated patching
- Consider tools like Snyk or Socket for supply chain security

---

## Logging

### Centralized Logging Service

```typescript
// lib/logger.ts

enum LogLevel {
  Debug = 0,
  Info = 1,
  Warn = 2,
  Error = 3,
}

const LOG_LEVEL = process.env.NODE_ENV === 'production' ? LogLevel.Warn : LogLevel.Debug;

function formatMessage(level: string, message: string, context?: Record<string, unknown>): string {
  return JSON.stringify({
    timestamp: new Date().toISOString(),
    level,
    message,
    ...context,
  });
}

function sendToErrorTracker(message: string, context?: Record<string, unknown>): void {
  // Integrate with Sentry, Datadog, etc.
}

export const logger = {
  debug(message: string, context?: Record<string, unknown>): void {
    if (LOG_LEVEL <= LogLevel.Debug) {
      console.debug(formatMessage('DEBUG', message, context));
    }
  },

  info(message: string, context?: Record<string, unknown>): void {
    if (LOG_LEVEL <= LogLevel.Info) {
      console.info(formatMessage('INFO', message, context));
    }
  },

  warn(message: string, context?: Record<string, unknown>): void {
    if (LOG_LEVEL <= LogLevel.Warn) {
      console.warn(formatMessage('WARN', message, context));
    }
  },

  error(message: string, context?: Record<string, unknown>): void {
    console.error(formatMessage('ERROR', message, context));

    if (process.env.NODE_ENV === 'production') {
      sendToErrorTracker(message, context);
    }
  },
};
```

### Logging Best Practices

**What to Log:**
- API errors (status, URL, method)
- Authentication events (login, logout, failed attempts)
- Critical user actions (form submissions, payments)
- Performance metrics (component render times, API latencies)
- Errors with full context (component stack, user action that triggered it)

**What NOT to Log:**
- Passwords or tokens
- Personal Identifiable Information (PII)
- Credit card numbers
- Full request/response bodies
- Verbose debug logs in production

---

## Code Review

### Review Checklist

**Functionality:**
- [ ] Code solves the stated problem
- [ ] Edge cases handled
- [ ] Error handling appropriate
- [ ] No obvious bugs

**React-Specific Quality:**
- [ ] Components are small and focused on one responsibility
- [ ] Server Components used by default; `'use client'` only where needed
- [ ] `'use client'` placed at the lowest possible boundary
- [ ] Hooks follow the Rules of Hooks (top level, no conditionals)
- [ ] Custom hooks extract reusable logic
- [ ] `key` props use stable, unique identifiers (never array index for dynamic lists)
- [ ] Effects have proper cleanup functions (event listeners, subscriptions, timers)
- [ ] Effects have correct dependency arrays
- [ ] No unnecessary state (derive values from existing state instead)
- [ ] Props are typed with explicit interfaces
- [ ] Event handlers follow naming conventions (`handle*` / `on*`)

**Accessibility:**
- [ ] Semantic HTML used (`<button>`, `<nav>`, `<main>`, `<section>`)
- [ ] Form inputs have associated `<label>` elements
- [ ] Images have `alt` attributes
- [ ] Interactive elements are keyboard accessible
- [ ] Color contrast meets WCAG 2.2 AA (4.5:1)
- [ ] Dynamic content uses `aria-live` regions

**Testing:**
- [ ] Tests included for new code
- [ ] Tests cover edge cases and error paths
- [ ] Coverage meets minimum (90%)
- [ ] E2E tests for critical user flows
- [ ] Tests use accessible queries (`getByRole` first)

**Security:**
- [ ] No secrets in client code
- [ ] No `dangerouslySetInnerHTML` without DOMPurify
- [ ] Server Actions validate all inputs
- [ ] External links use `rel="noopener noreferrer"`

**Performance:**
- [ ] No unnecessary client-side JavaScript (use Server Components)
- [ ] Code splitting for heavy components
- [ ] Stable `key` props in lists
- [ ] No unnecessary re-renders (React DevTools Profiler verified)
- [ ] Bundle size checked for new dependencies

### Review Process

**Requirements:**
- Minimum 2 approvals for production code
- All automated checks must pass (lint, types, tests, build)
- No unresolved comments
- Squash commits before merge

---

## Tooling

### Required Tools

**Build:**
- `Vite` — Build tool and dev server (or Next.js built-in)
- `TypeScript` — Strict mode enabled

**Code Formatting:**
- `Prettier` — Opinionated code formatter

**Linting:**
- `ESLint` with flat config (`eslint.config.js`)
- `@typescript-eslint/eslint-plugin` — TypeScript-specific rules
- `eslint-plugin-jsx-a11y` — Accessibility checks
- `eslint-plugin-react-hooks` — Hooks rules enforcement

**Testing:**
- `Vitest` — Unit and integration testing
- `@testing-library/react` — Component testing
- `MSW` — API mocking
- `Playwright` — End-to-end testing

### ESLint Configuration

```javascript
// eslint.config.js
import js from '@eslint/js';
import tseslint from 'typescript-eslint';
import reactHooks from 'eslint-plugin-react-hooks';
import jsxA11y from 'eslint-plugin-jsx-a11y';

export default tseslint.config(
  js.configs.recommended,
  ...tseslint.configs.recommendedTypeChecked,
  {
    languageOptions: {
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
  },
  {
    plugins: {
      'react-hooks': reactHooks,
      'jsx-a11y': jsxA11y,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      ...jsxA11y.configs.recommended.rules,
      '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_' }],
      '@typescript-eslint/no-explicit-any': 'error',
      '@typescript-eslint/consistent-type-imports': 'error',
      'no-console': 'warn',
    },
  },
);
```

### Prettier Configuration

```json
{
  "singleQuote": true,
  "trailingComma": "all",
  "printWidth": 100,
  "tabWidth": 2,
  "semi": true,
  "bracketSpacing": true,
  "arrowParens": "avoid",
  "jsxSingleQuote": false
}
```

### TypeScript Configuration

```json
{
  "compilerOptions": {
    "strict": true,
    "noImplicitAny": true,
    "strictNullChecks": true,
    "strictFunctionTypes": true,
    "noImplicitReturns": true,
    "noFallthroughCasesInSwitch": true,
    "noUncheckedIndexedAccess": true,
    "forceConsistentCasingInFileNames": true,
    "jsx": "react-jsx",
    "moduleResolution": "bundler",
    "target": "ES2022",
    "lib": ["DOM", "DOM.Iterable", "ES2022"],
    "paths": {
      "@/*": ["./src/*"]
    }
  },
  "include": ["src"],
  "exclude": ["node_modules"]
}
```

### CI/CD Pipeline

**Required checks before merge:**
```bash
# Type checking
npx tsc --noEmit

# Lint
npx eslint .

# Format check
npx prettier --check "src/**/*.{ts,tsx,css,json}"

# Unit tests with coverage
npx vitest run --coverage

# E2E tests
npx playwright test

# Build (ensures compilation passes)
npx next build  # or: npx vite build

# Bundle analysis
npx next build --analyze  # or: npx vite-bundle-visualizer

# Dependency audit
npm audit --audit-level=moderate
```

---

## Quick Reference

### Daily Workflow

1. **Before coding:**
   - Pull latest changes
   - Create feature branch
   - Write failing test (TDD)

2. **While coding:**
   - Follow TypeScript strict mode
   - Add TSDoc for exported APIs
   - Use Server Components by default
   - Keep components small and focused
   - Run tests frequently
   - Commit small, logical changes

3. **Before pushing:**
   - Run full test suite
   - Check coverage (90%+)
   - Run linter and formatter
   - Check bundle size impact
   - Update documentation

4. **Code review:**
   - Address all comments
   - Ensure CI passes
   - Get 2 approvals
   - Squash and merge

### Common Commands

```bash
# Create new Next.js app
npx create-next-app@latest my-app --typescript --eslint --tailwind --app

# Create new Vite + React app
npm create vite@latest my-app -- --template react-ts

# Run dev server
npm run dev

# Lint
npx eslint .

# Format
npx prettier --write "src/**/*.{ts,tsx,css,json}"

# Type check
npx tsc --noEmit

# Run tests
npx vitest

# Run tests with coverage
npx vitest run --coverage

# Run E2E tests
npx playwright test           # Headless
npx playwright test --ui      # Interactive

# Build for production
npx next build                # or: npx vite build

# Audit dependencies
npm audit

# Update dependencies
npx npm-check-updates -u && npm install
```

---

## References

- [React Documentation](https://react.dev/)
- [Next.js Documentation](https://nextjs.org/docs)
- [TypeScript Handbook](https://www.typescriptlang.org/docs/handbook/)
- [Vercel React Best Practices](https://vercel.com/blog/introducing-react-best-practices)
- [React Server Components Guide](https://react.dev/reference/rsc/server-components)
- [TanStack Query Documentation](https://tanstack.com/query/latest)
- [Zustand Documentation](https://zustand.docs.pmnd.rs/)
- [Vitest Documentation](https://vitest.dev/)
- [React Testing Library](https://testing-library.com/docs/react-testing-library/intro/)
- [Playwright Documentation](https://playwright.dev/)
- [ESLint Flat Config Migration](https://eslint.org/docs/latest/use/configure/migration-guide)
- [WCAG 2.2 Guidelines](https://www.w3.org/TR/WCAG22/)
- [DOMPurify](https://github.com/cure53/DOMPurify)

---

**Questions or suggestions?** Update this document through team discussion and code review.

**Version History:**
- v1.0 (2026) - Initial enterprise-grade guidelines
