# Angular Coding Guidelines - Enterprise Grade

**Version:** 1.0  
**Last Updated:** 2026  
**Target:** Production Angular applications requiring high quality, security, and maintainability

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

Follow **component-driven architecture** and reactive programming principles:
- One concept per file
- Components are focused on presentation
- Services encapsulate business logic
- Signals for state, Observables for async streams
- Composition over inheritance

**Core Principles:**
- Write code for humans first, machines second
- Optimize for maintainability over cleverness
- Test-driven development (TDD) is mandatory
- Security is not optional
- Performance matters, but measure before optimizing
- When in doubt, prefer consistency within a file over strict rule adherence
- Keep templates simple; refactor complex logic to TypeScript

---

## Code Style

### Base Standard

Follow the **Official Angular Style Guide** (angular.dev/style-guide) and **Google TypeScript Style Guide** with these specifications:

**Formatting:**
- Line length: 100 characters
- Indentation: 2 spaces (never tabs)
- Encoding: UTF-8
- Line endings: LF (Unix style)
- Semicolons: always required
- Quotes: single quotes for TypeScript, double quotes for HTML attributes

**File Naming:**
```
# Hyphen-separated, lowercase
user-profile.ts              # Component TypeScript
user-profile.html            # Component template
user-profile.css             # Component styles
user-profile.spec.ts         # Unit tests

# Match file name to primary TypeScript identifier
UserProfile  →  user-profile.ts
AuthService  →  auth.service.ts
CanActivateGuard → can-activate.guard.ts
```

**Naming Conventions:**
```typescript
// Classes - PascalCase
export class UserProfileComponent { }
export class AuthService { }

// Interfaces - PascalCase (no "I" prefix)
export interface UserConfig { }
export interface ApiResponse<T> { }

// Variables, functions, methods - camelCase
const userName = 'John';
function calculateTotal(): number { return 0; }

// Constants - UPPER_SNAKE_CASE for true compile-time constants
export const MAX_RETRY_COUNT = 3;
export const API_BASE_URL = 'https://api.example.com';

// Enums - PascalCase for name, PascalCase for members
enum UserRole {
  Admin = 'ADMIN',
  User = 'USER',
  Moderator = 'MODERATOR',
}

// Observables - suffix with $
users$: Observable<User[]>;
isLoading$: Observable<boolean>;

// Selectors - app prefix (or your app-specific prefix)
@Component({ selector: 'app-user-profile' })

// Directive selectors - camelCase with app prefix
@Directive({ selector: '[appTooltip]' })
```

**Import Ordering:**
```typescript
// 1. Angular core and framework
import { Component, inject, signal } from '@angular/core';
import { RouterModule } from '@angular/router';

// 2. Third-party libraries
import { Observable } from 'rxjs';
import { map, catchError } from 'rxjs/operators';

// 3. Local application
import { UserService } from '../services/user.service';
import { User } from '../models/user.model';
```

**Avoid:**
- Wildcard imports: `import * as _ from 'lodash'`
- Barrel files that re-export everything (causes tree-shaking issues)
- Overly generic file names: `helpers.ts`, `utils.ts`, `common.ts`
- Using `any` without explicit justification in a comment

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
    "forceConsistentCasingInFileNames": true
  },
  "angularCompilerOptions": {
    "strictInjectionParameters": true,
    "strictInputAccessModifiers": true,
    "strictTemplates": true
  }
}
```

**Interfaces for data contracts:**
```typescript
// [GOOD] Typed data models
export interface User {
  id: number;
  email: string;
  name: string;
  role: UserRole;
  createdAt: string; // ISO 8601 date string
}

export interface ApiResponse<T> {
  data: T;
  meta: {
    total: number;
    page: number;
    pageSize: number;
  };
}

export interface CreateUserRequest {
  email: string;
  name: string;
  role: UserRole;
}
```

**Discriminated unions for state management:**
```typescript
// [GOOD] Exhaustive state handling
type LoadingState<T> =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'success'; data: T }
  | { status: 'error'; error: string };

// Usage in component
@Component({ /* ... */ })
export class UserListComponent {
  protected state = signal<LoadingState<User[]>>({ status: 'idle' });

  protected renderState() {
    const current = this.state();
    switch (current.status) {
      case 'idle': return;
      case 'loading': return; // show spinner
      case 'success': return current.data; // render list
      case 'error': return current.error; // show error
    }
  }
}
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
- All public services, components, and directives
- All exported functions, interfaces, and types
- Complex private methods

**Format:**
```typescript
/**
 * Service for user authentication and session management.
 *
 * Provides login, logout, token refresh, and role-based access methods.
 * Uses JWT tokens stored in HttpOnly cookies.
 *
 * @example
 * ```typescript
 * const authService = inject(AuthService);
 * authService.login(credentials).subscribe(user => {
 *   console.log('Logged in:', user.name);
 * });
 * ```
 */
@Injectable({ providedIn: 'root' })
export class AuthService {
  /**
   * Authenticates a user with email and password.
   *
   * @param credentials - User login credentials
   * @returns Observable that emits the authenticated user
   * @throws {HttpErrorResponse} 401 if credentials are invalid
   * @throws {HttpErrorResponse} 429 if rate limit exceeded
   */
  login(credentials: LoginCredentials): Observable<User> {
    // ...
  }
}
```

**Component documentation:**
```typescript
/**
 * Displays a paginated, sortable list of users.
 *
 * Supports filtering by role and searching by name.
 * Emits events when a user is selected or deleted.
 *
 * @example
 * ```html
 * <app-user-list
 *   [users]="users()"
 *   [pageSize]="20"
 *   (userSelected)="onUserSelected($event)"
 * />
 * ```
 */
@Component({
  selector: 'app-user-list',
  templateUrl: './user-list.html',
  styleUrl: './user-list.css',
})
export class UserListComponent {
  /** List of users to display. */
  readonly users = input.required<User[]>();

  /** Number of users per page. Defaults to 10. */
  readonly pageSize = input<number>(10);

  /** Emits the selected user when a row is clicked. */
  readonly userSelected = output<User>();
}
```

---

## Architecture

### Feature-Based Directory Structure

**Organize by feature, not by type:**
```
src/
├── app/
│   ├── core/                    # Singleton services, guards, interceptors
│   │   ├── auth/
│   │   │   ├── auth.service.ts
│   │   │   ├── auth.guard.ts
│   │   │   └── auth.interceptor.ts
│   │   ├── logging/
│   │   │   └── logging.service.ts
│   │   └── error/
│   │       └── global-error-handler.ts
│   ├── shared/                  # Reusable components, pipes, directives
│   │   ├── components/
│   │   │   ├── button/
│   │   │   └── modal/
│   │   ├── directives/
│   │   ├── pipes/
│   │   └── models/
│   │       └── api-response.model.ts
│   ├── features/                # Feature modules (lazy loaded)
│   │   ├── users/
│   │   │   ├── user-list/
│   │   │   │   ├── user-list.ts
│   │   │   │   ├── user-list.html
│   │   │   │   ├── user-list.css
│   │   │   │   └── user-list.spec.ts
│   │   │   ├── user-detail/
│   │   │   ├── services/
│   │   │   │   └── user.service.ts
│   │   │   ├── models/
│   │   │   │   └── user.model.ts
│   │   │   └── users.routes.ts
│   │   ├── dashboard/
│   │   └── settings/
│   ├── app.component.ts
│   ├── app.config.ts
│   └── app.routes.ts
├── assets/
├── environments/
│   ├── environment.ts
│   └── environment.prod.ts
└── main.ts
```

### Component Patterns

**Smart (Container) vs Dumb (Presentational) components:**
```typescript
// Smart component - manages state and orchestrates
@Component({
  selector: 'app-user-page',
  template: `
    <app-user-filters
      [currentFilter]="filter()"
      (filterChanged)="onFilterChanged($event)"
    />
    <app-user-list
      [users]="filteredUsers()"
      [loading]="loading()"
      (userSelected)="onUserSelected($event)"
    />
  `,
})
export class UserPageComponent {
  private readonly userService = inject(UserService);
  private readonly router = inject(Router);

  protected filter = signal<UserFilter>({ role: 'all' });
  protected loading = signal(false);
  protected users = signal<User[]>([]);

  protected filteredUsers = computed(() => {
    const f = this.filter();
    return this.users().filter(u => f.role === 'all' || u.role === f.role);
  });

  constructor() {
    this.loadUsers();
  }

  protected onFilterChanged(filter: UserFilter): void {
    this.filter.set(filter);
  }

  protected onUserSelected(user: User): void {
    this.router.navigate(['/users', user.id]);
  }

  private loadUsers(): void {
    this.loading.set(true);
    this.userService.getUsers().subscribe({
      next: users => { this.users.set(users); this.loading.set(false); },
      error: () => this.loading.set(false),
    });
  }
}

// Dumb component - pure presentation, inputs/outputs only
@Component({
  selector: 'app-user-list',
  template: `
    @if (loading()) {
      <app-spinner />
    } @else {
      @for (user of users(); track user.id) {
        <div class="user-row" (click)="userSelected.emit(user)">
          {{ user.name }} - {{ user.email }}
        </div>
      } @empty {
        <p>No users found.</p>
      }
    }
  `,
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class UserListComponent {
  readonly users = input.required<User[]>();
  readonly loading = input(false);
  readonly userSelected = output<User>();
}
```

### Dependency Injection

**Prefer `inject()` over constructor injection:**
```typescript
// [GOOD] inject() function
@Injectable({ providedIn: 'root' })
export class UserService {
  private readonly http = inject(HttpClient);
  private readonly config = inject(APP_CONFIG);
  private readonly errorHandler = inject(ErrorHandlerService);
}

// [AVOID] Constructor injection (verbose with many deps)
@Injectable({ providedIn: 'root' })
export class UserService {
  constructor(
    private readonly http: HttpClient,
    @Inject(APP_CONFIG) private readonly config: AppConfig,
    private readonly errorHandler: ErrorHandlerService,
  ) {}
}
```

### Routing with Lazy Loading

```typescript
// app.routes.ts
export const appRoutes: Routes = [
  { path: '', redirectTo: 'dashboard', pathMatch: 'full' },
  {
    path: 'dashboard',
    loadComponent: () =>
      import('./features/dashboard/dashboard.component').then(m => m.DashboardComponent),
  },
  {
    path: 'users',
    loadChildren: () =>
      import('./features/users/users.routes').then(m => m.userRoutes),
    canActivate: [authGuard],
  },
];

// features/users/users.routes.ts
export const userRoutes: Routes = [
  { path: '', component: UserListComponent },
  { path: ':id', component: UserDetailComponent },
];
```

---

## Error Handling

### Global Error Handler

```typescript
import { ErrorHandler, Injectable, inject } from '@angular/core';
import { LoggingService } from '../logging/logging.service';

@Injectable()
export class GlobalErrorHandler implements ErrorHandler {
  private readonly logger = inject(LoggingService);

  handleError(error: unknown): void {
    const message = error instanceof Error ? error.message : 'Unknown error';
    const stack = error instanceof Error ? error.stack : undefined;

    this.logger.error('Unhandled error', { message, stack });

    // Show user-friendly notification
    // Do NOT expose stack traces or internal details to users
  }
}

// Register in app.config.ts
export const appConfig: ApplicationConfig = {
  providers: [
    { provide: ErrorHandler, useClass: GlobalErrorHandler },
  ],
};
```

### HTTP Error Interceptor

```typescript
import { HttpInterceptorFn, HttpErrorResponse } from '@angular/common/http';
import { inject } from '@angular/core';
import { catchError, throwError } from 'rxjs';
import { Router } from '@angular/router';

export const errorInterceptor: HttpInterceptorFn = (req, next) => {
  const router = inject(Router);
  const logger = inject(LoggingService);

  return next(req).pipe(
    catchError((error: HttpErrorResponse) => {
      switch (error.status) {
        case 401:
          router.navigate(['/login']);
          break;
        case 403:
          router.navigate(['/forbidden']);
          break;
        case 404:
          logger.warn('Resource not found', { url: req.url });
          break;
        case 429:
          logger.warn('Rate limit exceeded', { url: req.url });
          break;
        case 500:
        default:
          logger.error('Server error', {
            url: req.url,
            status: error.status,
            message: error.message,
          });
      }
      return throwError(() => error);
    }),
  );
};

// Register in app.config.ts
provideHttpClient(withInterceptors([errorInterceptor]));
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
```

### RxJS Error Handling

```typescript
// [GOOD] Explicit error handling in service
getUsers(): Observable<User[]> {
  return this.http.get<ApiResponse<User[]>>('/api/users').pipe(
    map(response => response.data),
    retry({ count: 2, delay: 1000 }),
    catchError(error => {
      this.logger.error('Failed to fetch users', error);
      return of([]); // Return fallback value
    }),
  );
}

// [BAD] Swallowing errors silently
getUsers(): Observable<User[]> {
  return this.http.get<User[]>('/api/users').pipe(
    catchError(() => of([])), // No logging, no context
  );
}

// [BAD] Not handling errors at all
getUsers(): Observable<User[]> {
  return this.http.get<User[]>('/api/users'); // Will crash on error
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
- All public services: 100% coverage

### Component Testing

```typescript
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';
import { UserListComponent } from './user-list.component';

describe('UserListComponent', () => {
  let component: UserListComponent;
  let fixture: ComponentFixture<UserListComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [UserListComponent],
    }).compileComponents();

    fixture = TestBed.createComponent(UserListComponent);
    component = fixture.componentInstance;
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('should display users when provided', () => {
    // Arrange
    const users: User[] = [
      { id: 1, email: 'john@example.com', name: 'John Doe', role: UserRole.User, createdAt: '' },
      { id: 2, email: 'jane@example.com', name: 'Jane Doe', role: UserRole.Admin, createdAt: '' },
    ];

    // Act
    fixture.componentRef.setInput('users', users);
    fixture.componentRef.setInput('loading', false);
    fixture.detectChanges();

    // Assert
    const rows = fixture.nativeElement.querySelectorAll('.user-row');
    expect(rows.length).toBe(2);
    expect(rows[0].textContent).toContain('John Doe');
  });

  it('should show spinner when loading', () => {
    fixture.componentRef.setInput('users', []);
    fixture.componentRef.setInput('loading', true);
    fixture.detectChanges();

    const spinner = fixture.nativeElement.querySelector('app-spinner');
    expect(spinner).toBeTruthy();
  });

  it('should emit userSelected when row is clicked', () => {
    const user: User = { id: 1, email: 'john@example.com', name: 'John', role: UserRole.User, createdAt: '' };
    fixture.componentRef.setInput('users', [user]);
    fixture.componentRef.setInput('loading', false);
    fixture.detectChanges();

    // Arrange spy
    const spy = jest.spyOn(component.userSelected, 'emit');

    // Act
    const row = fixture.nativeElement.querySelector('.user-row');
    row.click();

    // Assert
    expect(spy).toHaveBeenCalledWith(user);
  });
});
```

### Service Testing

```typescript
import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';
import { UserService } from './user.service';

describe('UserService', () => {
  let service: UserService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        UserService,
        provideHttpClient(),
        provideHttpClientTesting(),
      ],
    });

    service = TestBed.inject(UserService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.verify(); // Ensure no outstanding requests
  });

  describe('getUsers', () => {
    it('should return users from API', () => {
      // Arrange
      const mockUsers: User[] = [
        { id: 1, email: 'test@example.com', name: 'Test', role: UserRole.User, createdAt: '' },
      ];

      // Act
      service.getUsers().subscribe(users => {
        // Assert
        expect(users.length).toBe(1);
        expect(users[0].email).toBe('test@example.com');
      });

      // Respond to HTTP request
      const req = httpMock.expectOne('/api/users');
      expect(req.request.method).toBe('GET');
      req.flush({ data: mockUsers });
    });

    it('should return empty array on error', () => {
      service.getUsers().subscribe(users => {
        expect(users).toEqual([]);
      });

      const req = httpMock.expectOne('/api/users');
      req.flush('Error', { status: 500, statusText: 'Server Error' });
    });
  });
});
```

### E2E Testing (Cypress)

```typescript
// cypress/e2e/users.cy.ts
describe('User Management', () => {
  beforeEach(() => {
    cy.intercept('GET', '/api/users', { fixture: 'users.json' }).as('getUsers');
    cy.visit('/users');
    cy.wait('@getUsers');
  });

  it('should display user list', () => {
    cy.get('[data-testid="user-row"]').should('have.length.gt', 0);
  });

  it('should navigate to user detail on click', () => {
    cy.get('[data-testid="user-row"]').first().click();
    cy.url().should('include', '/users/');
  });

  it('should filter users by role', () => {
    cy.get('[data-testid="role-filter"]').select('admin');
    cy.get('[data-testid="user-row"]').each(($row) => {
      cy.wrap($row).should('contain', 'Admin');
    });
  });
});
```

---

## Security

### Angular Built-in Protections

**XSS Protection:**
```typescript
// Angular automatically sanitizes values in templates.
// The following is safe - Angular escapes the HTML:
@Component({
  template: `<p>{{ userInput }}</p>`,
})
export class SafeComponent {
  userInput = '<script>alert("xss")</script>'; // Rendered as text, not executed
}

// [BAD] Never bypass security unless absolutely necessary
// If you must, document the reason
@Component({
  template: `<div [innerHTML]="trustedHtml"></div>`,
})
export class RiskyComponent {
  private readonly sanitizer = inject(DomSanitizer);

  // [BAD] Avoid this pattern
  trustedHtml = this.sanitizer.bypassSecurityTrustHtml(unsafeContent);
}
```

**CSRF/XSRF Protection:**
```typescript
// Angular's HttpClient includes XSRF protection by default
// Configure the cookie and header names if your backend differs
provideHttpClient(
  withXsrfConfiguration({
    cookieName: 'XSRF-TOKEN',
    headerName: 'X-XSRF-TOKEN',
  }),
);
```

**Content Security Policy (CSP):**
```html
<!-- index.html -->
<meta http-equiv="Content-Security-Policy"
  content="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline';">
```

**Route Guards for Authorization:**
```typescript
export const authGuard: CanActivateFn = () => {
  const authService = inject(AuthService);
  const router = inject(Router);

  if (authService.isAuthenticated()) {
    return true;
  }

  return router.createUrlTree(['/login']);
};

export const roleGuard: CanActivateFn = (route) => {
  const authService = inject(AuthService);
  const requiredRole = route.data['role'] as string;

  return authService.hasRole(requiredRole);
};

// Usage in routes
{
  path: 'admin',
  loadComponent: () => import('./admin.component'),
  canActivate: [authGuard, roleGuard],
  data: { role: 'admin' },
}
```

**Security Checklist:**
- [ ] No secrets in client-side code or version control
- [ ] All user input sanitized (Angular does this by default)
- [ ] Never use `bypassSecurityTrust*` without documented justification
- [ ] HTTPS only for all API communication
- [ ] CSRF/XSRF protection enabled
- [ ] CSP headers configured
- [ ] Route guards on all protected routes
- [ ] JWT tokens stored in HttpOnly cookies (not localStorage)
- [ ] No sensitive data in URL parameters
- [ ] Dependencies scanned for vulnerabilities

---

## Performance

### Change Detection

**Use OnPush change detection for all presentational components:**
```typescript
@Component({
  selector: 'app-user-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="card">
      <h3>{{ user().name }}</h3>
      <p>{{ user().email }}</p>
    </div>
  `,
})
export class UserCardComponent {
  readonly user = input.required<User>();
}
```

### Signals and Computed

```typescript
// [GOOD] Use signals for reactive state
@Component({ /* ... */ })
export class DashboardComponent {
  private readonly analyticsService = inject(AnalyticsService);

  protected users = signal<User[]>([]);
  protected searchTerm = signal('');

  // Computed values are memoized - only recalculate when dependencies change
  protected filteredUsers = computed(() => {
    const term = this.searchTerm().toLowerCase();
    return this.users().filter(u =>
      u.name.toLowerCase().includes(term) || u.email.toLowerCase().includes(term)
    );
  });

  protected totalCount = computed(() => this.filteredUsers().length);
}
```

### Lazy Loading

```typescript
// [GOOD] Lazy load feature routes
{
  path: 'reports',
  loadChildren: () => import('./features/reports/reports.routes').then(m => m.reportRoutes),
}

// [GOOD] Defer heavy components in templates
@Component({
  template: `
    <h1>Dashboard</h1>
    @defer (on viewport) {
      <app-heavy-chart [data]="chartData()" />
    } @placeholder {
      <div class="chart-placeholder">Loading chart...</div>
    }
  `,
})
export class DashboardComponent { }
```

### Track By for Loops

```typescript
// [GOOD] Always use track in @for blocks
@for (user of users(); track user.id) {
  <app-user-card [user]="user" />
}

// [BAD] No track expression - Angular re-renders all items on change
@for (user of users(); track $index) {
  <app-user-card [user]="user" />
}
```

### Bundle Size

```typescript
// [GOOD] Import only what you need
import { map, filter } from 'rxjs/operators';

// [BAD] Importing entire library
import * as _ from 'lodash';

// [GOOD] Import specific lodash functions
import debounce from 'lodash-es/debounce';
```

### Image Optimization

```typescript
// [GOOD] Use NgOptimizedImage
import { NgOptimizedImage } from '@angular/common';

@Component({
  imports: [NgOptimizedImage],
  template: `
    <img ngSrc="/assets/hero.jpg" width="800" height="400" priority />
    <img ngSrc="{{ user.avatar }}" width="48" height="48" />
  `,
})
export class HeroComponent { }
```

---

## Dependencies

### Dependency Management

**Principles:**
- Use lockfile (`package-lock.json` or `pnpm-lock.yaml`)
- Pin exact versions for production dependencies
- Regular security updates with `ng update` and `npm audit`
- Minimize third-party dependencies

**package.json:**
```json
{
  "dependencies": {
    "@angular/core": "~18.0.0",
    "@angular/router": "~18.0.0",
    "rxjs": "~7.8.0"
  },
  "devDependencies": {
    "@angular-eslint/eslint-plugin": "~18.0.0",
    "@angular-eslint/template-parser": "~18.0.0",
    "cypress": "~13.0.0",
    "eslint": "~9.0.0",
    "prettier": "~3.3.0"
  }
}
```

**Security Scanning:**
- Run `npm audit` regularly
- Update dependencies with `ng update`
- Review dependency licenses
- Audit transitive dependencies
- Use `npm audit fix` for automated patching

---

## Logging

### Centralized Logging Service

```typescript
import { Injectable, inject } from '@angular/core';
import { environment } from '../../environments/environment';

export enum LogLevel {
  Debug = 0,
  Info = 1,
  Warn = 2,
  Error = 3,
}

@Injectable({ providedIn: 'root' })
export class LoggingService {
  private readonly level = environment.production ? LogLevel.Warn : LogLevel.Debug;

  debug(message: string, context?: Record<string, unknown>): void {
    if (this.level <= LogLevel.Debug) {
      console.debug(this.formatMessage('DEBUG', message, context));
    }
  }

  info(message: string, context?: Record<string, unknown>): void {
    if (this.level <= LogLevel.Info) {
      console.info(this.formatMessage('INFO', message, context));
    }
  }

  warn(message: string, context?: Record<string, unknown>): void {
    if (this.level <= LogLevel.Warn) {
      console.warn(this.formatMessage('WARN', message, context));
    }
  }

  error(message: string, context?: Record<string, unknown>): void {
    console.error(this.formatMessage('ERROR', message, context));

    // In production, send to external error tracking service
    if (environment.production) {
      this.sendToErrorTracker(message, context);
    }
  }

  private formatMessage(
    level: string,
    message: string,
    context?: Record<string, unknown>,
  ): string {
    const entry = {
      timestamp: new Date().toISOString(),
      level,
      message,
      ...context,
    };
    return JSON.stringify(entry);
  }

  private sendToErrorTracker(message: string, context?: Record<string, unknown>): void {
    // Integrate with Sentry, Datadog, etc.
  }
}
```

### Logging Best Practices

**What to Log:**
- HTTP errors (status, URL, method)
- Authentication events (login, logout, failed attempts)
- Critical user actions (form submissions, navigation)
- Performance metrics (component load times)
- Errors with full context

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

**Angular-Specific Quality:**
- [ ] Components are focused on presentation
- [ ] Business logic lives in services
- [ ] OnPush change detection on presentational components
- [ ] Signals used for state, computed for derived values
- [ ] Templates are simple (complex logic in TypeScript)
- [ ] `inject()` used over constructor injection
- [ ] Lifecycle hooks implement their interfaces
- [ ] `readonly` on inputs, outputs, and queries
- [ ] `protected` on template-only members

**Testing:**
- [ ] Tests included for new code
- [ ] Tests cover edge cases and error paths
- [ ] Coverage meets minimum (90%)
- [ ] E2E tests for critical user flows

**Security:**
- [ ] No secrets in client code
- [ ] No `bypassSecurityTrust*` without justification
- [ ] Route guards on protected routes
- [ ] CSRF protection configured

**Performance:**
- [ ] Lazy loading for feature routes
- [ ] `track` expression in all `@for` blocks
- [ ] No unnecessary subscriptions (memory leaks)
- [ ] Bundle size checked for new dependencies

### Review Process

**Requirements:**
- Minimum 2 approvals for production code
- All automated checks must pass
- No unresolved comments
- Squash commits before merge

---

## Tooling

### Required Tools

**Code Formatting:**
- `Prettier` - Opinionated code formatter
- `@angular-eslint` - Angular-specific linting

**Linting:**
- `ESLint` with `@angular-eslint/eslint-plugin`
- `@angular-eslint/template-parser` for template linting

**Testing:**
- `Karma` + `Jasmine` (or `Vitest`) - Unit testing
- `Cypress` - End-to-end testing

**ESLint Configuration:**
```javascript
// eslint.config.js
import angular from '@angular-eslint/eslint-plugin';
import angularTemplate from '@angular-eslint/eslint-plugin-template';
import tsParser from '@typescript-eslint/parser';
import templateParser from '@angular-eslint/template-parser';

export default [
  {
    files: ['**/*.ts'],
    languageOptions: { parser: tsParser },
    plugins: { '@angular-eslint': angular },
    rules: {
      '@angular-eslint/component-class-suffix': 'error',
      '@angular-eslint/directive-class-suffix': 'error',
      '@angular-eslint/no-empty-lifecycle-method': 'warn',
      '@angular-eslint/prefer-on-push-component-change-detection': 'warn',
      '@angular-eslint/use-lifecycle-interface': 'error',
    },
  },
  {
    files: ['**/*.html'],
    languageOptions: { parser: templateParser },
    plugins: { '@angular-eslint/template': angularTemplate },
    rules: {
      '@angular-eslint/template/no-negated-async': 'error',
      '@angular-eslint/template/use-track-by-function': 'warn',
    },
  },
];
```

**Prettier Configuration:**
```json
{
  "singleQuote": true,
  "trailingComma": "all",
  "printWidth": 100,
  "tabWidth": 2,
  "semi": true,
  "bracketSpacing": true,
  "arrowParens": "avoid"
}
```

### CI/CD Pipeline

**Required checks before merge:**
```bash
# Lint
npx ng lint

# Format check
npx prettier --check "src/**/*.{ts,html,css,json}"

# Unit tests with coverage
npx ng test --watch=false --code-coverage

# E2E tests
npx cypress run

# Build (ensures AOT compilation passes)
npx ng build --configuration production

# Bundle size check
npx ng build --configuration production --stats-json
npx webpack-bundle-analyzer dist/stats.json --mode static

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
   - Add TSDoc for public APIs
   - Use signals and computed for state
   - Keep templates simple
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
# Generate component
npx ng generate component features/users/user-card

# Generate service
npx ng generate service features/users/services/user

# Lint
npx ng lint

# Format
npx prettier --write "src/**/*.{ts,html,css,json}"

# Run tests
npx ng test

# Run tests with coverage
npx ng test --watch=false --code-coverage

# Run E2E tests
npx cypress open    # Interactive
npx cypress run     # Headless

# Build for production
npx ng build --configuration production

# Serve locally
npx ng serve

# Update Angular
npx ng update @angular/core @angular/cli

# Audit dependencies
npm audit
```

---

## References

- [Official Angular Style Guide](https://angular.dev/style-guide)
- [Google TypeScript Style Guide](https://google.github.io/styleguide/tsguide.html)
- [Angular Security Guide](https://angular.dev/best-practices/security)
- [Angular Testing Guide](https://angular.dev/guide/testing)
- [Angular Performance Guide](https://angular.dev/best-practices/runtime-performance)
- [RxJS Documentation](https://rxjs.dev/)
- [Cypress Documentation](https://docs.cypress.io/)

---

**Questions or suggestions?** Update this document through team discussion and code review.

**Version History:**
- v1.0 (2026) - Initial enterprise-grade guidelines
