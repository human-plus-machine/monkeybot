# .NET / C# Coding Guidelines - Enterprise Grade

**Version:** 1.0  
**Last Updated:** 2026  
**Target:** Production .NET systems requiring high quality, security, and maintainability

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

Follow **SOLID principles** and Clean Architecture:
- Single Responsibility: Every class has one reason to change
- Open/Closed: Open for extension, closed for modification
- Liskov Substitution: Subtypes must be substitutable for their base types
- Interface Segregation: Prefer small, focused interfaces
- Dependency Inversion: Depend on abstractions, not concretions

**Core Principles:**
- Write code for humans first, machines second
- Correctness: Code must be resilient and correct even after multiple edits
- Consistency: Maintain uniform style across all code
- Test-driven development (TDD) is mandatory
- Security is not optional
- Performance matters, but measure before optimizing
- Use modern C# features and language idioms
- Favor composition over inheritance

---

## Code Style

### Base Standard

Follow the **Microsoft C# Coding Conventions** and **.NET Runtime Coding Style**:

**Formatting:**
- Line length: 120 characters (recommended)
- Indentation: 4 spaces (never tabs)
- Encoding: UTF-8
- Line endings: LF (Unix style)
- Braces: Allman style (opening brace on its own line)

**Naming Conventions:**
```csharp
// Namespaces - PascalCase with dot separation
namespace MyCompany.MyApp.Domain.Entities;

// Classes, records, structs, enums, delegates - PascalCase
public class UserAccount { }
public record UserDto(string Name, string Email);
public struct ValueCoordinate { }
public enum UserRole { Admin, User, Moderator }
public delegate void EventHandler(string message);

// Interfaces - PascalCase with "I" prefix
public interface IUserRepository { }
public interface IAuthService { }

// Methods, properties, events, constants - PascalCase
public string FullName { get; set; }
public void SendMessage() { }
public event Action EventProcessing;
public const int MaxRetryCount = 3;

// Parameters, local variables - camelCase
public void ProcessUser(string userName, int retryCount)
{
    var isActive = true;
    var currentUser = GetUser(userName);
}

// Private instance fields - camelCase with _ prefix
private readonly IUserRepository _userRepository;
private string _cachedName;

// Private static fields - camelCase with s_ prefix
private static IWorkerQueue s_workerQueue;

// Thread-static fields - camelCase with t_ prefix
[ThreadStatic]
private static TimeSpan t_timeSpan;

// Type parameters - PascalCase with T prefix
public interface IRepository<TEntity> where TEntity : class { }
public class Converter<TInput, TOutput> { }

// Test classes - ClassNameTests
public class UserAccountTests { }
```

**File-Scoped Namespaces:**
```csharp
// [GOOD] File-scoped namespace (one namespace per file)
namespace MyApp.Domain.Entities;

public class User
{
    // ...
}

// [AVOID] Block-scoped namespace (adds unnecessary nesting)
namespace MyApp.Domain.Entities
{
    public class User
    {
        // ...
    }
}
```

**Using Directives (outside namespace):**
```csharp
// 1. System namespaces
using System;
using System.Collections.Generic;
using System.Threading.Tasks;

// 2. Third-party namespaces
using Microsoft.Extensions.Logging;
using FluentValidation;

// 3. Application namespaces
using MyApp.Domain.Entities;
using MyApp.Application.Services;

namespace MyApp.Presentation.Controllers;
```

**Avoid:**
- Hungarian notation: `strName`, `intCount`
- Abbreviations (except widely known: `Id`, `Url`, `Http`)
- Single-letter variable names except in loops or lambdas
- Consecutive underscores in identifiers
- `var` when the type is not obvious from the right side

**Use `var` appropriately:**
```csharp
// [GOOD] Type is obvious
var message = "This is clearly a string.";
var users = new List<User>();
var service = new UserService();

// [BAD] Type is not obvious - use explicit type
int numberOfIterations = Convert.ToInt32(Console.ReadLine());
User currentUser = GetActiveUser();

// [GOOD] Always use var for LINQ queries (often anonymous types)
var seattleCustomers = from c in customers
                       where c.City == "Seattle"
                       select c.Name;
```

---

## Type Safety

### Nullable Reference Types

**Enable nullable context globally:**
```xml
<!-- .csproj -->
<PropertyGroup>
  <Nullable>enable</Nullable>
  <TreatWarningsAsErrors>true</TreatWarningsAsErrors>
</PropertyGroup>
```

```csharp
// Null safety enforced at compile time
public class UserService
{
    // Non-nullable: compiler warns if null assigned
    private readonly IUserRepository _userRepository;

    // Nullable: explicitly marked, caller must handle null
    public User? FindById(int id)
    {
        return _userRepository.GetById(id);
    }

    // Non-nullable return: guaranteed non-null
    public User GetById(int id)
    {
        return _userRepository.GetById(id)
            ?? throw new NotFoundException("User", id);
    }
}
```

### Records for DTOs

```csharp
// [GOOD] Immutable DTOs with records
public record CreateUserRequest(
    string Email,
    string Name,
    UserRole Role
);

public record UserResponse(
    int Id,
    string Email,
    string Name,
    string Role,
    DateTime CreatedAt
);

// Record with validation
public record EmailAddress
{
    public string Value { get; }

    public EmailAddress(string value)
    {
        if (string.IsNullOrWhiteSpace(value) || !value.Contains('@'))
        {
            throw new ArgumentException("Invalid email address", nameof(value));
        }
        Value = value;
    }
}
```

### Pattern Matching

```csharp
// [GOOD] Exhaustive pattern matching
public string FormatShape(Shape shape) => shape switch
{
    Circle c => $"Circle with radius {c.Radius}",
    Rectangle r => $"Rectangle {r.Width}x{r.Height}",
    Triangle t => $"Triangle with base {t.Base}",
    _ => throw new ArgumentOutOfRangeException(nameof(shape))
};

// [GOOD] Property pattern matching
public decimal CalculateDiscount(User user) => user switch
{
    { Role: UserRole.Admin } => 0.20m,
    { Role: UserRole.User, OrderCount: > 100 } => 0.15m,
    { Role: UserRole.User, OrderCount: > 10 } => 0.05m,
    _ => 0m
};

// [GOOD] is pattern for null checks
if (user is { Email: not null } activeUser)
{
    SendWelcomeEmail(activeUser.Email);
}
```

### Required Properties

```csharp
// [GOOD] Force initialization with required
public class UserConfig
{
    public required string ConnectionString { get; init; }
    public required string ApiKey { get; init; }
    public int TimeoutSeconds { get; init; } = 30;
}

// Usage - compiler enforces required properties
var config = new UserConfig
{
    ConnectionString = "Server=...",
    ApiKey = "key-123",
};
```

---

## Documentation

### XML Doc Comments

**Required for:**
- All public classes, interfaces, records, and enums
- All public and protected methods and properties
- Complex private methods

**Format:**
```csharp
/// <summary>
/// Creates a new user in the system.
/// </summary>
/// <remarks>
/// Validates the email format, checks for duplicates,
/// and assigns default permissions based on the role.
/// </remarks>
/// <param name="request">The user creation request containing email, name, and role.</param>
/// <returns>The newly created user with a generated ID.</returns>
/// <exception cref="ValidationException">Thrown when the email format is invalid.</exception>
/// <exception cref="DuplicateEntityException">Thrown when the email already exists.</exception>
/// <example>
/// <code>
/// var request = new CreateUserRequest("john@example.com", "John Doe", UserRole.User);
/// var user = await userService.CreateUserAsync(request);
/// </code>
/// </example>
public async Task<User> CreateUserAsync(CreateUserRequest request)
{
    // ...
}
```

**Class documentation:**
```csharp
/// <summary>
/// Repository for User entity database operations.
/// </summary>
/// <remarks>
/// <para>
/// Provides CRUD operations and query methods for User entities.
/// Uses Entity Framework Core with connection pooling for performance.
/// </para>
/// <para>
/// Thread-safe: this class can be used concurrently from multiple threads
/// when registered as a scoped service in the DI container.
/// </para>
/// </remarks>
public class UserRepository : IUserRepository
{
    // ...
}
```

**Interface documentation:**
```csharp
/// <summary>
/// Defines the contract for user persistence operations.
/// </summary>
/// <remarks>
/// Implementations should handle database-specific concerns
/// while keeping the interface technology-agnostic.
/// </remarks>
public interface IUserRepository
{
    /// <summary>
    /// Finds a user by their unique identifier.
    /// </summary>
    /// <param name="id">The unique user identifier.</param>
    /// <param name="cancellationToken">Token to cancel the operation.</param>
    /// <returns>The user if found; otherwise, <c>null</c>.</returns>
    Task<User?> FindByIdAsync(int id, CancellationToken cancellationToken = default);
}
```

---

## Architecture

### Clean Architecture Principles

**Layer Separation:**
```
┌─────────────────────────────────────┐
│   Presentation (API/Controllers)    │
├─────────────────────────────────────┤
│   Application (Use Cases/Services)  │
├─────────────────────────────────────┤
│   Domain (Entities/Business Logic)  │
├─────────────────────────────────────┤
│   Infrastructure (DB/External APIs) │
└─────────────────────────────────────┘
```

**Dependency Rule:** Inner layers never depend on outer layers.

**Solution Structure:**
```
src/
├── MyApp.Domain/                    # Business entities and logic
│   ├── Entities/
│   │   └── User.cs
│   ├── ValueObjects/
│   │   └── Email.cs
│   ├── Exceptions/
│   │   └── DomainException.cs
│   └── Interfaces/
│       └── IUserRepository.cs
├── MyApp.Application/               # Use cases and services
│   ├── Services/
│   │   └── UserService.cs
│   ├── DTOs/
│   │   ├── CreateUserRequest.cs
│   │   └── UserResponse.cs
│   ├── Validators/
│   │   └── CreateUserValidator.cs
│   ├── Interfaces/
│   │   └── IUserService.cs
│   └── Mapping/
│       └── UserMappingProfile.cs
├── MyApp.Infrastructure/            # External concerns
│   ├── Persistence/
│   │   ├── AppDbContext.cs
│   │   ├── Repositories/
│   │   │   └── UserRepository.cs
│   │   └── Configurations/
│   │       └── UserConfiguration.cs
│   ├── Services/
│   │   └── EmailService.cs
│   └── DependencyInjection.cs
├── MyApp.Api/                       # Presentation layer
│   ├── Controllers/
│   │   └── UsersController.cs
│   ├── Middleware/
│   │   └── ExceptionMiddleware.cs
│   ├── Program.cs
│   └── appsettings.json
└── tests/
    ├── MyApp.UnitTests/
    ├── MyApp.IntegrationTests/
    └── MyApp.ArchitectureTests/
```

### Dependency Injection

**Use the built-in DI container:**
```csharp
// Infrastructure/DependencyInjection.cs
public static class DependencyInjection
{
    public static IServiceCollection AddInfrastructure(
        this IServiceCollection services,
        IConfiguration configuration)
    {
        services.AddDbContext<AppDbContext>(options =>
            options.UseNpgsql(configuration.GetConnectionString("Default")));

        services.AddScoped<IUserRepository, UserRepository>();
        services.AddScoped<IUnitOfWork, UnitOfWork>();
        services.AddTransient<IEmailService, EmailService>();

        return services;
    }
}

// Program.cs
var builder = WebApplication.CreateBuilder(args);
builder.Services.AddInfrastructure(builder.Configuration);
builder.Services.AddApplication();
```

**Constructor injection (prefer primary constructors in .NET 8+):**
```csharp
// [GOOD] Primary constructor (C# 12+)
public class UserService(
    IUserRepository userRepository,
    IValidator<CreateUserRequest> validator,
    ILogger<UserService> logger) : IUserService
{
    public async Task<User> CreateUserAsync(CreateUserRequest request)
    {
        await validator.ValidateAndThrowAsync(request);
        // use userRepository, logger directly
    }
}

// [GOOD] Traditional constructor injection
public class UserService : IUserService
{
    private readonly IUserRepository _userRepository;
    private readonly ILogger<UserService> _logger;

    public UserService(
        IUserRepository userRepository,
        ILogger<UserService> logger)
    {
        _userRepository = userRepository;
        _logger = logger;
    }
}
```

### Minimal API vs Controllers

```csharp
// Minimal API (simple endpoints)
app.MapGet("/api/users/{id}", async (int id, IUserService userService) =>
{
    var user = await userService.GetByIdAsync(id);
    return user is not null ? Results.Ok(user) : Results.NotFound();
})
.WithName("GetUser")
.WithOpenApi()
.RequireAuthorization();

// Controller (complex endpoints with many actions)
[ApiController]
[Route("api/[controller]")]
[Authorize]
public class UsersController(IUserService userService) : ControllerBase
{
    [HttpGet("{id}")]
    [ProducesResponseType<UserResponse>(StatusCodes.Status200OK)]
    [ProducesResponseType(StatusCodes.Status404NotFound)]
    public async Task<IActionResult> GetById(int id)
    {
        var user = await userService.GetByIdAsync(id);
        return user is not null ? Ok(user) : NotFound();
    }

    [HttpPost]
    [ProducesResponseType<UserResponse>(StatusCodes.Status201Created)]
    [ProducesResponseType<ProblemDetails>(StatusCodes.Status400BadRequest)]
    public async Task<IActionResult> Create(CreateUserRequest request)
    {
        var user = await userService.CreateUserAsync(request);
        return CreatedAtAction(nameof(GetById), new { id = user.Id }, user);
    }
}
```

---

## Error Handling

### Exception Hierarchy

**Create domain-specific exceptions:**
```csharp
public abstract class AppException : Exception
{
    public string ErrorCode { get; }

    protected AppException(string message, string errorCode)
        : base(message)
    {
        ErrorCode = errorCode;
    }

    protected AppException(string message, string errorCode, Exception innerException)
        : base(message, innerException)
    {
        ErrorCode = errorCode;
    }
}

public class ValidationException : AppException
{
    public IReadOnlyDictionary<string, string[]> Errors { get; }

    public ValidationException(IReadOnlyDictionary<string, string[]> errors)
        : base("One or more validation errors occurred.", "VALIDATION_ERROR")
    {
        Errors = errors;
    }
}

public class NotFoundException : AppException
{
    public NotFoundException(string entity, object id)
        : base($"{entity} not found with id: {id}", "NOT_FOUND") { }
}

public class ConflictException : AppException
{
    public ConflictException(string message)
        : base(message, "CONFLICT") { }
}
```

### Global Exception Middleware

```csharp
public class ExceptionMiddleware(
    RequestDelegate next,
    ILogger<ExceptionMiddleware> logger)
{
    public async Task InvokeAsync(HttpContext context)
    {
        try
        {
            await next(context);
        }
        catch (Exception ex)
        {
            await HandleExceptionAsync(context, ex);
        }
    }

    private async Task HandleExceptionAsync(HttpContext context, Exception exception)
    {
        var (statusCode, problemDetails) = exception switch
        {
            ValidationException ve => (StatusCodes.Status400BadRequest, new ProblemDetails
            {
                Title = "Validation Error",
                Detail = ve.Message,
                Status = StatusCodes.Status400BadRequest,
                Extensions = { ["errors"] = ve.Errors },
            }),
            NotFoundException nf => (StatusCodes.Status404NotFound, new ProblemDetails
            {
                Title = "Not Found",
                Detail = nf.Message,
                Status = StatusCodes.Status404NotFound,
            }),
            ConflictException ce => (StatusCodes.Status409Conflict, new ProblemDetails
            {
                Title = "Conflict",
                Detail = ce.Message,
                Status = StatusCodes.Status409Conflict,
            }),
            _ => (StatusCodes.Status500InternalServerError, new ProblemDetails
            {
                Title = "Internal Server Error",
                Detail = "An unexpected error occurred.",
                Status = StatusCodes.Status500InternalServerError,
            }),
        };

        if (statusCode >= 500)
        {
            logger.LogError(exception, "Unhandled exception: {Message}", exception.Message);
        }
        else
        {
            logger.LogWarning("Handled exception: {Type} - {Message}",
                exception.GetType().Name, exception.Message);
        }

        context.Response.StatusCode = statusCode;
        context.Response.ContentType = "application/problem+json";
        await context.Response.WriteAsJsonAsync(problemDetails);
    }
}

// Register in Program.cs
app.UseMiddleware<ExceptionMiddleware>();
```

### Result Pattern (for expected failures)

```csharp
// [GOOD] Result type for operations that can fail predictably
public class Result<T>
{
    public T? Value { get; }
    public string? Error { get; }
    public bool IsSuccess => Error is null;

    private Result(T value) { Value = value; }
    private Result(string error) { Error = error; }

    public static Result<T> Success(T value) => new(value);
    public static Result<T> Failure(string error) => new(error);
}

// Usage
public async Task<Result<User>> CreateUserAsync(CreateUserRequest request)
{
    if (await _userRepository.ExistsByEmailAsync(request.Email))
    {
        return Result<User>.Failure($"Email {request.Email} is already taken.");
    }

    var user = new User(request.Email, request.Name, request.Role);
    await _userRepository.AddAsync(user);
    return Result<User>.Success(user);
}
```

### Error Handling Rules

**Never:**
```csharp
// [BAD] Empty catch block
try { riskyOperation(); }
catch { }

// [BAD] Catching Exception without context
try { operation(); }
catch (Exception) { /* swallowed */ }

// [BAD] throw ex (resets stack trace)
try { operation(); }
catch (Exception ex) { throw ex; }
```

**Always:**
```csharp
// [GOOD] Specific exceptions, preserve stack trace
try
{
    await ProcessOrderAsync(order);
}
catch (NotFoundException)
{
    return NotFound();
}
catch (ValidationException ex)
{
    return BadRequest(ex.Errors);
}

// [GOOD] using for IDisposable
await using var connection = new SqlConnection(connectionString);

// [GOOD] Rethrow with throw (not throw ex)
catch (Exception ex)
{
    logger.LogError(ex, "Failed to process order {OrderId}", order.Id);
    throw; // Preserves original stack trace
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
- All public APIs: 100% coverage

### Test Structure

```csharp
using FluentAssertions;
using Moq;
using Xunit;

public class UserServiceTests
{
    private readonly Mock<IUserRepository> _userRepositoryMock;
    private readonly Mock<IValidator<CreateUserRequest>> _validatorMock;
    private readonly Mock<ILogger<UserService>> _loggerMock;
    private readonly UserService _sut; // System Under Test

    public UserServiceTests()
    {
        _userRepositoryMock = new Mock<IUserRepository>();
        _validatorMock = new Mock<IValidator<CreateUserRequest>>();
        _loggerMock = new Mock<ILogger<UserService>>();

        _sut = new UserService(
            _userRepositoryMock.Object,
            _validatorMock.Object,
            _loggerMock.Object);
    }

    [Fact]
    public async Task CreateUserAsync_WithValidData_ShouldReturnCreatedUser()
    {
        // Arrange
        var request = new CreateUserRequest("test@example.com", "Test User", UserRole.User);
        _userRepositoryMock
            .Setup(r => r.ExistsByEmailAsync(request.Email))
            .ReturnsAsync(false);
        _userRepositoryMock
            .Setup(r => r.AddAsync(It.IsAny<User>()))
            .Callback<User>(u => u.Id = 1);

        // Act
        var result = await _sut.CreateUserAsync(request);

        // Assert
        result.Should().NotBeNull();
        result.Email.Should().Be("test@example.com");
        result.Name.Should().Be("Test User");
        result.Id.Should().Be(1);
        _userRepositoryMock.Verify(r => r.AddAsync(It.IsAny<User>()), Times.Once);
    }

    [Fact]
    public async Task CreateUserAsync_WithDuplicateEmail_ShouldThrowConflictException()
    {
        // Arrange
        var request = new CreateUserRequest("taken@example.com", "User", UserRole.User);
        _userRepositoryMock
            .Setup(r => r.ExistsByEmailAsync(request.Email))
            .ReturnsAsync(true);

        // Act
        var act = () => _sut.CreateUserAsync(request);

        // Assert
        await act.Should()
            .ThrowAsync<ConflictException>()
            .WithMessage("*taken@example.com*");
    }

    [Theory]
    [InlineData("", "Name", "Email")]
    [InlineData("test@example.com", "", "Name")]
    [InlineData("invalid-email", "Name", "email")]
    public async Task CreateUserAsync_WithInvalidData_ShouldThrowValidationException(
        string email, string name, string expectedField)
    {
        // Arrange
        var request = new CreateUserRequest(email, name, UserRole.User);
        _validatorMock
            .Setup(v => v.ValidateAndThrowAsync(request, default))
            .ThrowsAsync(new FluentValidation.ValidationException("Validation failed"));

        // Act
        var act = () => _sut.CreateUserAsync(request);

        // Assert
        await act.Should().ThrowAsync<FluentValidation.ValidationException>();
    }
}
```

### Integration Tests

```csharp
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.Extensions.DependencyInjection;
using Testcontainers.PostgreSql;

public class UsersApiTests : IClassFixture<WebApplicationFactory<Program>>, IAsyncLifetime
{
    private readonly WebApplicationFactory<Program> _factory;
    private readonly PostgreSqlContainer _postgres = new PostgreSqlBuilder()
        .WithImage("postgres:16")
        .Build();

    public UsersApiTests(WebApplicationFactory<Program> factory)
    {
        _factory = factory.WithWebHostBuilder(builder =>
        {
            builder.ConfigureServices(services =>
            {
                services.RemoveAll<DbContextOptions<AppDbContext>>();
                services.AddDbContext<AppDbContext>(options =>
                    options.UseNpgsql(_postgres.GetConnectionString()));
            });
        });
    }

    public async Task InitializeAsync() => await _postgres.StartAsync();
    public async Task DisposeAsync() => await _postgres.DisposeAsync();

    [Fact]
    public async Task CreateUser_WithValidData_ShouldReturn201()
    {
        // Arrange
        var client = _factory.CreateClient();
        var request = new CreateUserRequest("test@example.com", "Test User", UserRole.User);

        // Act
        var response = await client.PostAsJsonAsync("/api/users", request);

        // Assert
        response.StatusCode.Should().Be(HttpStatusCode.Created);
        var user = await response.Content.ReadFromJsonAsync<UserResponse>();
        user.Should().NotBeNull();
        user!.Email.Should().Be("test@example.com");
    }
}
```

---

## Security

### OWASP Top 10 Compliance

**Input Validation (FluentValidation):**
```csharp
public class CreateUserValidator : AbstractValidator<CreateUserRequest>
{
    public CreateUserValidator()
    {
        RuleFor(x => x.Email)
            .NotEmpty().WithMessage("Email is required")
            .EmailAddress().WithMessage("Invalid email format")
            .MaximumLength(255);

        RuleFor(x => x.Name)
            .NotEmpty().WithMessage("Name is required")
            .MinimumLength(2)
            .MaximumLength(100)
            .Matches(@"^[a-zA-Z\s'-]+$").WithMessage("Name contains invalid characters");

        RuleFor(x => x.Role)
            .IsInEnum().WithMessage("Invalid role");
    }
}
```

**SQL Injection Prevention (always use parameterized queries):**
```csharp
// [GOOD] Entity Framework Core (parameterized by default)
var user = await context.Users
    .Where(u => u.Email == email)
    .FirstOrDefaultAsync();

// [GOOD] Dapper with parameters
var user = await connection.QuerySingleOrDefaultAsync<User>(
    "SELECT * FROM users WHERE email = @Email",
    new { Email = email });

// [BAD] Never string concatenation
var query = $"SELECT * FROM users WHERE email = '{email}'"; // SQL INJECTION!
```

**Authentication and Authorization:**
```csharp
// Program.cs
builder.Services.AddAuthentication(JwtBearerDefaults.AuthenticationScheme)
    .AddJwtBearer(options =>
    {
        options.TokenValidationParameters = new TokenValidationParameters
        {
            ValidateIssuer = true,
            ValidateAudience = true,
            ValidateLifetime = true,
            ValidateIssuerSigningKey = true,
            ValidIssuer = builder.Configuration["Jwt:Issuer"],
            ValidAudience = builder.Configuration["Jwt:Audience"],
            IssuerSigningKey = new SymmetricSecurityKey(
                Encoding.UTF8.GetBytes(builder.Configuration["Jwt:Key"]!)),
        };
    });

builder.Services.AddAuthorization(options =>
{
    options.AddPolicy("AdminOnly", policy => policy.RequireRole("Admin"));
});
```

**Secrets Management:**
```csharp
// [GOOD] Use User Secrets for development
// dotnet user-secrets set "Jwt:Key" "my-secret-key"

// [GOOD] Use Azure Key Vault for production
builder.Configuration.AddAzureKeyVault(
    new Uri(builder.Configuration["KeyVault:Url"]!),
    new DefaultAzureCredential());

// [GOOD] Use environment variables
var connectionString = builder.Configuration.GetConnectionString("Default");

// [BAD] Never hardcode secrets
var apiKey = "sk-1234567890abcdef"; // NEVER DO THIS
```

**Security Checklist:**
- [ ] No secrets in code or version control
- [ ] All inputs validated (FluentValidation or DataAnnotations)
- [ ] Parameterized queries for all SQL (EF Core/Dapper)
- [ ] HTTPS enforced (`app.UseHttpsRedirection()`)
- [ ] Authentication and authorization configured
- [ ] CORS properly configured (not `AllowAll` in production)
- [ ] Anti-forgery tokens for form submissions
- [ ] Rate limiting enabled
- [ ] Security headers (HSTS, CSP, X-Frame-Options)
- [ ] Dependencies scanned for vulnerabilities
- [ ] Nullable reference types enabled

---

## Performance

### Optimization Rules

1. **Measure first:** Use BenchmarkDotNet for micro-benchmarks, dotTrace/PerfView for profiling
2. **Optimize algorithms:** O(n^2) to O(n log n) before micro-optimizations
3. **Use async/await:** For all I/O-bound operations
4. **Cache expensive operations:** Database queries, API calls, computations

### Async/Await (mandatory for I/O)

```csharp
// [GOOD] Async all the way down
public async Task<User> GetUserAsync(int id, CancellationToken ct = default)
{
    return await _context.Users
        .AsNoTracking()
        .FirstOrDefaultAsync(u => u.Id == id, ct)
        ?? throw new NotFoundException("User", id);
}

// [BAD] Blocking on async code (causes deadlocks)
public User GetUser(int id)
{
    return GetUserAsync(id).Result; // DEADLOCK RISK
}

// [GOOD] ConfigureAwait(false) in library code
public async Task<string> FetchDataAsync()
{
    var response = await _httpClient.GetAsync(url).ConfigureAwait(false);
    return await response.Content.ReadAsStringAsync().ConfigureAwait(false);
}
```

### High-Performance Patterns

```csharp
// [GOOD] Span<T> for hot paths (zero allocation)
public static int CountOccurrences(ReadOnlySpan<char> text, char target)
{
    int count = 0;
    foreach (var ch in text)
    {
        if (ch == target) count++;
    }
    return count;
}

// [GOOD] StringBuilder for string concatenation
var sb = new StringBuilder();
foreach (var item in items)
{
    sb.Append(item).Append(", ");
}

// [GOOD] String.Join or LINQ
var result = string.Join(", ", items);
```

### Caching

```csharp
// In-memory caching
public class UserService(
    IUserRepository userRepository,
    IMemoryCache cache,
    ILogger<UserService> logger)
{
    public async Task<User> GetByIdAsync(int id)
    {
        var cacheKey = $"user:{id}";
        if (cache.TryGetValue(cacheKey, out User? cached))
        {
            return cached!;
        }

        var user = await userRepository.FindByIdAsync(id)
            ?? throw new NotFoundException("User", id);

        cache.Set(cacheKey, user, TimeSpan.FromMinutes(10));
        return user;
    }
}

// Distributed caching (Redis)
builder.Services.AddStackExchangeRedisCache(options =>
{
    options.Configuration = builder.Configuration.GetConnectionString("Redis");
});
```

### EF Core Query Optimization

```csharp
// [GOOD] AsNoTracking for read-only queries
var users = await context.Users
    .AsNoTracking()
    .Where(u => u.IsActive)
    .ToListAsync();

// [GOOD] Select only needed columns
var names = await context.Users
    .Where(u => u.IsActive)
    .Select(u => new { u.Id, u.Name })
    .ToListAsync();

// [BAD] N+1 queries
var orders = await context.Orders.ToListAsync();
foreach (var order in orders)
{
    var customer = await context.Customers.FindAsync(order.CustomerId); // N+1!
}

// [GOOD] Include related data
var orders = await context.Orders
    .Include(o => o.Customer)
    .ToListAsync();
```

---

## Dependencies

### Dependency Management

**Principles:**
- Use Central Package Management for consistent versions
- Pin exact versions in production
- Regular security updates
- Minimize dependency count

**Central Package Management (Directory.Packages.props):**
```xml
<Project>
  <PropertyGroup>
    <ManagePackageVersionsCentrally>true</ManagePackageVersionsCentrally>
  </PropertyGroup>
  <ItemGroup>
    <PackageVersion Include="Microsoft.EntityFrameworkCore" Version="8.0.6" />
    <PackageVersion Include="FluentValidation" Version="11.9.0" />
    <PackageVersion Include="Serilog.AspNetCore" Version="8.0.1" />
    <PackageVersion Include="xunit" Version="2.8.0" />
    <PackageVersion Include="Moq" Version="4.20.0" />
    <PackageVersion Include="FluentAssertions" Version="6.12.0" />
  </ItemGroup>
</Project>
```

**Security Scanning:**
```bash
# Check for known vulnerabilities
dotnet list package --vulnerable

# Audit transitive dependencies
dotnet list package --vulnerable --include-transitive
```

---

## Logging

### Structured Logging (Serilog)

**Configuration:**
```csharp
// Program.cs
builder.Host.UseSerilog((context, services, configuration) =>
    configuration
        .ReadFrom.Configuration(context.Configuration)
        .ReadFrom.Services(services)
        .Enrich.FromLogContext()
        .Enrich.WithMachineName()
        .Enrich.WithEnvironmentName()
        .WriteTo.Console(new JsonFormatter())
        .WriteTo.Seq(context.Configuration["Seq:Url"]!));
```

**Usage:**
```csharp
public class UserService(
    IUserRepository userRepository,
    ILogger<UserService> logger)
{
    public async Task<User> CreateUserAsync(CreateUserRequest request)
    {
        logger.LogInformation("Creating user with Email={Email}", request.Email);

        try
        {
            var user = await userRepository.AddAsync(MapToUser(request));
            logger.LogInformation(
                "User created successfully: UserId={UserId}, Email={Email}",
                user.Id, user.Email);
            return user;
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "Failed to create user: Email={Email}", request.Email);
            throw;
        }
    }
}
```

**Correlation IDs with Middleware:**
```csharp
public class CorrelationIdMiddleware(RequestDelegate next)
{
    public async Task InvokeAsync(HttpContext context)
    {
        var correlationId = context.Request.Headers["X-Correlation-Id"].FirstOrDefault()
            ?? Guid.NewGuid().ToString();

        context.Response.Headers.Append("X-Correlation-Id", correlationId);

        using (LogContext.PushProperty("CorrelationId", correlationId))
        {
            await next(context);
        }
    }
}
```

### Logging Best Practices

**Log Levels:**
- `Trace`: Very detailed diagnostic information
- `Debug`: Detailed diagnostic information
- `Information`: General informational messages
- `Warning`: Warning messages for recoverable issues
- `Error`: Error messages for failures
- `Critical`: Critical issues requiring immediate attention

**What to Log:**
- Request/response for external APIs (sanitized)
- Authentication attempts (success and failure)
- Database operations (with timing at Debug level)
- Business events (user created, order placed)
- Errors with full stack trace and context

**What NOT to Log:**
- Passwords or secrets
- Personal Identifiable Information (PII)
- Credit card numbers
- Session tokens or API keys
- Full request/response bodies in production

**Use structured logging placeholders (not string interpolation):**
```csharp
// [GOOD] Structured logging - properties are indexed
logger.LogInformation("User {UserId} created order {OrderId}", userId, orderId);

// [BAD] String interpolation - not structured, not indexed
logger.LogInformation($"User {userId} created order {orderId}");
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
- [ ] Follows Microsoft C# conventions
- [ ] Nullable reference types used correctly
- [ ] XML doc comments for public APIs
- [ ] No code duplication (DRY)
- [ ] Appropriate abstractions and SOLID adherence
- [ ] async/await used for all I/O operations

**Testing:**
- [ ] Tests included for new code
- [ ] Tests cover edge cases and error paths
- [ ] Coverage meets minimum (90%)
- [ ] Tests are readable and maintainable

**Security:**
- [ ] No secrets in code
- [ ] Input validation present
- [ ] No SQL injection vulnerabilities
- [ ] Dependencies up to date

**Performance:**
- [ ] No obvious performance issues (N+1 queries, blocking async)
- [ ] AsNoTracking for read-only EF Core queries
- [ ] Caching where appropriate
- [ ] CancellationToken passed through async chains

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
- `dotnet format` - Built-in code formatter
- `.editorconfig` - Style enforcement in IDE

**Linting & Static Analysis:**
- Roslyn analyzers (built-in)
- `Microsoft.CodeAnalysis.NetAnalyzers` - .NET analyzers
- SonarQube / SonarCloud (optional, for enterprise)

**Testing:**
- `xUnit` - Testing framework
- `Moq` or `NSubstitute` - Mocking library
- `FluentAssertions` - Fluent assertion syntax
- `Coverlet` - Code coverage
- `Testcontainers` - Integration test infrastructure

**Security:**
- `dotnet list package --vulnerable` - Vulnerability scanner

### Configuration

**.editorconfig:**
```ini
root = true

[*.cs]
indent_style = space
indent_size = 4
end_of_line = lf
charset = utf-8
trim_trailing_whitespace = true
insert_final_newline = true

# Naming rules
dotnet_naming_rule.private_fields_should_be_camel_case.severity = error
dotnet_naming_rule.private_fields_should_be_camel_case.symbols = private_fields
dotnet_naming_rule.private_fields_should_be_camel_case.style = camel_case_underscore

dotnet_naming_symbols.private_fields.applicable_kinds = field
dotnet_naming_symbols.private_fields.applicable_accessibilities = private
dotnet_naming_symbols.private_fields.required_modifiers =

dotnet_naming_style.camel_case_underscore.required_prefix = _
dotnet_naming_style.camel_case_underscore.capitalization = camel_case

# Code style
csharp_style_namespace_declarations = file_scoped:error
csharp_using_directive_placement = outside_namespace:error
csharp_style_var_for_built_in_types = false:suggestion
csharp_style_var_when_type_is_apparent = true:suggestion
csharp_prefer_braces = true:error
csharp_style_expression_bodied_methods = when_on_single_line:suggestion
dotnet_style_require_accessibility_modifiers = always:error
```

### CI/CD Pipeline

**Required checks before merge:**
```bash
# Restore dependencies
dotnet restore

# Format check
dotnet format --verify-no-changes

# Build
dotnet build --no-restore --warnaserrors

# Run tests with coverage
dotnet test --no-build --collect:"XPlat Code Coverage" \
  --results-directory ./coverage

# Check coverage threshold
dotnet reportgenerator \
  -reports:"./coverage/**/coverage.cobertura.xml" \
  -targetdir:"./coverage/report" \
  -reporttypes:TextSummary

# Vulnerability scan
dotnet list package --vulnerable --include-transitive

# Publish
dotnet publish -c Release --no-build
```

---

## Quick Reference

### Daily Workflow

1. **Before coding:**
   - Pull latest changes
   - Create feature branch
   - Write failing test (TDD)

2. **While coding:**
   - Follow nullable reference types
   - Add XML doc comments
   - Use async/await for I/O
   - Run tests frequently
   - Commit small, logical changes

3. **Before pushing:**
   - Run full test suite
   - Check coverage (90%+)
   - Run formatters and analyzers
   - Update documentation

4. **Code review:**
   - Address all comments
   - Ensure CI passes
   - Get 2 approvals
   - Squash and merge

### Common Commands

```bash
# Create new project
dotnet new webapi -n MyApp.Api

# Restore dependencies
dotnet restore

# Build
dotnet build

# Format code
dotnet format

# Run tests
dotnet test

# Run tests with coverage
dotnet test --collect:"XPlat Code Coverage"

# Run specific test
dotnet test --filter "FullyQualifiedName~UserServiceTests"

# Check vulnerabilities
dotnet list package --vulnerable

# Add package
dotnet add package FluentValidation

# EF Core migrations
dotnet ef migrations add InitialCreate
dotnet ef database update

# Publish for production
dotnet publish -c Release
```

---

## References

- [Microsoft C# Coding Conventions](https://learn.microsoft.com/en-us/dotnet/csharp/fundamentals/coding-style/coding-conventions)
- [.NET Runtime Coding Style](https://github.com/dotnet/runtime/blob/main/docs/coding-guidelines/coding-style.md)
- [C# Identifier Naming Rules](https://learn.microsoft.com/en-us/dotnet/csharp/fundamentals/coding-style/identifier-names)
- [OWASP .NET Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/DotNet_Security_Cheat_Sheet.html)
- [Clean Architecture (Robert C. Martin)](https://blog.cleancoder.com/uncle-bob/2012/08/13/the-clean-architecture.html)
- [ASP.NET Core Documentation](https://learn.microsoft.com/en-us/aspnet/core/)
- [Entity Framework Core Documentation](https://learn.microsoft.com/en-us/ef/core/)

---

**Questions or suggestions?** Update this document through team discussion and code review.

**Version History:**
- v1.0 (2026) - Initial enterprise-grade guidelines
