# Python Coding Guidelines - Enterprise Grade

**Version:** 1.0  
**Last Updated:** 2024  
**Target:** Production Python systems requiring high quality, security, and maintainability

---

## Table of Contents

1. [Philosophy](#philosophy)
2. [Code Style](#code-style)
3. [Type Hints](#type-hints)
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

Follow the **Zen of Python** (PEP 20):
- Explicit is better than implicit
- Simple is better than complex
- Readability counts
- Errors should never pass silently
- In the face of ambiguity, refuse the temptation to guess

**Core Principles:**
- Write code for humans first, machines second
- Optimize for maintainability over cleverness
- Test-driven development (TDD) is mandatory
- Security is not optional
- Performance matters, but measure before optimizing

---

## Code Style

### Base Standard

Follow **PEP 8** with these specifications:

**Formatting:**
- Line length: 88 characters (Black default)
- Indentation: 4 spaces (never tabs)
- Encoding: UTF-8
- Line endings: LF (Unix style)

**Naming Conventions:**
```python
# Variables and functions
user_name = "John"
def calculate_total(): pass

# Classes
class UserAccount: pass

# Constants
MAX_RETRY_COUNT = 3
API_BASE_URL = "https://api.example.com"

# Private (internal use)
_internal_helper = None
def _private_method(): pass

# Protected (subclass use)
class Base:
    def _protected_method(self): pass
```

**Import Ordering:**
```python
# 1. Standard library
import os
import sys
from typing import Optional

# 2. Third-party packages
import requests
from fastapi import FastAPI

# 3. Local application
from app.models import User
from app.services import AuthService
```

**Avoid:**
- Wildcard imports: `from module import *`
- Relative imports beyond parent: `from ...module import x`
- Single-letter variables except in comprehensions/lambdas

---

## Type Hints

**Mandatory for all function signatures:**

```python
from typing import Optional, List, Dict, Any, Union
from collections.abc import Sequence

def process_users(
    users: List[User],
    filter_active: bool = True,
    limit: Optional[int] = None
) -> Dict[str, Any]:
    """Process user list with optional filtering."""
    pass

# Use modern syntax (Python 3.10+)
def get_user(user_id: int) -> User | None:
    """Returns user or None if not found."""
    pass

# Generic types
from typing import TypeVar, Generic

T = TypeVar('T')

class Repository(Generic[T]):
    def get(self, id: int) -> T | None:
        pass
```

**Type Checking:**
- Run `mypy` in strict mode
- No `Any` types without justification
- Use `Protocol` for structural typing
- Use `TypedDict` for dictionary schemas

---

## Documentation

### Docstrings (Google Style)

**Required for:**
- All public modules, classes, functions
- Complex private functions

**Format:**
```python
def create_user(
    email: str,
    name: str,
    role: str = "user"
) -> User:
    """Creates a new user in the system.
    
    This function validates the email format, checks for duplicates,
    and assigns default permissions based on the role.
    
    Args:
        email: Valid email address (RFC 5322 compliant)
        name: User's full name (2-100 characters)
        role: User role, one of: 'user', 'admin', 'moderator'
    
    Returns:
        Newly created User instance with generated ID
    
    Raises:
        ValueError: If email format is invalid or name is empty
        DuplicateUserError: If email already exists
        DatabaseError: If database operation fails
    
    Example:
        >>> user = create_user("john@example.com", "John Doe")
        >>> print(user.id)
        12345
    """
    pass

class UserRepository:
    """Repository for User entity database operations.
    
    Provides CRUD operations and query methods for User entities.
    Uses connection pooling and prepared statements for performance.
    
    Attributes:
        connection: Database connection instance
        cache: Optional Redis cache for read operations
    """
    pass
```

**Module Docstrings:**
```python
"""User authentication and authorization module.

This module provides:
- JWT token generation and validation
- Password hashing with bcrypt
- Role-based access control (RBAC)
- Session management

Example:
    from app.auth import authenticate_user
    
    user = authenticate_user(email, password)
    token = generate_token(user)
"""
```

---

## Architecture

### Clean Architecture Principles

**Layer Separation:**
```
┌─────────────────────────────────────┐
│   Presentation (API/CLI/UI)         │
├─────────────────────────────────────┤
│   Application (Use Cases)           │
├─────────────────────────────────────┤
│   Domain (Business Logic)           │
├─────────────────────────────────────┤
│   Infrastructure (DB/External APIs) │
└─────────────────────────────────────┘
```

**Dependency Rule:** Inner layers never depend on outer layers

**Project Structure:**
```
src/
├── domain/           # Business entities and logic
│   ├── entities/
│   ├── value_objects/
│   └── exceptions/
├── application/      # Use cases and interfaces
│   ├── use_cases/
│   └── interfaces/
├── infrastructure/   # External concerns
│   ├── database/
│   ├── api_clients/
│   └── cache/
└── presentation/     # Controllers/handlers
    ├── api/
    └── cli/
```

### Design Patterns

**Prefer:**
- Dependency Injection over global state
- Composition over inheritance
- Factory patterns for complex object creation
- Repository pattern for data access
- Strategy pattern for algorithms

**Use dataclasses for data containers:**
```python
from dataclasses import dataclass, field
from datetime import datetime

@dataclass(frozen=True)  # Immutable
class User:
    id: int
    email: str
    created_at: datetime = field(default_factory=datetime.utcnow)
    
    def __post_init__(self):
        """Validate after initialization."""
        if not self.email or "@" not in self.email:
            raise ValueError("Invalid email")
```

---

## Error Handling

### Exception Hierarchy

**Create domain-specific exceptions:**
```python
class AppError(Exception):
    """Base exception for all application errors."""
    pass

class ValidationError(AppError):
    """Raised when input validation fails."""
    pass

class NotFoundError(AppError):
    """Raised when requested resource doesn't exist."""
    pass

class AuthenticationError(AppError):
    """Raised when authentication fails."""
    pass

class DatabaseError(AppError):
    """Raised when database operation fails."""
    pass
```

### Error Handling Rules

**Never:**
```python
# [BAD] Bare except
try:
    risky_operation()
except:
    pass

# [BAD] Catching Exception without re-raising
try:
    operation()
except Exception:
    log.error("Failed")
    # Swallows error!

# [BAD] Using exceptions for control flow
try:
    user = users[user_id]
except KeyError:
    user = None
```

**Always:**
```python
# [GOOD] Specific exceptions
try:
    user = get_user(user_id)
except NotFoundError:
    return None
except DatabaseError as e:
    log.error(f"Database error: {e}")
    raise

# [GOOD] Context managers for resources
with open(file_path) as f:
    data = f.read()

# [GOOD] Explicit error messages
if not email:
    raise ValidationError(
        f"Email is required for user creation. Got: {email!r}"
    )
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
- All public APIs: 100% coverage

### Test Structure

```python
import pytest
from app.services import UserService
from app.exceptions import ValidationError

class TestUserService:
    """Test suite for UserService."""
    
    @pytest.fixture
    def service(self):
        """Provides UserService instance for tests."""
        return UserService(db=MockDatabase())
    
    def test_create_user_success(self, service):
        """Should create user with valid data."""
        # Arrange
        email = "test@example.com"
        name = "Test User"
        
        # Act
        user = service.create_user(email, name)
        
        # Assert
        assert user.email == email
        assert user.name == name
        assert user.id is not None
    
    def test_create_user_invalid_email(self, service):
        """Should raise ValidationError for invalid email."""
        with pytest.raises(ValidationError, match="Invalid email"):
            service.create_user("invalid-email", "Test User")
    
    @pytest.mark.parametrize("email,name,expected_error", [
        ("", "Name", "Email is required"),
        ("test@example.com", "", "Name is required"),
        ("invalid", "Name", "Invalid email format"),
    ])
    def test_create_user_validation(self, service, email, name, expected_error):
        """Should validate all input fields."""
        with pytest.raises(ValidationError, match=expected_error):
            service.create_user(email, name)
```

### Test Types

**Unit Tests:**
- Test single function/method in isolation
- Mock all external dependencies
- Fast execution (< 1ms per test)

**Integration Tests:**
- Test multiple components together
- Use test database/containers
- Verify external integrations

**Property-Based Tests:**
```python
from hypothesis import given, strategies as st

@given(st.emails(), st.text(min_size=1, max_size=100))
def test_create_user_with_any_valid_input(email, name):
    """Should handle any valid email and name combination."""
    user = create_user(email, name)
    assert user.email == email
    assert user.name == name
```

---

## Security

### OWASP Top 10 Compliance

**Input Validation:**
```python
import re
from html import escape

def sanitize_input(user_input: str) -> str:
    """Sanitizes user input to prevent injection attacks."""
    # Remove null bytes
    cleaned = user_input.replace('\x00', '')
    # Escape HTML
    cleaned = escape(cleaned)
    return cleaned

def validate_email(email: str) -> bool:
    """Validates email format (RFC 5322 simplified)."""
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return bool(re.match(pattern, email))
```

**SQL Injection Prevention:**
```python
# [GOOD] Use parameterized queries
cursor.execute(
    "SELECT * FROM users WHERE email = %s",
    (email,)
)

# [BAD] Never string concatenation
cursor.execute(f"SELECT * FROM users WHERE email = '{email}'")
```

**Secrets Management:**
```python
import os
from functools import lru_cache

@lru_cache
def get_secret(key: str) -> str:
    """Retrieves secret from environment or secret manager."""
    value = os.getenv(key)
    if not value:
        raise ValueError(f"Secret {key} not found")
    return value

# [BAD] Never hardcode secrets
API_KEY = "sk-1234567890abcdef"  # NEVER DO THIS

# [GOOD] Use environment variables
API_KEY = get_secret("API_KEY")
```

**Password Hashing:**
```python
import bcrypt

def hash_password(password: str) -> str:
    """Hashes password using bcrypt."""
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(password.encode(), salt).decode()

def verify_password(password: str, hashed: str) -> bool:
    """Verifies password against hash."""
    return bcrypt.checkpw(password.encode(), hashed.encode())
```

**Security Checklist:**
- [ ] No secrets in code or version control
- [ ] All inputs validated and sanitized
- [ ] Parameterized queries for SQL
- [ ] HTTPS only for external communication
- [ ] Authentication on all protected endpoints
- [ ] Rate limiting on public APIs
- [ ] Security headers configured
- [ ] Dependencies scanned for vulnerabilities

---

## Performance

### Optimization Rules

1. **Measure first:** Use profilers before optimizing
2. **Optimize algorithms:** O(n²) → O(n log n) before micro-optimizations
3. **Cache expensive operations:** Database queries, API calls, computations
4. **Use appropriate data structures:** dict for lookups, set for membership

### Performance Patterns

**Use `__slots__` for memory-intensive classes:**
```python
class Point:
    __slots__ = ('x', 'y')
    
    def __init__(self, x: float, y: float):
        self.x = x
        self.y = y
```

**Lazy evaluation:**
```python
from functools import cached_property

class DataProcessor:
    @cached_property
    def expensive_computation(self) -> dict:
        """Computed once, cached thereafter."""
        return self._process_large_dataset()
```

**Batch operations:**
```python
# [BAD] N+1 queries
for user_id in user_ids:
    user = db.get_user(user_id)
    process(user)

# [GOOD] Single batch query
users = db.get_users_batch(user_ids)
for user in users:
    process(user)
```

**Generators for large datasets:**
```python
def process_large_file(file_path: str):
    """Yields lines without loading entire file."""
    with open(file_path) as f:
        for line in f:
            yield process_line(line)
```

---

## Dependencies

### Dependency Management

**Principles:**
- Pin exact versions in production
- Use lock files (Poetry, pip-tools)
- Regular security updates
- Minimize dependency count

**Dependency Hygiene:**
```toml
# pyproject.toml
[project]
dependencies = [
    "requests>=2.31.0,<3.0.0",
    "pydantic>=2.0.0,<3.0.0",
]

[project.optional-dependencies]
dev = [
    "pytest",
    "mypy",
    "black",
    "ruff",
]
```

**Security Scanning:**
- Run vulnerability scanners regularly
- Update dependencies monthly
- Review dependency licenses
- Audit transitive dependencies

---

## Logging

### Structured Logging

**Use structured logs (JSON) for production:**
```python
import logging
import json
from datetime import datetime
from typing import Any

class StructuredLogger:
    def __init__(self, name: str):
        self.logger = logging.getLogger(name)
    
    def log(
        self,
        level: str,
        message: str,
        **context: Any
    ) -> None:
        """Logs structured message with context."""
        log_entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "level": level,
            "message": message,
            "context": context,
        }
        self.logger.log(
            getattr(logging, level.upper()),
            json.dumps(log_entry)
        )

# Usage
logger = StructuredLogger(__name__)
logger.log(
    "info",
    "User created",
    user_id=user.id,
    email=user.email,
    correlation_id=request.correlation_id
)
```

### Logging Best Practices

**Log Levels:**
- `DEBUG`: Detailed diagnostic information
- `INFO`: General informational messages
- `WARNING`: Warning messages for recoverable issues
- `ERROR`: Error messages for failures
- `CRITICAL`: Critical issues requiring immediate attention

**What to Log:**
- Request/response for external APIs
- Authentication attempts
- Database operations (with timing)
- Business events (user created, order placed)
- Errors with full context

**What NOT to Log:**
- Passwords or secrets
- Personal Identifiable Information (PII)
- Credit card numbers
- Session tokens

**Correlation IDs:**
```python
import uuid
from contextvars import ContextVar

correlation_id: ContextVar[str] = ContextVar('correlation_id')

def set_correlation_id() -> str:
    """Sets correlation ID for request tracing."""
    cid = str(uuid.uuid4())
    correlation_id.set(cid)
    return cid

def get_correlation_id() -> str:
    """Gets current correlation ID."""
    return correlation_id.get()
```

---

## Code Review

### Review Checklist

**Functionality:**
- [ ] Code solves the stated problem
- [ ] Edge cases handled
- [ ] Error handling appropriate
- [ ] No obvious bugs

**Quality:**
- [ ] Follows style guide (Black/Ruff pass)
- [ ] Type hints present and correct
- [ ] Docstrings for public APIs
- [ ] No code duplication
- [ ] Appropriate abstractions

**Testing:**
- [ ] Tests included for new code
- [ ] Tests cover edge cases
- [ ] Coverage meets minimum (90%)
- [ ] Tests are readable and maintainable

**Security:**
- [ ] No secrets in code
- [ ] Input validation present
- [ ] No SQL injection vulnerabilities
- [ ] Dependencies up to date

**Performance:**
- [ ] No obvious performance issues
- [ ] Appropriate data structures
- [ ] Database queries optimized
- [ ] Caching where appropriate

### Review Process

**Requirements:**
- Minimum 2 approvals for production code
- All automated checks must pass
- No unresolved comments
- Squash commits before merge

**Review Guidelines:**
- Be respectful and constructive
- Explain the "why" behind suggestions
- Distinguish between blocking and non-blocking comments
- Approve if code improves overall quality

---

## Tooling

### Required Tools

**Code Formatting:**
- `black` - Opinionated code formatter
- `isort` - Import sorting (or use Ruff)

**Linting:**
- `ruff` - Fast Python linter (replaces flake8, pylint, isort)
- `mypy` - Static type checker

**Testing:**
- `pytest` - Testing framework
- `pytest-cov` - Coverage reporting
- `hypothesis` - Property-based testing

**Security:**
- `bandit` - Security vulnerability scanner
- `safety` - Dependency vulnerability checker

**Pre-commit Hooks:**
```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/psf/black
    hooks:
      - id: black
  
  - repo: https://github.com/charliermarsh/ruff-pre-commit
    hooks:
      - id: ruff
        args: [--fix]
  
  - repo: https://github.com/pre-commit/mirrors-mypy
    hooks:
      - id: mypy
        additional_dependencies: [types-requests]
  
  - repo: https://github.com/PyCQA/bandit
    hooks:
      - id: bandit
        args: [-c, pyproject.toml]
```

### Configuration

**pyproject.toml:**
```toml
[tool.black]
line-length = 88
target-version = ['py311']

[tool.ruff]
line-length = 88
target-version = "py311"
select = [
    "E",   # pycodestyle errors
    "W",   # pycodestyle warnings
    "F",   # pyflakes
    "I",   # isort
    "N",   # pep8-naming
    "B",   # flake8-bugbear
    "C90", # mccabe complexity
    "S",   # bandit security
]

[tool.mypy]
python_version = "3.11"
strict = true
warn_return_any = true
warn_unused_configs = true
disallow_untyped_defs = true

[tool.pytest.ini_options]
testpaths = ["tests"]
python_files = ["test_*.py"]
addopts = """
    --cov=src
    --cov-report=term-missing
    --cov-report=html
    --cov-fail-under=90
"""

[tool.coverage.run]
omit = ["*/tests/*", "*/migrations/*"]

[tool.bandit]
exclude_dirs = ["tests", "venv"]
skips = ["B101"]  # Skip assert_used in tests
```

### CI/CD Pipeline

**Required checks before merge:**
```bash
# Format check
black --check .

# Linting
ruff check .

# Type checking
mypy src/

# Security scan
bandit -r src/

# Tests with coverage
pytest --cov=src --cov-fail-under=90

# Dependency vulnerabilities
safety check
```

---

## Quick Reference

### Daily Workflow

1. **Before coding:**
   - Pull latest changes
   - Create feature branch
   - Write failing test (TDD)

2. **While coding:**
   - Follow type hints
   - Add docstrings
   - Run tests frequently
   - Commit small, logical changes

3. **Before pushing:**
   - Run full test suite
   - Check coverage
   - Run linters
   - Update documentation

4. **Code review:**
   - Address all comments
   - Ensure CI passes
   - Get 2 approvals
   - Squash and merge

### Common Commands

```bash
# Format code
black .

# Lint and fix
ruff check . --fix

# Type check
mypy src/

# Run tests
pytest

# Run tests with coverage
pytest --cov=src --cov-report=html

# Security scan
bandit -r src/

# Install pre-commit hooks
pre-commit install

# Run all pre-commit checks
pre-commit run --all-files
```

---

## References

- [PEP 8 - Style Guide](https://pep8.org/)
- [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html)
- [The Hitchhiker's Guide to Python](https://docs.python-guide.org/)
- [Effective Python](https://effectivepython.com/)
- [Clean Architecture](https://blog.cleancoder.com/uncle-bob/2012/08/13/the-clean-architecture.html)
- [OWASP Python Security](https://cheatsheetseries.owasp.org/cheatsheets/Python_Security_Cheat_Sheet.html)
- [Python Packaging Guide](https://packaging.python.org/)

---

**Questions or suggestions?** Update this document through team discussion and code review.

**Version History:**
- v1.0 (2024) - Initial enterprise-grade guidelines
