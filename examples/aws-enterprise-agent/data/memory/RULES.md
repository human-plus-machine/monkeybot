# Rules

The harness treats every `[R-*]` entry below as a hard policy rule enforced by
`RulesEnforcementMW`. Operators may add project-specific rules; do not remove
the shipped ones without a security review.

- [R-1] DENY_TOOL: git push
- [R-2] DENY_SANDBOX_WRITE: /etc/**
- [R-3] DENY_SANDBOX_WRITE: ~/.aws/**
- [R-4] DENY_PATTERN: (?i)\baws\s+configure\b
- [R-5] DENY_PATTERN: (?i)\bDROP\s+TABLE\b
- [R-6] REQUIRE_APPROVAL: bedrock:DeleteGuardrail
- [R-7] REQUIRE_APPROVAL: secretsmanager:Delete*
