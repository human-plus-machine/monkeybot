# Rules

The harness treats every `[R-*]` entry below as a hard policy rule enforced by
`RulesEnforcementMW`. Operators may add project-specific rules; do not remove
the shipped ones without a security review.

- [R-1] DENY_TOOL: git push
- [R-2] DENY_SANDBOX_WRITE: /etc/**
- [R-3] DENY_SANDBOX_WRITE: ~/.ssh/**
- [R-4] DENY_PATTERN: (?i)\brm\s+-rf\s+/\b
- [R-5] REQUIRE_APPROVAL: git commit
- [R-6] REQUIRE_APPROVAL: npm publish
