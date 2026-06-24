---
name: design-contracts
description: Phase 1B - Detailed Contracts. Guides API design, integration points, and testing strategy.
---
 
# Phase 1B: Detailed Contracts
 
## Purpose
Define how components communicate and how the system will be tested.
 
## Output
A technical specification document (~400 lines) that defines:
- All API endpoints with requests, responses, and errors
- Integration points (events, external services)
- Comprehensive testing strategy
 
## Prerequisites
- Phase 1A complete (1a-discovery.md exists)
- Architecture and core data model approved
 
## Phase 1B Process
 
### Step 4: API Contract Design
 
#### Common Patterns
 
Before documenting individual endpoints, define shared patterns to avoid duplication:
 
##### Authentication & Authorization
```markdown
### Authentication
- **Method**: Firebase JWT tokens in `Authorization: Bearer {token}` header
- **Headers Required**:
  - `Authorization: Bearer {token}` - Firebase JWT token
  - `X-Tenant-Id: {tenant_id}` - Multi-tenancy identifier
  - `X-Identity-Provider-Type: firebase` - Identity provider type
- **Token Validation**: Every request validates token against Firebase Auth
- **Token Expiration**: Tokens expire after 1 hour
- **Refresh Strategy**: Client refreshes token using Firebase SDK
 
### Authorization
- **User Scope**: Users can only access their own resources unless admin role
- **Admin Role**: Can access any user's resources (for support)
- **Service-to-Service**: API keys for service authentication
- **Resource Ownership**: Validated via `user_id` in database matches authenticated user
```
 
##### Common Error Responses
```markdown
### Common Error Responses
 
All endpoints may return the following errors:
 
| Status | Code | Description | When |
|--------|------|-------------|------|
| 401 | UNAUTHORIZED | Missing or invalid token | No Authorization header or expired/invalid JWT |
| 403 | FORBIDDEN | Insufficient permissions | User lacks required role or cannot access this resource |
| 422 | VALIDATION_ERROR | Request validation failed | Pydantic/schema validation error, check error.details |
| 429 | RATE_LIMIT_EXCEEDED | Too many requests | User exceeded rate limit (see endpoint-specific limits) |
| 500 | INTERNAL_ERROR | Server error | Unexpected error, check logs with error.request_id |
| 503 | SERVICE_UNAVAILABLE | Service temporarily unavailable | Database or critical dependency is down |
 
**Error Response Format** (all endpoints):
```json
{
  "error": {
    "code": "ERROR_CODE",
    "message": "Human readable error message",
    "details": {
      "field": "specific_field",
      "reason": "validation failure reason"
    },
    "request_id": "uuid - For support/debugging",
    "timestamp": "ISO8601 - When error occurred"
  }
}
```
 
### Resource-Specific Common Errors
 
Define errors that apply to multiple endpoints for your resource:
 
| Status | Code | Description | When |
|--------|------|-------------|------|
| 404 | RESOURCE_NOT_FOUND | Resource doesn't exist | {resource}_id not in database |
| 404 | USER_NOT_FOUND | User doesn't exist | user_id not in database |
 
**Note**: Replace `RESOURCE` with your specific resource type (e.g., `DEADLINE`, `TASK`, `PRODUCT`)
```
 
##### Pagination Pattern
```markdown
### Pagination
 
All list endpoints follow this pattern:
 
**Query Parameters**:
- `limit`: integer (default: 50, max: 100) - Page size
- `offset`: integer (default: 0) - Number of items to skip
- `sort_by`: string - Field to sort by
- `sort_order`: string - 'asc' or 'desc' (default: 'asc')
 
**Response Format**:
```json
{
  "data": [...],
  "pagination": {
    "total": 150,
    "limit": 50,
    "offset": 0,
    "has_more": true
  }
}
```
```
 
##### Caching Strategy
```markdown
### Caching Strategy
 
- **Cache Headers**: Use `Cache-Control`, `ETag`, and `Last-Modified`
- **Cache Key Pattern**: `{resource}:{operation}:{params_hash}`
- **Cache TTL**:
  - List queries: 60 seconds
  - Single resource: 300 seconds (5 minutes)
  - Write operations: No caching
- **Cache Invalidation**: On create/update/delete of resource
```
 
#### Endpoint Specification
For each endpoint:
```markdown
#### POST /api/v1/favorites
**Purpose:** Add a product to user's favorites
 
**Authentication:** Required (JWT)
**Authorization:** User can only add to their own favorites
 
**Request:**
```json
{
  "productId": "uuid - Product to favorite"
}
```
 
**Response (201 Created):**
```json
{
  "id": "uuid - Favorite ID",
  "userId": "uuid - User ID",
  "productId": "uuid - Product ID",
  "createdAt": "ISO8601 timestamp"
}
```
 
**Error Responses:**
 
**Common Errors**: See [Common Error Responses](#common-error-responses) section above.
 
**Endpoint-Specific Errors**:
| Status | Code | Description | When |
|--------|------|-------------|------|
| 400 | INVALID_PRODUCT_ID | Invalid UUID format | productId is not valid UUID |
| 404 | PRODUCT_NOT_FOUND | Product doesn't exist | productId not in database |
| 409 | ALREADY_FAVORITED | Already in favorites | User already favorited this product |
 
**Rate Limiting:** 100 requests per minute per user
 
**Caching:** No caching (write operation)
```
 
#### API Versioning & Compatibility
```markdown
### Versioning Strategy
- **Approach:** URL versioning (`/api/v1/favorites`)
- **Alternative:** Header versioning (`Accept: application/vnd.api+json;version=1`)
 
### Breaking Change Policy
- No breaking changes to existing endpoints without version bump
- Deprecation notice: Minimum 6 months before removal
- Migration guide provided for all breaking changes
- Clients notified via: [email / changelog / dashboard]
 
### Backward Compatibility Checklist
- [ ] Existing clients continue working unchanged
- [ ] New required fields have defaults or are optional initially
- [ ] Removed fields return gracefully (empty/null) before removal
- [ ] Response schema additions are non-breaking
- [ ] Error codes remain stable
```
 
### Step 5: Integration Points
 
#### Events Published
```markdown
### Events Published
 
#### favorite.added
**Trigger:** User adds a product to favorites
**Payload:**
```json
{
  "eventId": "uuid",
  "eventType": "favorite.added",
  "eventVersion": "1.0",
  "timestamp": "ISO8601",
  "source": "favorites-service",
  "data": {
    "userId": "uuid",
    "productId": "uuid"
  },
  "metadata": {
    "requestId": "uuid",
    "source": "web" | "mobile" | "api"
  }
}
```
**Consumers:**
- Analytics Service (tracks user behavior)
- Recommendation Service (updates user preferences)
- Notification Service (may trigger emails)
 
**Delivery Guarantee:** At-least-once
**Ordering:** Not guaranteed (use eventId for idempotency)
**Retry Policy:** 3 retries with exponential backoff
```
 
#### External Service Calls
```markdown
| Service | Endpoint | Purpose | Timeout | Retry | Circuit Breaker | Failure Handling |
|---------|----------|---------|---------|-------|-----------------|------------------|
| Product Service | GET /products/:id | Validate product exists | 2s | 3x with backoff | 5 failures/30s | Return 404 to user |
| Analytics | POST /events | Track favorite action | 5s | None | N/A | Log error, continue |
```
 
#### Dependency Analysis
```markdown
### This Service Depends On:
| Service | Criticality | Failure Impact | Mitigation |
|---------|-------------|----------------|------------|
| Product Service | High | Can't validate products | Cache product existence for 5min |
| Auth Service | Critical | All requests fail | Short-circuit with cached token validation |
| Database | Critical | Full outage | Multi-AZ, read replicas |
 
### Services That Depend On Us:
| Service | Usage | SLA Commitment | Breaking Change Impact |
|---------|-------|----------------|------------------------|
| Recommendation | Reads favorites via API | 99.9% uptime | Must maintain v1 API |
| Analytics | Consumes favorite.* events | Best effort | Schema versioned |
```
 
### Step 6: Testing Strategy
 
#### Unit Testing
```markdown
**Coverage Target:** 80% line coverage, 100% for critical paths
 
**Components Requiring Unit Tests:**
- [ ] Service layer (business logic)
- [ ] Repository layer (data access)
- [ ] Validation logic
- [ ] Error handling paths
 
**Mock Boundaries:**
- External services (Product Service, Analytics)
- Database (use in-memory or mocks)
- Time/dates (use fixed timestamps)
- Random generation (use seeded values)
```
 
#### Integration Testing
```markdown
**Scope:** API endpoints with real database
 
**Test Database Strategy:**
- [ ] Dedicated test database
- [ ] Transaction rollback per test
- [ ] Docker containers (testcontainers)
 
**Test Data Strategy:**
- Use factories for test data creation
- Seed data for common scenarios
- Clean up after each test suite
 
**Key Integration Scenarios:**
| Scenario | Components | Expected Outcome |
|----------|------------|------------------|
| Add favorite | API → Service → DB | Favorite persisted, 201 returned |
| Duplicate favorite | API → Service → DB | 409 Conflict returned |
| List with pagination | API → Service → DB | Correct page returned |
```
 
#### Contract Testing
```markdown
**API Contracts:**
- [ ] OpenAPI spec validation
- [ ] Request/response schema validation
- [ ] Error response format validation
 
**Event Contracts:**
- [ ] Event schema validation (JSON Schema)
- [ ] Consumer contract tests (Pact or similar)
```
 
#### Load/Performance Testing
```markdown
**Tools:** [k6 / Locust / Gatling / Artillery]
 
**Test Scenarios:**
| Scenario | Load | Duration | Success Criteria |
|----------|------|----------|------------------|
| Baseline | 100 req/s | 5 min | p99 < 200ms |
| Peak | 1000 req/s | 10 min | p99 < 500ms, 0% errors |
| Stress | Ramp to failure | Until failure | Identify breaking point |
| Soak | 500 req/s | 1 hour | No memory leaks, stable latency |
```
 
#### End-to-End Testing
```markdown
**Critical User Journeys:**
1. User adds favorite → appears in list
2. User removes favorite → removed from list
3. User views favorites → paginated correctly
```
 
## Output Document Structure
 
```markdown
# Design: [Feature Name] - Phase 1B: Detailed Contracts
 
**Feature Name**: [Feature Name]  
**Date**: [YYYY-MM-DD]  
**Status**: Phase 1B - API Contracts & Integration Points  
**Version**: 1.0  
**Author**: [Name/Team]
 
---
 
## API Contracts
 
### Base URL & Versioning
 
**Base URL**: `{SERVICE_URL}/v1/...`
 
**Versioning Strategy**:
- **Approach**: URL versioning (`/v1/...`)
- **Current version**: v1
- **Breaking change policy**: Minimum 6 months deprecation notice
- **Migration path**: v2 endpoints live alongside v1 until deprecation complete
 
### Authentication & Authorization
 
[Define common auth/authz patterns - see Common Patterns section]
 
### Common Error Responses
 
[Define all common errors that apply across endpoints - see Common Patterns section]
 
### Common Patterns
 
[Define pagination, caching, rate limiting patterns - see Common Patterns section]
 
---
 
### Endpoints
 
#### [Endpoint 1]
 
[Full specification with requests, responses, errors]
 
**Error Responses**:
 
**Common Errors**: See [Common Error Responses](#common-error-responses) above.
 
**Endpoint-Specific Errors**:
| Status | Code | Description | When |
|--------|------|-------------|------|
[Only document errors unique to this endpoint]
 
#### [Endpoint 2]
 
[Full specification with requests, responses, errors]
 
### API Versioning Strategy
 
**Backward Compatibility Checklist**:
- ✅ Existing clients continue working unchanged
- ✅ New required fields have defaults or are optional initially
- ✅ Removed fields return gracefully (null) before hard removal
- ✅ Response schema additions are non-breaking (new fields appended)
- ✅ Error codes remain stable (no code changes for same error)
 
**Breaking Change Policy**:
- Minimum 6 months deprecation notice
- Deprecation header: `X-API-Deprecated: true; sunset="YYYY-MM-DD"`
- Migration guide published on docs site
- Email notification to all API consumers
- v1 and v2 run in parallel during transition
 
---
 
## Integration Points
 
### Events Published
[List of events with full schemas and consumers]
 
### Events Consumed
[List of events we subscribe to]
 
### External Service Dependencies
 
| Service | Endpoint | Purpose | Timeout | Retry | Circuit Breaker | Failure Handling |
|---------|----------|---------|---------|-------|-----------------|------------------|
[Document all external dependencies with failure strategies]
 
### Dependency Analysis
 
**This Service Depends On**:
| Service | Criticality | Failure Impact | Mitigation |
|---------|-------------|----------------|------------|
[What we depend on and how we handle failures]
 
**Services That Depend On Us**:
| Service | Usage | SLA Commitment | Breaking Change Impact |
|---------|-------|----------------|------------------------|
[Who depends on us and our obligations]
 
---
 
## Testing Strategy
 
### Unit Testing
[Coverage targets, what to test, mock boundaries]
 
### Integration Testing
[Database strategy, test scenarios]
 
### Contract Testing
[API and event contract validation]
 
### Load/Performance Testing
[Tools, scenarios, success criteria]
 
### End-to-End Testing
[Critical user journeys]
 
---
 
## Next Steps
- Phase 1C: Address security, performance, deployment, and operational concerns
```
 
## Quality Checklist for Phase 1B
 
Before moving to Phase 1C, verify:
 
### Completeness
- [ ] Common patterns section defined (auth, errors, pagination, caching)
- [ ] All API endpoints specified with error cases
- [ ] Only endpoint-specific errors documented (common errors referenced)
- [ ] All integration points identified
- [ ] All events defined with consumers
- [ ] Testing strategy defined for all layers
- [ ] Error response format is standardized
 
### Quality
- [ ] Error handling strategy is clear and consistent
- [ ] Common errors documented once and referenced
- [ ] Backward compatibility addressed
- [ ] Event schemas are complete
- [ ] Test scenarios cover critical paths
 
### Clarity
- [ ] API contracts are unambiguous
- [ ] Integration points are well-documented
- [ ] Testing approach is practical and achievable
- [ ] Error response format is documented with examples
 
## Anti-Patterns to Avoid
 
❌ **Duplicating common errors across endpoints**
```
Documenting 401 UNAUTHORIZED in every endpoint
→ Document once in "Common Error Responses", reference from endpoints
```
 
❌ **No error response format**
```
Only listing error codes
→ Define standard JSON error response structure
```
 
❌ **No authentication/authorization pattern**
```
Repeating "JWT required" on every endpoint
→ Define once in "Authentication & Authorization" section
```
 
❌ **No error handling**
```
Only defining happy path
→ Define all error cases and how to handle them
```
 
❌ **Vague contracts**
```
"Returns user data"
→ "Returns {id: uuid, name: string, email: string}"
```
 
❌ **Skipping testing strategy**
```
"We'll add tests later"
→ Define testing approach upfront, include in estimates
```
 
## Timeline Guidance
 
| Complexity | API Design | Integration | Testing | Total |
|------------|------------|-------------|---------|-------|
| Simple feature | 30-45 min | 15-30 min | 15-30 min | 1-1.5 hours |
| Medium complexity | 45-60 min | 30-45 min | 30-45 min | 2-2.5 hours |
| Complex system | 1-2 hours | 1-2 hours | 1 hour | 3-5 hours |
 
## Definition of Done
 
Phase 1B is complete when:
- [ ] All API endpoints documented
- [ ] Integration points defined
- [ ] Testing strategy complete
- [ ] User approves: "API contracts and integration look good"
- [ ] Document saved to `.monkeymode/{feature-name}/design/1b-contracts.md`