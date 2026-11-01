# Appendix A — Tool error codes (`details.code`)

> Part of the [regression plan](../README.md). Section numbers (§5.9, §6.U, D12, …) are global across the plan; the README's section map says which file holds each one.


Workspace (`WorkspaceError` via `_workspace_error_envelope`): `invalid_file_glob`, `incomplete_scan`, `ambiguous_replace`, `not_found_replace`, `invalid_path`, `path_escape`, `skills_read_only`, `missing_path`, `credential_denied`, `path_needs_grant`, `invalid_write_scope`, `write_outside_scope`, `not_found`, `list_failed`, `invalid_offset`, `invalid_limit`, `payload_too_large`, `write_failed`, `is_directory`, `delete_failed`, `no_change`, `empty_old_string`, `missing_pattern`, `invalid_pattern`, `glob_failed`, `invalid_context`, `invalid_regex`, `invalid_max_matches`, `invalid_cwd`, `workspace_error`.

Read ledger (`error_kind: policy`): `read_before_write`, `stale_read`.

Patch (`PatchError.code`): `invalid_hunk_line`, `missing_markers`, `empty_hunk`, `empty_patch`, `ambiguous_context`, `context_not_found`, `hunk_not_found`, `already_exists`, `not_found`, `internal`, `rollback_failed`, fallback `patch_error`.

No `details.code` (assert on `error_kind` / message instead): `run_command` allowlist/path `SecurityError`, parse `ValueError`, timeout (`partial_output_path`), `JobCapExceededError`, unknown `job_id`, `credential_egress_blocked`, unknown tool, `load_file` string errors, `enable_mcp` exceptions, all `computer_*` errors.


## Appendix B — SSE event types

`Thinking`, `AssistantDelta`, `AssistantTextStarted`, `AssistantTextEnded`, `ToolInputDelta`, `ToolCallStarted`, `ToolCallResult`, `TurnComplete`, `Error`, `ContextSummarizing`, `ContextSummarized`, `ContextUsage`, `ContextEpochStarted`, `SystemContextUpdated`, `SystemPromptSnapshot`, `ImageBlock`, `FileBlock`, `FileChange`, `ThinkingBlockStarted`, `ThinkingBlockDelta`, `ThinkingBlockComplete`, `RedactedThinkingBlock`, `ToolConfirmationRequest`, `AskUserRequest`, `ActionRequiredEvent`, `FrontendToolRequest`, `SystemNotificationEvent`, `AttachmentDescriptor`, `GroundingEvent`, `UserSteered`, `QueuedInputAccepted`, `ConfigReloaded`, `SubagentStarted`, `SubagentEvent`, `SubagentCompleted`, `CredentialEgressBlocked`, `VerifierVerdict`, `HistoryRewritten`. Framing-only: `ActiveRequests` (no `id:`), `: ping N`. Transcript-only: `HarnessIntervention`.

Realtime WS client → server: `connect`, `text`, `interrupt`, `audio_stream_end`, `close`, `tool_confirmation_response`, `elicitation_response`. Server → client: `connected`, `text_delta`, `audio` (binary), `interrupted`, `turn_boundary`, `tool_call`, `tool_result`, `user_transcript`, `usage`, `tool_confirmation`, `elicitation`, `error`, `session_ended`.


## Appendix C — Harness interventions

`doom_loop`, `truncated_batch` (`stream_truncated` | `all_parse_error`), `empty_completion` (`retry` | `exhausted`), `post_tool_empty` (`retry` | `exhausted`), `background_jobs_nudge`, `compaction_fallback`, `max_turns`, `verifier_replan`, `final_answer_gate`.


## Appendix D — HTTP routes (snapshot source for T1-GW-05)

`GET /health`; `POST /sessions`; `DELETE /sessions/{session_id}`; `POST /sessions/{session_id}/reply`; `POST /sessions/{session_id}/steer`; `POST /sessions/{session_id}/queue`; `POST /sessions/{session_id}/queue/{request_id}/steer`; `DELETE /sessions/{session_id}/queue/{request_id}`; `POST /sessions/{session_id}/attachments`; `GET /sessions/{session_id}/events`; `POST /sessions/{session_id}/cancel`; `POST /sessions/{session_id}/tool-confirmations/{tool_call_id}`; `POST /sessions/{session_id}/ask-user/{tool_call_id}`; `POST /sessions/{session_id}/elicitations/{elicitation_id}`; `POST /sessions/{session_id}/frontend-tool-results/{tool_call_id}`; `GET /sessions/{session_id}/changes`; `GET /sessions/{session_id}/changes/{change_id}/content`; `POST /sessions/{session_id}/changes/revert`; `GET /sessions/{session_id}/usage`; `GET /usage`; `GET /api/workspace/tree`; `GET /api/workspace/file`; `GET /api/chat-history`; `GET /api/chat-history/{session_id}`; `POST /api/chat-title`; `DELETE /api/chat-history/{session_id}`; `GET /sessions/{session_id}/branches`; `POST /sessions/{session_id}/branches`; `PUT /sessions/{session_id}/branches/active`; `POST /sessions/{session_id}/truncate`; `POST /sessions/{session_id}/fork`; `POST /scheduler/loops`; `GET /scheduler/loops`; `GET /scheduler/loops/{loop_id}`; `POST /scheduler/loops/{loop_id}/pause|resume|stop`; `POST /scheduler/invoke-tick`; `GET /goals`; `GET /goals/{goal_id}`; `POST /goals/{goal_id}/pause|resume|stop`; `GET /admin/config`; `POST /admin/config/reload`; `GET /realtime/health`; `GET /realtime/sessions/{session_id}`; `WS /sessions/{session_id}/realtime`; `GET /openapi.json`; `HEAD /openapi.json`; `GET /docs`; `HEAD /docs`; `GET /docs/oauth2-redirect`; `HEAD /docs/oauth2-redirect`; `GET /redoc`; `HEAD /redoc`.
