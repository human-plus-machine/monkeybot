---
name: design-operations
description: Phase 1C - Production Readiness. Guides security, performance, deployment, observability, and risk assessment.
---
 
# Phase 1C: Production Readiness
 
## Purpose
Ensure the design is ready for production deployment with proper security, performance, and operational excellence.
 
## Output
An operational specification document (~400 lines) covering:
- Security design
- Performance & scalability strategy
- Deployment strategy
- Observability (logging, metrics, tracing)
- Risk assessment
 
## Prerequisites
- Phase 1A and 1B complete
- Architecture, data model, and API contracts approved
 
## Phase 1C Process
 
### Step 7: Security Design
 
```markdown
### Authentication
- JWT tokens with 1-hour expiration
- Refresh tokens with 30-day expiration
- Token validation on every request
 
### Authorization
- Users can only access their own favorites
- Admin role can view any user's favorites (for support)
- Service-to-service calls use API keys with IP whitelist
 
### Input Validation
- All UUIDs validated for format
- Request body size limited to 1KB
- Rate limiting: 100 req/min per user
- SQL injection prevention (parameterized queries)
- XSS prevention (input sanitization, output encoding)
 
### Data Protection
- No PII in favorites table
- Audit log for all favorite additions/removals
- GDPR: Favorites deleted when user account deleted
- Encryption at rest: [Yes/No - specify method]
- Encryption in transit: TLS 1.3
 
### Security Headers
- CORS: Whitelist known domains only
- CSP: Restrict script sources
- HSTS: Force HTTPS (max-age=31536000)
- X-Content-Type-Options: nosniff
- X-Frame-Options: DENY
 
### Secrets Management
- Database credentials: [Vault / AWS Secrets Manager / etc.]
- API keys: Environment variables (never in code)
- Rotation policy: [Every X days]
```
 
### Step 8: Performance & Scalability
 
```markdown
### Expected Load
- 1000 requests/second peak
- 10M users
- Average 50 favorites per user
- 500M total favorites records
 
### Performance Targets
| Metric | Target | Alert Threshold |
|--------|--------|-----------------|
| p50 latency | < 50ms | > 100ms |
| p95 latency | < 200ms | > 400ms |
| p99 latency | < 500ms | > 1s |
| Error rate | < 0.1% | > 1% |
| Availability | 99.9% | < 99.5% |
 
### Optimization Strategy
 
**Database:**
- Composite index on (user_id, product_id)
- Index on product_id for reverse lookups
- Connection pooling (min: 10, max: 50)
- Query timeout: 5 seconds
 
**Caching:**
| Data | Cache | TTL | Invalidation |
|------|-------|-----|--------------|
| User's favorite list | Redis | 5min | On add/remove |
| Product existence | Redis | 1hour | On product delete |
| User's favorite count | Redis | 5min | On add/remove |
 
**Query Optimization:**
- Paginate favorites list (limit: 50 per page)
- Use SELECT only needed columns
- Avoid N+1 queries (batch product lookups)
- Use cursor-based pagination for large datasets
 
### Scalability Plan
 
**Horizontal Scaling:**
- Stateless service (can add more instances)
- Load balancer distributes traffic (round-robin)
- Auto-scaling: 2-10 instances based on CPU (target: 70%)
 
**Database Scaling:**
- Read replicas for favorite list queries (2 replicas)
- Write to primary for add/remove
- Partition by user_id if > 1B records
- Consider sharding strategy for global scale
 
**Caching Scaling:**
- Redis Cluster for > 100GB cache
- Cache-aside pattern for flexibility
- Circuit breaker on cache failures (fallback to DB)
```
 
### Step 9: Deployment Strategy
 
```markdown
### Rollout Approach
- [ ] Big bang (all at once) - Simple features
- [x] Blue-green deployment - Zero downtime
- [ ] Canary release - High-risk changes
- [ ] Feature flags - Gradual enablement
 
### Deployment Pipeline
```
Code → Build → Unit Tests → Integration Tests →
Staging Deploy → Smoke Tests → Production Deploy → Health Checks
```
 
### Feature Flags (if applicable)
| Flag Name | Default | Description | Rollout Plan |
|-----------|---------|-------------|--------------|
| `enable_favorites_v2` | OFF | New favorites implementation | 1% → 10% → 50% → 100% |
 
### Health Checks
- **Liveness:** `/health/live` - App is running
- **Readiness:** `/health/ready` - App can serve traffic
- **Startup:** Allow 30s for initialization
 
### Rollback Plan
**Trigger Conditions:**
- Error rate > 5% for 5 minutes
- p99 latency > 2s for 5 minutes
- Health check failures > 3 consecutive
 
**Rollback Steps:**
1. Automatic: Deployment fails health checks → previous version restored
2. Operator-triggered: `kubectl rollout undo deployment/favorites` or equivalent
3. Database: [Migration rollback steps if applicable]
 
**Rollback Time Target:** < 5 minutes
 
### Post-Deployment Verification
- [ ] Health checks passing
- [ ] Key metrics within normal range
- [ ] No error spikes in logs
- [ ] Smoke tests passing
- [ ] Synthetic monitoring green
```
 
### Step 10: Observability
 
```markdown
### Logging
**Level Guidelines:**
- DEBUG: Detailed diagnostic info (disabled in prod)
- INFO: Normal operations (request/response summary)
- WARN: Unexpected but handled situations
- ERROR: Failures requiring attention
- CRITICAL: System-wide failures
 
**Log Format (Structured JSON):**
```json
{
  "timestamp": "2024-01-15T10:30:00Z",
  "level": "INFO",
  "service": "favorites-service",
  "requestId": "abc123",
  "userId": "user456",
  "operation": "addFavorite",
  "productId": "prod789",
  "durationMs": 45,
  "message": "Favorite added successfully"
}
```
 
**What to Log:**
- All API requests (method, path, status, duration)
- Business operations (add, remove, list)
- Errors with stack traces
- External service calls (success/failure, duration)
 
**What NOT to Log:**
- Passwords, tokens, API keys
- Full credit card numbers
- Personal health information
- Large request/response bodies
 
### Metrics
| Metric | Type | Labels | Alert Threshold |
|--------|------|--------|-----------------|
| `http_requests_total` | Counter | method, path, status | N/A |
| `http_request_duration_seconds` | Histogram | method, path | p99 > 1s |
| `favorites_operations_total` | Counter | operation, status | error_rate > 1% |
| `db_connections_active` | Gauge | pool | > 80% capacity |
| `cache_hit_ratio` | Gauge | cache_name | < 80% |
 
### Tracing
- Distributed tracing with OpenTelemetry
- Trace context propagation via W3C headers
- Sample rate: 10% normal, 100% errors
 
### Alerting
| Alert | Condition | Severity | Action |
|-------|-----------|----------|--------|
| High Error Rate | error_rate > 1% for 5min | Critical | Page on-call |
| High Latency | p99 > 1s for 5min | Warning | Slack notification |
| DB Connection Pool | > 80% for 5min | Warning | Slack notification |
| Service Down | health check fails 3x | Critical | Page on-call |
 
### Dashboards
- **Overview:** Request rate, error rate, latency percentiles
- **Database:** Query performance, connection pool, slow queries
- **Cache:** Hit ratio, memory usage, evictions
- **Business:** Favorites added/removed per hour, active users
```
 
### Step 11: Risk Assessment
 
```markdown
| Risk | Likelihood | Impact | Mitigation | Owner |
|------|------------|--------|------------|-------|
| Database write contention on favorites table | Medium | Medium | Optimistic locking, proper indexes | Backend |
| Product service unavailable | Low | High | Cache product existence for 5min, graceful degradation | Backend |
| Favorites table grows too large | High | Medium | Partition by user_id, archive inactive data | DBA |
| Race condition on duplicate favorites | Medium | Low | Unique constraint, idempotent operations | Backend |
| Cache stampede on popular products | Low | Medium | Cache warming, staggered TTLs | Backend |
 
### Risk Monitoring
- Set up alerts for each high/critical risk indicator
- Review risks weekly during initial rollout
- Update risk assessment after each incident
```
 
## Output Document Structure
 
```markdown
# Design: [Feature Name] - Phase 1C: Production Readiness
 
## Security Design
[Authentication, Authorization, Input Validation, Data Protection, Secrets]
 
## Performance & Scalability
[Expected Load, Performance Targets, Optimization, Scaling Strategy]
 
## Deployment Strategy
[Rollout Approach, Pipeline, Health Checks, Rollback Plan]
 
## Observability
[Logging, Metrics, Tracing, Alerting, Dashboards]
 
## Risk Assessment
[Risks with Likelihood, Impact, Mitigation, Owner]
 
## Final Sign-Off
- [ ] Security reviewed
- [ ] Performance targets achievable
- [ ] Deployment plan clear
- [ ] Observability in place
- [ ] Risks identified and mitigated
```
 
## Quality Checklist for Phase 1C
 
Before finalizing design, verify:
 
### Completeness
- [ ] Security considered at every layer
- [ ] Performance targets defined and achievable
- [ ] Deployment strategy defined
- [ ] Monitoring and alerting defined
- [ ] All risks identified with mitigations
 
### Quality
- [ ] Scalability plan addresses 10x growth
- [ ] Rollback plan exists
- [ ] Production-ready (not just MVP thinking)
 
## Anti-Patterns to Avoid
 
❌ **No scalability consideration**
```
"It works for 100 users"
→ Plan for 10x growth: "It works for 1000 users and can scale to 10,000"
```
 
❌ **No rollback plan**
```
"Deploy and hope for the best"
→ Define rollback triggers, steps, and time targets
```
 
## Timeline Guidance
 
| Complexity | Security | Perf/Scale | Deploy/Observe | Total |
|------------|----------|------------|----------------|-------|
| Simple | 30 min | 30 min | 45 min | 1.75 hours |
| Medium | 1 hour | 1 hour | 1.5 hours | 3.5 hours |
| Complex | 2 hours | 2 hours | 2 hours | 6 hours |
 
## Definition of Done
 
Phase 1C is complete when:
- [ ] Security considerations reviewed
- [ ] Performance targets defined
- [ ] Deployment strategy clear
- [ ] Observability planned
- [ ] Risks assessed
- [ ] User approves: "Complete design ready for implementation"
- [ ] Document saved to `.monkeymode/{feature-name}/design/1c-operations.md`