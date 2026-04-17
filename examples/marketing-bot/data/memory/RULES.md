# Rules

The harness treats every `[R-*]` entry below as a hard policy rule enforced by
`RulesEnforcementMW`. Operators may add project-specific rules; do not remove
the shipped ones without a security review.

- [R-1] DENY_TOOL: git push
- [R-2] DENY_SANDBOX_WRITE: /etc/**
- [R-3] DENY_PATTERN: (?i)\bsend\s+email\b
- [R-4] REQUIRE_APPROVAL: publish_post
- [R-5] REQUIRE_APPROVAL: charge_credit_card
