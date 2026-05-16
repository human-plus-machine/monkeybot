# Java Coding Guidelines - Enterprise Grade

**Version:** 1.0  
**Last Updated:** 2026  
**Target:** Production Java systems requiring high quality, security, and maintainability

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

Follow **SOLID principles** and Clean Code (Robert C. Martin):
- Single Responsibility: Every class has one reason to change
- Open/Closed: Open for extension, closed for modification
- Liskov Substitution: Subtypes must be substitutable for their base types
- Interface Segregation: Prefer small, focused interfaces
- Dependency Inversion: Depend on abstractions, not concretions

**Core Principles:**
- Write code for humans first, machines second
- Optimize for readability and maintainability over cleverness
- Test-driven development (TDD) is mandatory
- Security is not optional
- Performance matters, but measure before optimizing
- Favor composition over inheritance
- Make illegal states unrepresentable

---

## Code Style

### Base Standard

Follow the **Google Java Style Guide** with these specifications:

**Formatting:**
- Line length: 100 characters
- Indentation: 2 spaces (never tabs)
- Encoding: UTF-8
- Line endings: LF (Unix style)
- Braces: K&R style (opening brace on same line)

**Naming Conventions:**
```java
// Classes - UpperCamelCase (nouns or noun phrases)
public class UserAccount { }
public class ImmutableOrderList { }

// Interfaces - UpperCamelCase (nouns, noun phrases, or adjectives)
public interface Readable { }
public interface UserRepository { }

// Methods - lowerCamelCase (verbs or verb phrases)
public void sendMessage() { }
public boolean isValid() { }

// Variables - lowerCamelCase
int userCount = 0;
String firstName = "John";

// Constants - UPPER_SNAKE_CASE (static final, deeply immutable)
static final int MAX_RETRY_COUNT = 3;
static final String API_BASE_URL = "https://api.example.com";
static final ImmutableList<String> VALID_ROLES = ImmutableList.of("admin", "user");

// Packages - all lowercase, no underscores
package com.example.deepspace;

// Type parameters - single capital letter or descriptive with T prefix
<T>, <E>, <K, V>
<TRequest, TResponse>

// Test classes - ClassNameTest
public class UserAccountTest { }
```

**Import Ordering:**
```java
// 1. All non-static imports in a single group (ASCII sorted)
import com.example.domain.User;
import com.example.service.AuthService;
import java.util.List;
import java.util.Optional;

// 2. Blank line separator

// 3. All static imports in a single group (ASCII sorted)
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.mockito.Mockito.when;
```

**Avoid:**
- Wildcard imports: `import java.util.*`
- C-style array declarations: `String args[]` (use `String[] args`)
- Hungarian notation or prefixes: `mName`, `sInstance`, `kConstant`
- Single-letter variable names except in lambdas, loops, or generics

**Braces:**
```java
// [GOOD] Always use braces, even for single-line bodies
if (condition) {
  doSomething();
}

// [BAD] Never omit braces
if (condition)
  doSomething();

// [GOOD] Empty blocks may be concise
void doNothing() {}

// [BAD] Not for multi-block statements
try {
  doSomething();
} catch (Exception e) {} // Never
```

---

## Type Safety

### Generics

**Use generics to eliminate unchecked casts:**
```java
// [GOOD] Type-safe collection
List<User> users = new ArrayList<>();

// [BAD] Raw types
List users = new ArrayList();
```

**Bounded type parameters:**
```java
public <T extends Comparable<T>> T findMax(List<T> items) {
  return items.stream()
      .max(Comparable::compareTo)
      .orElseThrow();
}
```

### Null Safety

**Use `Optional` for return types that may be absent:**
```java
// [GOOD] Optional for nullable returns
public Optional<User> findById(long id) {
  return Optional.ofNullable(userMap.get(id));
}

// [BAD] Returning null
public User findById(long id) {
  return userMap.get(id); // Caller has no idea this can be null
}
```

**Use nullability annotations:**
```java
import org.jspecify.annotations.NonNull;
import org.jspecify.annotations.Nullable;

public @NonNull String formatUser(@NonNull User user, @Nullable String prefix) {
  String label = prefix != null ? prefix + " " : "";
  return label + user.getName();
}
```

**Never pass `null` as a method argument unless the API explicitly allows it.**

### Records and Sealed Classes

**Use records for immutable data carriers (Java 16+):**
```java
public record UserDto(
    long id,
    String email,
    String name,
    Instant createdAt
) {
  // Compact constructor for validation
  public UserDto {
    if (email == null || !email.contains("@")) {
      throw new IllegalArgumentException("Invalid email: " + email);
    }
  }
}
```

**Use sealed classes for restricted hierarchies (Java 17+):**
```java
public sealed interface Shape permits Circle, Rectangle, Triangle {
  double area();
}

public record Circle(double radius) implements Shape {
  public double area() { return Math.PI * radius * radius; }
}

public record Rectangle(double width, double height) implements Shape {
  public double area() { return width * height; }
}
```

---

## Documentation

### Javadoc Standards

**Required for:**
- All public classes, interfaces, enums, records
- All public and protected methods
- Complex private methods

**Format:**
```java
/**
 * Creates a new user in the system.
 *
 * <p>Validates the email format, checks for duplicates,
 * and assigns default permissions based on the role.
 *
 * @param email valid email address (RFC 5322 compliant)
 * @param name  user's full name (2-100 characters)
 * @param role  user role, one of: {@code user}, {@code admin}, {@code moderator}
 * @return newly created User instance with generated ID
 * @throws ValidationException if email format is invalid or name is empty
 * @throws DuplicateUserException if email already exists
 * @throws DatabaseException if database operation fails
 * @see User
 * @since 1.0
 */
public User createUser(String email, String name, String role) {
  // ...
}
```

**Class Javadoc:**
```java
/**
 * Repository for User entity database operations.
 *
 * <p>Provides CRUD operations and query methods for User entities.
 * Uses connection pooling and prepared statements for performance.
 *
 * <p>Thread-safe: this class is safe for concurrent use by multiple threads.
 *
 * @author Team Name
 * @since 1.0
 */
public class UserRepository {
  // ...
}
```

**Package documentation (package-info.java):**
```java
/**
 * User authentication and authorization module.
 *
 * <p>This package provides:
 * <ul>
 *   <li>JWT token generation and validation</li>
 *   <li>Password hashing with BCrypt</li>
 *   <li>Role-based access control (RBAC)</li>
 *   <li>Session management</li>
 * </ul>
 *
 * @see com.example.auth.AuthService
 */
package com.example.auth;
```

---

## Architecture

### Clean Architecture Principles

**Layer Separation:**
```
┌─────────────────────────────────────┐
│   Presentation (Controllers/API)    │
├─────────────────────────────────────┤
│   Application (Use Cases/Services)  │
├─────────────────────────────────────┤
│   Domain (Entities/Business Logic)  │
├─────────────────────────────────────┤
│   Infrastructure (DB/External APIs) │
└─────────────────────────────────────┘
```

**Dependency Rule:** Inner layers never depend on outer layers.

**Project Structure (Maven/Gradle):**
```
src/
├── main/
│   ├── java/com/example/myapp/
│   │   ├── domain/              # Business entities and logic
│   │   │   ├── model/
│   │   │   ├── exception/
│   │   │   └── repository/      # Repository interfaces
│   │   ├── application/         # Use cases and services
│   │   │   ├── service/
│   │   │   ├── dto/
│   │   │   └── port/            # Port interfaces
│   │   ├── infrastructure/      # External concerns
│   │   │   ├── persistence/     # JPA repositories
│   │   │   ├── client/          # HTTP/gRPC clients
│   │   │   └── config/          # Spring configuration
│   │   └── presentation/        # Controllers/handlers
│   │       ├── controller/
│   │       └── mapper/
│   └── resources/
│       ├── application.yml
│       └── db/migration/        # Flyway/Liquibase
└── test/
    ├── java/com/example/myapp/
    │   ├── unit/
    │   └── integration/
    └── resources/
```

### Design Patterns

**Dependency Injection (prefer constructor injection):**
```java
@Service
public class UserService {
  private final UserRepository userRepository;
  private final PasswordEncoder passwordEncoder;
  private final EventPublisher eventPublisher;

  // Constructor injection - all dependencies explicit and final
  public UserService(
      UserRepository userRepository,
      PasswordEncoder passwordEncoder,
      EventPublisher eventPublisher) {
    this.userRepository = userRepository;
    this.passwordEncoder = passwordEncoder;
    this.eventPublisher = eventPublisher;
  }
}
```

**Repository pattern:**
```java
// Domain layer - interface
public interface UserRepository {
  Optional<User> findById(Long id);
  User save(User user);
  void deleteById(Long id);
  List<User> findByRole(String role);
}

// Infrastructure layer - implementation
@Repository
public class JpaUserRepository implements UserRepository {
  private final JpaUserEntityRepository jpaRepository;
  private final UserMapper mapper;

  @Override
  public Optional<User> findById(Long id) {
    return jpaRepository.findById(id).map(mapper::toDomain);
  }
}
```

**DTO/Entity separation:**
```java
// Domain entity - rich model with business logic
public class User {
  private Long id;
  private Email email;
  private String name;
  private Role role;

  public void promote() {
    if (this.role == Role.ADMIN) {
      throw new IllegalStateException("User is already admin");
    }
    this.role = Role.ADMIN;
  }
}

// DTO - data transfer only
public record CreateUserRequest(
    @NotBlank String email,
    @NotBlank @Size(min = 2, max = 100) String name,
    @NotNull Role role
) {}

public record UserResponse(
    long id,
    String email,
    String name,
    String role,
    Instant createdAt
) {}
```

---

## Error Handling

### Exception Hierarchy

**Create domain-specific exceptions:**
```java
public abstract class AppException extends RuntimeException {
  private final String errorCode;

  protected AppException(String message, String errorCode) {
    super(message);
    this.errorCode = errorCode;
  }

  protected AppException(String message, String errorCode, Throwable cause) {
    super(message, cause);
    this.errorCode = errorCode;
  }

  public String getErrorCode() { return errorCode; }
}

public class ValidationException extends AppException {
  public ValidationException(String message) {
    super(message, "VALIDATION_ERROR");
  }
}

public class NotFoundException extends AppException {
  public NotFoundException(String entity, Object id) {
    super(String.format("%s not found with id: %s", entity, id), "NOT_FOUND");
  }
}

public class AuthenticationException extends AppException {
  public AuthenticationException(String message) {
    super(message, "AUTH_ERROR");
  }
}

public class DatabaseException extends AppException {
  public DatabaseException(String message, Throwable cause) {
    super(message, "DB_ERROR", cause);
  }
}
```

### Error Handling Rules

**Never:**
```java
// [BAD] Empty catch block - swallows errors silently
try {
  riskyOperation();
} catch (Exception e) {
  // nothing
}

// [BAD] Catching generic Exception without re-raising
try {
  operation();
} catch (Exception e) {
  log.error("Failed");
}

// [BAD] Using exceptions for control flow
try {
  return users.get(userId);
} catch (IndexOutOfBoundsException e) {
  return null;
}

// [BAD] Logging and re-throwing (causes duplicate log entries)
try {
  operation();
} catch (SomeException e) {
  log.error("Failed", e);
  throw e;
}
```

**Always:**
```java
// [GOOD] Catch specific exceptions
try {
  user = findUser(userId);
} catch (NotFoundException e) {
  return ResponseEntity.notFound().build();
} catch (DatabaseException e) {
  log.error("Database error fetching user {}: {}", userId, e.getMessage(), e);
  throw e;
}

// [GOOD] Try-with-resources for AutoCloseable
try (var connection = dataSource.getConnection();
     var statement = connection.prepareStatement(sql)) {
  statement.setString(1, email);
  return statement.executeQuery();
}

// [GOOD] Context-rich error messages
if (email == null || email.isBlank()) {
  throw new ValidationException(
      String.format("Email is required for user creation. Got: '%s'", email));
}

// [GOOD] Wrap lower-level exceptions with context
try {
  return objectMapper.readValue(json, User.class);
} catch (JsonProcessingException e) {
  throw new ValidationException("Invalid JSON for user: " + e.getMessage(), e);
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

```java
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;
import org.junit.jupiter.params.provider.NullAndEmptySource;
import org.mockito.InjectMocks;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;

@ExtendWith(MockitoExtension.class)
@DisplayName("UserService")
class UserServiceTest {

  @Mock
  private UserRepository userRepository;

  @Mock
  private PasswordEncoder passwordEncoder;

  @InjectMocks
  private UserService userService;

  @Nested
  @DisplayName("createUser")
  class CreateUser {

    @Test
    @DisplayName("should create user with valid data")
    void shouldCreateUserWithValidData() {
      // Arrange
      var request = new CreateUserRequest("test@example.com", "Test User", Role.USER);
      when(userRepository.existsByEmail("test@example.com")).thenReturn(false);
      when(passwordEncoder.encode(any())).thenReturn("hashed");
      when(userRepository.save(any())).thenAnswer(inv -> {
        User user = inv.getArgument(0);
        return user.withId(1L);
      });

      // Act
      User result = userService.createUser(request);

      // Assert
      assertThat(result.getId()).isEqualTo(1L);
      assertThat(result.getEmail()).isEqualTo("test@example.com");
      assertThat(result.getName()).isEqualTo("Test User");
      verify(userRepository).save(any(User.class));
    }

    @Test
    @DisplayName("should throw when email already exists")
    void shouldThrowWhenEmailAlreadyExists() {
      // Arrange
      var request = new CreateUserRequest("taken@example.com", "User", Role.USER);
      when(userRepository.existsByEmail("taken@example.com")).thenReturn(true);

      // Act & Assert
      assertThatThrownBy(() -> userService.createUser(request))
          .isInstanceOf(DuplicateUserException.class)
          .hasMessageContaining("taken@example.com");
    }

    @ParameterizedTest
    @NullAndEmptySource
    @DisplayName("should reject blank email")
    void shouldRejectBlankEmail(String email) {
      var request = new CreateUserRequest(email, "Test User", Role.USER);

      assertThatThrownBy(() -> userService.createUser(request))
          .isInstanceOf(ValidationException.class)
          .hasMessageContaining("Email");
    }

    @ParameterizedTest
    @CsvSource({
        "'', 'Name', Email is required",
        "'test@example.com', '', Name is required",
        "'invalid', 'Name', Invalid email format"
    })
    @DisplayName("should validate all input fields")
    void shouldValidateAllInputFields(String email, String name, String expectedError) {
      var request = new CreateUserRequest(email, name, Role.USER);

      assertThatThrownBy(() -> userService.createUser(request))
          .isInstanceOf(ValidationException.class)
          .hasMessageContaining(expectedError);
    }
  }
}
```

### Test Types

**Unit Tests:**
- Test single class/method in isolation
- Mock all external dependencies with Mockito
- Fast execution (< 10ms per test)
- Use AssertJ for fluent assertions

**Integration Tests:**
```java
@SpringBootTest
@Testcontainers
class UserRepositoryIntegrationTest {

  @Container
  static PostgreSQLContainer<?> postgres = new PostgreSQLContainer<>("postgres:16");

  @DynamicPropertySource
  static void configureProperties(DynamicPropertyRegistry registry) {
    registry.add("spring.datasource.url", postgres::getJdbcUrl);
    registry.add("spring.datasource.username", postgres::getUsername);
    registry.add("spring.datasource.password", postgres::getPassword);
  }

  @Autowired
  private UserRepository userRepository;

  @Test
  void shouldPersistAndRetrieveUser() {
    User saved = userRepository.save(new User("test@example.com", "Test User"));

    Optional<User> found = userRepository.findById(saved.getId());

    assertThat(found).isPresent();
    assertThat(found.get().getEmail()).isEqualTo("test@example.com");
  }
}
```

---

## Security

### OWASP Top 10 Compliance

**Input Validation:**
```java
import jakarta.validation.constraints.*;

public record CreateUserRequest(
    @NotBlank @Email
    String email,

    @NotBlank @Size(min = 2, max = 100)
    @Pattern(regexp = "^[a-zA-Z\\s'-]+$", message = "Name contains invalid characters")
    String name,

    @NotNull
    Role role
) {}

// Sanitize HTML to prevent XSS
import org.owasp.html.PolicyFactory;
import org.owasp.html.Sanitizers;

public String sanitizeInput(String input) {
  PolicyFactory policy = Sanitizers.FORMATTING.and(Sanitizers.LINKS);
  return policy.sanitize(input);
}
```

**SQL Injection Prevention:**
```java
// [GOOD] Parameterized queries
PreparedStatement stmt = connection.prepareStatement(
    "SELECT * FROM users WHERE email = ? AND status = ?");
stmt.setString(1, email);
stmt.setString(2, status);

// [GOOD] JPA named parameters
@Query("SELECT u FROM User u WHERE u.email = :email")
Optional<User> findByEmail(@Param("email") String email);

// [BAD] Never string concatenation
String query = "SELECT * FROM users WHERE email = '" + email + "'";
```

**Secrets Management:**
```java
// [GOOD] Use environment variables or secret manager
@Value("${app.api-key}")
private String apiKey;

// [GOOD] Use vault integration
@VaultPropertySource("secret/myapp")
@Configuration
public class VaultConfig { }

// [BAD] Never hardcode secrets
private static final String API_KEY = "sk-1234567890abcdef"; // NEVER
```

**Password Hashing:**
```java
import org.springframework.security.crypto.bcrypt.BCryptPasswordEncoder;

public class PasswordService {
  private final BCryptPasswordEncoder encoder = new BCryptPasswordEncoder(12);

  public String hashPassword(String rawPassword) {
    return encoder.encode(rawPassword);
  }

  public boolean verifyPassword(String rawPassword, String hashedPassword) {
    return encoder.matches(rawPassword, hashedPassword);
  }
}
```

**Security Checklist:**
- [ ] No secrets in code or version control
- [ ] All inputs validated and sanitized
- [ ] Parameterized queries for all SQL
- [ ] HTTPS only for external communication
- [ ] Authentication on all protected endpoints
- [ ] Rate limiting on public APIs
- [ ] Security headers configured (CSP, HSTS, X-Frame-Options)
- [ ] Dependencies scanned for vulnerabilities
- [ ] JWT tokens validated with proper algorithms
- [ ] Sensitive data encrypted at rest and in transit

---

## Performance

### Optimization Rules

1. **Measure first:** Use JMH or async-profiler before optimizing
2. **Optimize algorithms:** O(n^2) to O(n log n) before micro-optimizations
3. **Cache expensive operations:** Database queries, API calls, computations
4. **Use appropriate data structures:** HashMap for lookups, HashSet for membership

### Performance Patterns

**StringBuilder for string concatenation:**
```java
// [BAD] String concatenation in loops
String result = "";
for (String item : items) {
  result += item + ", ";
}

// [GOOD] StringBuilder
StringBuilder sb = new StringBuilder();
for (String item : items) {
  sb.append(item).append(", ");
}

// [GOOD] String.join or Collectors.joining
String result = String.join(", ", items);
String result = items.stream().collect(Collectors.joining(", "));
```

**Streams vs loops (choose wisely):**
```java
// [GOOD] Streams for readability in transformations
List<String> emails = users.stream()
    .filter(User::isActive)
    .map(User::getEmail)
    .sorted()
    .toList();

// [GOOD] Traditional loop for performance-critical hot paths
List<String> emails = new ArrayList<>(users.size());
for (User user : users) {
  if (user.isActive()) {
    emails.add(user.getEmail());
  }
}
```

**Batch operations:**
```java
// [BAD] N+1 queries
for (Long userId : userIds) {
  User user = userRepository.findById(userId).orElseThrow();
  process(user);
}

// [GOOD] Single batch query
List<User> users = userRepository.findAllById(userIds);
users.forEach(this::process);
```

**Caching:**
```java
import com.github.benmanes.caffeine.cache.Caffeine;
import com.github.benmanes.caffeine.cache.Cache;

@Service
public class UserService {
  private final Cache<Long, User> userCache = Caffeine.newBuilder()
      .maximumSize(10_000)
      .expireAfterWrite(Duration.ofMinutes(10))
      .build();

  public User getUser(Long id) {
    return userCache.get(id, key -> userRepository.findById(key).orElseThrow());
  }
}
```

**Connection pooling:**
```yaml
# application.yml - HikariCP settings
spring:
  datasource:
    hikari:
      maximum-pool-size: 20
      minimum-idle: 5
      idle-timeout: 300000
      connection-timeout: 20000
      max-lifetime: 1200000
```

---

## Dependencies

### Dependency Management

**Principles:**
- Pin exact versions in production (or use BOMs)
- Use lock files (Gradle: `--write-locks`, Maven: `versions-lock`)
- Regular security updates
- Minimize dependency count

**Maven BOM for version alignment:**
```xml
<!-- pom.xml -->
<dependencyManagement>
  <dependencies>
    <dependency>
      <groupId>org.springframework.boot</groupId>
      <artifactId>spring-boot-dependencies</artifactId>
      <version>3.3.0</version>
      <type>pom</type>
      <scope>import</scope>
    </dependency>
  </dependencies>
</dependencyManagement>

<dependencies>
  <dependency>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter-web</artifactId>
  </dependency>
  <dependency>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter-test</artifactId>
    <scope>test</scope>
  </dependency>
</dependencies>
```

**Gradle:**
```kotlin
// build.gradle.kts
plugins {
  id("org.springframework.boot") version "3.3.0"
  id("io.spring.dependency-management") version "1.1.5"
}

dependencies {
  implementation("org.springframework.boot:spring-boot-starter-web")
  testImplementation("org.springframework.boot:spring-boot-starter-test")
}
```

**Security Scanning:**
- Run vulnerability scanners regularly (OWASP dependency-check)
- Update dependencies monthly
- Review dependency licenses
- Audit transitive dependencies

---

## Logging

### Structured Logging

**Use SLF4J with Logback (JSON for production):**
```java
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.slf4j.MDC;

public class UserService {
  private static final Logger log = LoggerFactory.getLogger(UserService.class);

  public User createUser(CreateUserRequest request) {
    log.info("Creating user with email={}", request.email());

    try {
      User user = userRepository.save(mapToUser(request));
      log.info("User created successfully: userId={}, email={}", user.getId(), user.getEmail());
      return user;
    } catch (Exception e) {
      log.error("Failed to create user: email={}", request.email(), e);
      throw e;
    }
  }
}
```

**MDC for correlation IDs:**
```java
import org.slf4j.MDC;
import jakarta.servlet.*;
import java.util.UUID;

public class CorrelationIdFilter implements Filter {

  @Override
  public void doFilter(ServletRequest request, ServletResponse response, FilterChain chain)
      throws IOException, ServletException {
    String correlationId = UUID.randomUUID().toString();
    MDC.put("correlationId", correlationId);
    try {
      chain.doFilter(request, response);
    } finally {
      MDC.remove("correlationId");
    }
  }
}
```

**Logback configuration (JSON):**
```xml
<!-- logback-spring.xml -->
<configuration>
  <appender name="JSON" class="ch.qos.logback.core.ConsoleAppender">
    <encoder class="net.logstash.logback.encoder.LogstashEncoder">
      <includeMdcKeyName>correlationId</includeMdcKeyName>
    </encoder>
  </appender>

  <root level="INFO">
    <appender-ref ref="JSON" />
  </root>
</configuration>
```

### Logging Best Practices

**Log Levels:**
- `TRACE`: Very detailed diagnostic information
- `DEBUG`: Detailed diagnostic information
- `INFO`: General informational messages
- `WARN`: Warning messages for recoverable issues
- `ERROR`: Error messages for failures

**What to Log:**
- Request/response for external APIs (sanitized)
- Authentication attempts (success and failure)
- Database operations (with timing at DEBUG level)
- Business events (user created, order placed)
- Errors with full stack trace and context

**What NOT to Log:**
- Passwords or secrets
- Personal Identifiable Information (PII)
- Credit card numbers
- Session tokens or API keys
- Full request/response bodies in production

---

## Code Review

### Review Checklist

**Functionality:**
- [ ] Code solves the stated problem
- [ ] Edge cases handled
- [ ] Error handling appropriate
- [ ] No obvious bugs

**Quality:**
- [ ] Follows Google Java Style Guide
- [ ] Type safety enforced (no raw types, no unnecessary casts)
- [ ] Javadoc for public APIs
- [ ] No code duplication (DRY)
- [ ] Appropriate abstractions and SOLID adherence

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
- [ ] No obvious performance issues (N+1 queries, unbounded collections)
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
- `google-java-format` - Enforces Google Java Style
- IDE plugin for automatic formatting on save

**Linting & Static Analysis:**
- `Checkstyle` - Style and convention enforcement
- `SpotBugs` - Bug pattern detection
- `Error Prone` - Compile-time bug catching (Google)

**Testing:**
- `JUnit 5` - Testing framework
- `Mockito` - Mocking library
- `AssertJ` - Fluent assertions
- `JaCoCo` - Code coverage
- `Testcontainers` - Integration test infrastructure

**Security:**
- `OWASP Dependency-Check` - Vulnerability scanner
- `SpotBugs Find Security Bugs` - Security-focused static analysis

**Build Configuration (Gradle):**
```kotlin
// build.gradle.kts
plugins {
  java
  id("com.diffplug.spotless") version "6.25.0"
  id("com.github.spotbugs") version "6.0.0"
  jacoco
}

java {
  toolchain {
    languageVersion = JavaLanguageVersion.of(21)
  }
}

spotless {
  java {
    googleJavaFormat("1.22.0")
  }
}

spotbugs {
  effort = com.github.spotbugs.snom.Effort.MAX
  reportLevel = com.github.spotbugs.snom.Confidence.MEDIUM
}

tasks.jacocoTestReport {
  reports {
    xml.required = true
    html.required = true
  }
}

tasks.jacocoTestCoverageVerification {
  violationRules {
    rule {
      limit {
        minimum = "0.90".toBigDecimal()
      }
    }
  }
}

tasks.test {
  useJUnitPlatform()
  finalizedBy(tasks.jacocoTestReport)
}
```

### CI/CD Pipeline

**Required checks before merge:**
```bash
# Format check
./gradlew spotlessCheck

# Compile
./gradlew compileJava

# Static analysis
./gradlew spotbugsMain checkstyleMain

# Tests with coverage
./gradlew test jacocoTestCoverageVerification

# Dependency vulnerability scan
./gradlew dependencyCheckAnalyze

# Build artifact
./gradlew build
```

---

## Quick Reference

### Daily Workflow

1. **Before coding:**
   - Pull latest changes
   - Create feature branch
   - Write failing test (TDD)

2. **While coding:**
   - Follow type safety (generics, Optional, annotations)
   - Add Javadoc
   - Run tests frequently
   - Commit small, logical changes

3. **Before pushing:**
   - Run full test suite
   - Check coverage (90%+)
   - Run linters and static analysis
   - Update documentation

4. **Code review:**
   - Address all comments
   - Ensure CI passes
   - Get 2 approvals
   - Squash and merge

### Common Commands

```bash
# Format code
./gradlew spotlessApply

# Run linters
./gradlew spotbugsMain checkstyleMain

# Run tests
./gradlew test

# Run tests with coverage report
./gradlew test jacocoTestReport

# Run specific test class
./gradlew test --tests "com.example.UserServiceTest"

# Security scan
./gradlew dependencyCheckAnalyze

# Full build
./gradlew build

# Clean build
./gradlew clean build
```

---

## References

- [Google Java Style Guide](https://google.github.io/styleguide/javaguide.html)
- [Oracle Secure Coding Guidelines for Java SE](https://www.oracle.com/java/technologies/javase/seccodeguide.html)
- [Effective Java (Joshua Bloch)](https://www.oreilly.com/library/view/effective-java-3rd/9780134686097/)
- [Clean Architecture (Robert C. Martin)](https://blog.cleancoder.com/uncle-bob/2012/08/13/the-clean-architecture.html)
- [OWASP Java Security](https://cheatsheetseries.owasp.org/cheatsheets/Java_Security_Cheat_Sheet.html)
- [JUnit 5 User Guide](https://junit.org/junit5/docs/current/user-guide/)
- [Spring Boot Reference](https://docs.spring.io/spring-boot/docs/current/reference/html/)

---

**Questions or suggestions?** Update this document through team discussion and code review.

**Version History:**
- v1.0 (2026) - Initial enterprise-grade guidelines
