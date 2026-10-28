# 4. File structure

> Part of the [regression plan](../README.md). Section numbers (§5.9, §6.U, D12, …) are global across the plan; the README's section map says which file holds each one.


Legend used in every tree below:

- `+` marks a new file or folder.
- `←` gives the old path of a moved file. Under `tests/unit/`, a file with no `←` keeps its name and comes from the same relative path under `tests/` (for example `tests/unit/providers/test_nvidia.py` ← `tests/providers/test_nvidia.py`). Files under `tests/unit/core/**` come from `tests/core/` with the same file name.
- `(Tn-AREA-nn)` links a file to its case IDs in section 6.
- `__init__.py` files are omitted below; every test folder gets one (section 4.6).

## 4.1 Repository top level after the change

```text
monkeybot/
  src/monkeybot/
    core/testing/                     # stays in src: the gateway needs it at runtime (MODEL_PROVIDER=fake)
      mocks_provider.py               # ScriptedFakeProvider -> ScriptedProvider (multi-turn, strict)
      mocks_realtime_provider.py      # unchanged
  testkit/                        +   # shared test doubles; imported by tests/ and regression/, never shipped
  tests/                              # TIER 1 ONLY: unit + contract tests
    conftest.py                       # root conftest (sandbox off, layout env isolation, aiosqlite reap); only the plugin path changes
    unit/                         +   # every existing unit test, re-homed to mirror src/monkeybot/
    contracts/                    +   # new cross-cutting tier-1 contracts and their goldens
  regression/                     +   # TIERS 2-5
    t2/  t3/  t4/  t5/
  cli/tests/                          # unchanged (separate package)
  integrations/browser-mcp/tests/     # unchanged (separate package)
  docs/
    testing.md                    +   # test tiers, gates, authoring rules, failure policy, case catalog (5.10)
    live-evals.md                     # kept as the T4 runbook; links to testing.md
  test-runs/                      +   # gitignored failure ledger: one run file per test run (5.9)
  .cursor/
    rules/
      test-failures.mdc           +   # always-on agent rule: record failures, do not auto-fix (5.9.4)
    hooks.json                        # gains a postToolUse(Shell) entry; existing pre-pr.py entries unchanged
    hooks/
      pre-pr.py                       # unchanged
      test-failure-notice.py      +   # attaches the "recorded, do not fix" notice after test commands
  pytest.ini                          # testpaths = tests regression; pythonpath = .; -p testkit.failure_ledger
  pyproject.toml                      # ruff/mypy paths gain testkit, regression
  .gitignore                          # + /test-runs/, /regression/t4/runs/, /regression/t4/agent/.env, /.cursor/hooks/.state/
  scripts/
    precommit-pytest.sh               # collection fallback covers regression (section 4.6)
    ci-local.sh                       # runs tests + regression -m "t1 or t2 or t3"; sets MONKEYBOT_TEST_RUN_GROUP
  evals/                              # DELETED after cutover (section 4.5)
```

Why this split:

- `tests/` stays where every tool already looks (`pytest.ini`, `precommit-pytest.sh`, `ci-local.sh`, IDE runners). Making it tier-1 only means `uv run pytest tests` is always the fast, hermetic loop.
- `regression/` holds anything that drives the whole harness (loop, gateway, live agent). It needs heavier fixtures, cassettes, goldens and a live agent project, none of which belong next to unit tests.
- `testkit/` exists because both roots need the same doubles. Today 45 test files make 66 `from tests.core.test_loop import FakeProvider`-style imports between test modules; a separate package ends that. It is a sibling of `src/` rather than inside it, so it is not in the wheel.

## 4.2 `testkit/` (shared doubles)

```text
testkit/                          +
  __init__.py                         # empty: stays stdlib-only so the ledger plugin loads in cli/ and browser-mcp; import doubles from their modules
  py.typed
  provider.py                         # re-exports ScriptedProvider; adds CapturingProvider, ToolsRecordingProvider,
                                      #   CountingProvider (today copied in test_loop, test_core_tool_executor,
                                      #   test_hooks, test_verifier_*, test_loop_observability)
  history.py                          # FakeHistory: one full HistoryStore (dedupes 4 copies)
  executors.py                        # RecordingExecutor: scripted envelopes, running_background_jobs() schedule
  inspectors.py                       # AllowInspector, DenyInspector, ConfirmInspector, OrderRecordingInspector
  pending_bus.py                      # PresetPendingBus: confirmations, ask_user, elicitations, never-answer
  context.py                          # make_ctx / make_loop_ctx / ctx_with_task (today _ctx, _loop_ctx, _ctx_with_task)
  mcp.py                              # NoMCP, RecordingMCP, FakeMCPClient
  memory.py                           # mem_sub, make_memory_subsystem  ← tests/core/memory/helpers.py
  palace.py                           # InMemoryPalace  ← tests/core/memory/in_memory_palace.py
  hooks.py                            # payload() builder, RecordingHook (today test_hooks._payload)
  verifier.py                         # ScriptedClassifier, ScriptedJudge, mailbox helpers
  sandbox.py                          # make_opensandbox_module (today in test_sandbox_executor)
  conversations.py                    # typed_messages_four_turn, typed_messages_turn_2b_tool_only
                                      #   (today tests/providers/conftest.py, imported by core tests)
  clock.py                            # FakeClock (needs the now_ms seam in src, section 5.1)
  subagent_worker.py                  # fake worker; replays an NDJSON tape (MONKEYBOT_SUBAGENT_SCRIPT)
  web_search.py                       # FakeWebSearchBackend
  sse.py                              # SSE frame parser + streaming reader (id:/data:/comments, stop predicate)
  gateway_ports.py                    # FakeLoopPort (today in tests/gateway/sse/test_routes.py)
  gateway.py                          # in-process create_app + LifespanManager + scripted provider + temp agent root
  backends.py                         # open_backend(kind): sqlite | postgres | firestore, shared skip messages
  scrub.py                        +   # secret scrubber (stdlib only); used by the ledger and the cassette recorders
  failure_ledger.py               +   # pytest plugin + LedgerWriter API (stdlib only; 5.9)
  failures/                       +   # triage CLI: python -m testkit.failures latest|open|show|prompt|mark|export|prune
    __main__.py                   +
    store.py                      +   # reads run files, outcomes, triage.json; fingerprint logic
    catalog.py                    +   # parses docs/testing.md §8 (shared with test_catalog_pairing.py)
  data/
    attachments/                      # tiny.png, tiny.jpg, text_layer.pdf, no_text_layer.pdf (T1 PROV + T2 ATT)
```

Rule: `testkit` imports only `monkeybot` and third-party packages. It never imports `tests` or `regression`. `failure_ledger.py` and `scrub.py` import only the standard library, so the plugin can also load in `cli/tests` and `integrations/browser-mcp/tests`.

## 4.3 `tests/` (tier 1)

```text
tests/
  conftest.py                         # unchanged except: pytest_plugins path (P0A-04); marks tests/evals t2 until P1-12 (P0A-05)
  unit/                           +
    conftest.py                   +   # marks everything under it t1
    testkit/                      +   # self-tests for the doubles
      test_scripted_provider.py   +   # strict mode, multi-turn, raise step
      test_fake_history.py        +   # P0A-03
      test_sse_reader.py          +
      test_clock.py               +
      test_failure_ledger.py      +   # pytester-based (5.9.6)
      test_failures_cli.py        +
      test_scrub.py               +
    packaging/                    +
      test_cold_start.py              ← tests/test_cold_start.py
      test_packaging_config.py        ← tests/test_packaging_config.py
      test_release_script.py          ← tests/test_release_script.py
    core/
      fixtures/
        bitbucket_dc_style_pr.diff    ← tests/core/fixtures/
      test_agent_layout.py
      test_bootstrap.py
      test_layout.py
      test_logging_utils.py
      test_path_safety.py
      test_workspace_layout.py
      attachments/
        test_attachment_freeze.py
        test_attachment_store.py
        test_image_preview.py
      coding/
        test_verification_gate.py
      config/
        test_config_extensions.py
        test_env_map_grep_gates.py
        test_runtime_config_snapshot.py
        test_runtime_env.py
      context/
        test_context.py
        test_context_epoch.py
        test_project_context.py
        test_secret_egress.py
        test_slash_skills.py
        test_tool_result_ingress.py
        test_tool_shapers.py
        test_skill_discovery_rules.py +  (T1-SKILL-01)
      goals/
        test_goals.py
        test_goal_persistence.py
      hooks/
        test_hooks.py
        test_evidence_guard.py
        test_settlement.py
      llm/
        test_pricing.py
        test_provider_contract.py
      mcp/
        test_mcp_client.py
        test_mcp_oauth.py
        test_mcp_oauth_regressions.py
      memory/
        test_mempalace.py             ← tests/core/memory/
        test_review_fixes.py          ← tests/core/memory/
        test_migrate_embedding.py
        test_memory_layout_migrate.py
        test_memory_scoping.py    +  (T1-MEM-01..03)
      messages/
        test_gemini_history_replay.py
        test_message_pipeline.py
        test_tool_integrity.py
      persistence/
        test_branch_store_backends.py
        test_durable_runs.py
        test_firestore_history.py
        test_firestore_scheduled_loops.py
        test_firestore_usage.py
        test_goal_ledger.py
        test_history.py
        test_history_branches.py
        test_history_row_ids.py
        test_runs.py
        test_scheduled_loops.py
        test_session_branches_migration.py
        test_session_turn_locks.py
        test_storage_backends.py
        test_thread_summary_wire.py
        test_transcript.py
        test_transcript_reader.py
        test_usage.py
        test_usage_migration.py
      prompts/
        test_harness_prompt.py
        test_prompt.py
      runtime/
        test_background_summary.py
        test_chat_title.py
        test_context_budget.py
        test_doom_loop.py
        test_durable_events.py
        test_empty_completion.py
        test_events.py
        test_history_compaction_split.py
        test_input_admission.py
        test_loop.py                      # FakeProvider/FakeHistory/RecordingExecutor move to testkit
        test_loop_hooks.py
        test_loop_observability.py
        test_loop_transcript.py
        test_provider_stream_mapper.py
        test_realtime_loop.py
        test_truncated_tool_batch.py
        test_turn_session_id.py
        test_utterance_buffer.py
      subagents/
        test_progress_publish.py
        test_subagent_proto.py
        test_subagent_trace_propagation.py
        test_subagent_transcript.py
        test_subagent_worker_paths.py
        test_worker_pool.py
        test_child_tool_list.py   +  (T1-SUB-01)
      testing/
        test_mocks_realtime_provider.py
      tools/
        test_ask_user.py
        test_background_jobs.py
        test_core_tool_executor.py        # _mem_sub/_NoMCP move to testkit
        test_fs_isolation.py
        test_gitignore.py
        test_grant_store.py
        test_inspector.py
        test_patch.py
        test_path_grant_inspector.py
        test_permission.py
        test_replace_fuzzy.py
        test_run_command_parse.py
        test_sandbox_executor.py
        test_spill_dynamic.py
        test_spill_inventory.py
        test_terminal.py
        test_workspace_extra_read_roots.py
        test_workspace_symlinks.py
        test_workspace_write_scope.py
      types/
        test_content_blocks.py
        test_tool_def_schema.py
      verifier/
        test_progress_tracker.py
        test_verifier_binding.py
        test_verifier_inspector.py
        test_verifier_judge.py
        test_verifier_mailbox_lifecycle.py
        test_verifier_rewrite_effects.py
        test_verifier_sticky_nudge.py
        test_verifier_tracker.py
      workspace/
        test_factory.py               ← tests/core/workspace/
        test_local_storage.py         ← tests/core/workspace/
        test_protocol_contract.py     ← tests/core/workspace/
    gateway/
      test_otel_fastapi.py
      test_otel_lifespan.py
      test_pending_response_endpoints.py
      test_realtime_main.py
      test_resolve_provider.py
      test_sdk_client_shutdown.py
      test_sse_events.py
      test_subagent_event_publisher.py
      realtime/
        test_app.py
        test_deps.py
        test_guardrails.py
        test_manager.py
        test_metrics.py
        test_routes_client_text.py
        test_session.py
        test_wire.py
      sse/
        test_attachments.py
        test_chat_title_route.py
        test_config_reload.py
        test_create_session_model.py
        test_gateway_runtime.py
        test_history_routes.py
        test_routes.py
        test_scheduler_routes.py
        test_session_bus.py
        test_start_turn_setup_failure.py
        test_transcript_wiring.py
    providers/
      conftest.py                     # keeps fixtures; builders now come from testkit.conversations
      test_anthropic_cache.py
      test_anthropic_history.py
      test_bedrock_converse.py
      test_bedrock_estimate.py
      test_connection_warm.py
      test_fireworks.py
      test_gemini_history.py
      test_gemini_live.py
      test_gemini_usage.py
      test_huggingface.py
      test_model_capabilities.py
      test_nvidia.py
      test_ollama.py
      test_openai_byte_budget.py
      test_openai_compat_usage.py
      test_openai_history.py
      test_pricing.py
      test_request_budget.py
      test_safe_parse_tool_args.py
      test_sampling.py
      test_sdk_client_reuse.py
      test_sonnet5_sampling.py
      test_source_grep_gates.py
      test_tiktoken_encoding.py
      test_vertex_claude_history.py
    observability/
      conftest.py
      test_init.py
      test_instrumentation.py
      test_loop_hook_spans.py
      test_propagation.py
      test_span_contract.py
      test_spans.py
      test_sqlite_exporter.py
      test_truncate.py
      test_turn_complete_trace.py
    computer/
      test_approvals.py
      test_computer_tools.py
      test_package_init.py
      test_path_policy.py
      test_permissions.py
      test_subagent_exclusion.py
      test_wiring.py
      test_platform_gate.py       +  (T1-COMP-01)
    scheduler/
      test_engine.py
      test_interval.py                # extended for T1-SCHED-01
    todo_list/
      test_todo_list.py
    web_search/
      test_web_search.py
    cli/
      test_gateway_manager.py
      test_main.py
      test_push_to_talk.py
      test_realtime_client.py
    skills/
      test_generate_image_script.py
  contracts/                      +
    conftest.py                   +   # marks t1; open_backend fixture added in P2-07 (options come from regression.plugin)
    test_sse_schema.py            +  (T1-GW-01..04)    # event_to_json over every AgentEvent variant
    test_route_table.py           +  (T1-GW-05, 06)    # route snapshot + error envelope
    test_tool_error_catalog.py    +  (T1-FILE-01, 02)
    test_policy_decision_table.py +  (T1-POL-01..03)
    test_provider_conformance.py  +  (T1-PROV-01..10)  # parametrized over every adapter
    test_storage_parity.py        +  (T1-STORE-01..14) # sqlite always; postgres/firestore when available
    test_migrations_replay.py     +  (T1-STORE-20..25)
    test_config_matrix.py         +  (T1-CFG-01..06)
    test_harness_prompt_golden.py +  (T1-CTX-01, 02)
    test_scaffold_reexports.py    +  (T1-OPS-01)
    test_span_names.py            +  (T1-OPS-02)
    test_realtime_divergence.py   +  (T1-RT-01)
    test_catalog_pairing.py       +   # docs/testing.md §8 <-> case ids in every test (5.10)
    tables/                       +   # inputs for table-driven contracts
      policy_decisions.yaml       +
      config_precedence.yaml      +
      provider_error_mapping.yaml +
    fixtures/                     +
      legacy_db/                  +
        pre_row_id.sqlite         +
        pre_agent_scope.sqlite    +
        pre_branch_pk.sqlite      +
        pre_usage_cache_cols.sqlite +
    goldens/                      +   # rewritten only with --update-golden (refused in CI)
      sse/
        schema.json               +   # the cross-repo contract monkeyapp vendors (section 6.U)
      prompts/
        harness_prompt.default.txt      +
        harness_prompt.coding.txt       +
        harness_prompt.memory_off.txt   +
        harness_prompt.subagent.txt     +
        harness_prompt.realtime.txt     +
        harness_prompt.routine.txt      +
        volatile_tail_parts.json        +
      routes.txt                  +
      tool_error_codes.txt        +
      span_names.txt              +
```

Placement rule for any new tier-1 test: code from one package goes in `tests/unit/<package path>/`; a table that crosses packages or adapters goes in `tests/contracts/`; anything that calls `loop.run` or the gateway with a scripted provider as its main subject goes in `regression/t2/`. Existing tests in `tests/unit/core/runtime/test_loop.py` that already drive `loop.run` stay where they are (non-goal: no rewrites); new cases of that shape go to T2.

## 4.4 `regression/` (tiers 2–5)

```text
regression/                       +
  plan/                               # this plan, split for agents (README.md is the entry point)
    README.md  AGENT_GUIDE.md  tasks.md  status.md
    phases/                           # one file per phase: summary + step-by-step tasks
    reference/                        # sections 1-12 + appendices; 06-coverage/ has one file per area
  conftest.py                     +   # marker gating (t4/t5 deselected without --live; darwin_only skip) and shared fixtures
  plugin.py                       +   # loaded by pytest.ini (-p regression.plugin): registers --live, --record, --update-golden,
                                      #   --trials, --suite, --agent-url once for every test root; collects scenario YAML as items
  schema/                         +
    scenario.py                   +   # one Scenario model, mode: scripted | replay | live (5.2)
    events.py                     +   # YAML event vocabulary -> ProviderEvent
    steps.py                      +   # mid-run actions: cancel, steer, confirm_tool, advance_clock, reconnect_sse...
    assertions.py                 +   ← merged from evals/assertions.py + tests/evals/scenario_runner.py (5.3)
    invariants.py                 +   # global invariants on every T2/T3 run (5.3.2)
  drivers/                        +
    loop.py                       +   ← tests/evals/scenario_runner.py (runner half)
    gateway.py                    +   # uses testkit.gateway + testkit.sse
    realtime.py                   +   # WebSocket driver over the realtime app with mocks_realtime_provider
    replay.py                     +   # feeds a T3a cassette into the loop or gateway driver
    record.py                     +   ← tests/evals/eval_hook.py (EvalRecord / EvalHook)
  fixtures/                       +
    conftest.py                   +   # collect_ignore_glob = ["workspaces/*"] (fixture repos hold failing tests on purpose; P4-01)
    build.py                      +   # builds generated fixtures (huge file, 301-entry dir, binary, symlinks) into tmp_path
    workspaces/
      calc_repo/                      ← evals/fixtures/coding/calc_repo/
      calc_repo_green/                ← evals/fixtures/coding/calc_repo_green/
      shop_repo/                      ← evals/fixtures/coding/shop_repo/
      overlays/                       ← evals/fixtures/coding/overlays/
      hostile_ws/                 +   # see 5.4
      grants_ws/                  +
      drift_ws/                   +
    configs/                      +
      agent.base.yaml             +   # base config for scripted drivers
      command_allowlist.default.yaml       +
      command_allowlist.strict.yaml        +
      command_allowlist.no_deny_patterns.yaml +
      command_allowlist.empty.yaml         +   # must raise
      permissions.default.yaml    +
      permissions.overlap.yaml    +   # last-match-wins case
      grants.json                 +
      grants.corrupt.json         +
      mcp.autoconnect.json        +
      mcp.progressive.json        +
      mcp.broken.json             +
      mcp.two_prefix.json         +
      verifier.tracker.yaml       +
      verifier.judge.yaml         +
      verifier.replan.yaml        +
      verifier.block.yaml         +
    mcp/
      fixture_mcp_server.py           ← evals/smoke_agent/fixture_mcp_server.py (shared by T2 and the T4 agent)
    skills/                       +
      diagnostics/SKILL.md        +
      plain/SKILL.md              +
      a/nested/SKILL.md           +   # must not be discovered
      escape                      +   # symlink leaving the skills root
    subagent_tapes/               +   # NDJSON tapes for testkit.subagent_worker
      ok.ndjson                   +
      hang.ndjson                 +
      exit_nonzero.ndjson         +
      huge_line.ndjson            +
      tries_task.ndjson           +
    scripts/                      +   # provider scripts for gateway driver (MONKEYBOT_FAKE_PROVIDER_SCRIPT)
  t2/                             +
    loop/
      scenarios/
        loop/
          turn_completion.yaml        ← tests/evals/scenarios/turn_completion.yaml (T2-LOOP-01)
          max_turns_cap.yaml          max_turns_not_on_clean_finish.yaml
          truncated_batch_rejected.yaml   all_parse_error_batch_rejected.yaml   mixed_parse_error_batch_runs_valid.yaml
          doom_identical_read.yaml    doom_path_retry_spelling.yaml   doom_path_reset_on_success.yaml
          doom_exempt_poll.yaml       doom_disabled.yaml
          empty_then_text.yaml        empty_exhausted.yaml            post_tool_empty_retry.yaml
          thinking_only_is_empty.yaml
          background_nudge_then_release.yaml   background_check_raises_fails_closed.yaml
          background_nudges_exhausted.yaml
          steer_mid_turn.yaml         promote_follow_up_provenance.yaml
          cancel_mid_stream.yaml      cancel_after_tool_call_before_execute.yaml
          confirm_approve.yaml        confirm_deny.yaml   confirm_timeout.yaml   confirm_always_remembered.yaml
          parallel_results_in_submission_order.yaml   parallel_concurrency_cap.yaml
          registry_refresh_same_turn.yaml
          repair_orphan_tool_response.yaml   repair_missing_tool_result.yaml
          ask_user_roundtrip.yaml     ask_user_non_interactive.yaml
          thinking_block_order.yaml   thought_signature_preserved.yaml
          usage_totals.yaml           provider_raises.yaml
          tool_input_delta_passthrough.yaml   grounding_event_passthrough.yaml
          resume_thinking_budget_routine.yaml                        (T2-LOOP-02..40, all +)
        ctx/
          compact_at_trigger.yaml     compact_pins_verdict_attachment_thinking.yaml
          compact_does_not_persist_repairs.yaml   compact_keep_tail_ratio.yaml   compact_fallback.yaml
          compact_trailer_from_goal_ledger.yaml   summarizer_model_override.yaml
          background_summary_applied.yaml   background_summary_stale_fallback.yaml
          epoch_volatile_update.yaml  epoch_stable_change.yaml   epoch_after_compaction.yaml
          stable_prefix_byte_stable.yaml   project_instructions_spoof_heading.yaml
          slash_skill_turn_scoped.yaml     tool_result_shaping_under_pressure.yaml
          base64_sanitized.yaml       context_usage_current_not_peak.yaml   (T2-CTX-01..18, +)
          memory_wake_up_large.yaml   ← tests/evals/scenarios/ (T2-CTX-19)
        hook/
          evidence_missing_path.yaml  evidence_unread_then_read.yaml
          evidence_grep_only_not_accepted.yaml   evidence_thread_isolation.yaml
          secret_redacted_interactive.yaml   secret_ends_routine_turn.yaml
          secret_scanner_failure_fails_closed.yaml   secret_in_tool_args.yaml   (T2-HOOK-08..15, +)
        file/
          tool_read.yaml              ← tests/evals/scenarios/ (T2-FILE-01)
          tool_write.yaml             ← tests/evals/scenarios/ (T2-FILE-08)
          tool_replace_apply_patch.yaml ← tests/evals/scenarios/ (T2-FILE-17)
          tool_glob_grep.yaml         ← tests/evals/scenarios/ (T2-FILE-33)
          read_paging_no_gap.yaml     read_offset_past_eof.yaml   read_escape_dotdot.yaml
          read_escape_symlink.yaml    read_absolute_needs_grant.yaml   read_huge_single_line.yaml
          write_new_without_read_ok.yaml   overwrite_unread_rejected.yaml   stale_read_rejected.yaml
          skills_read_only.yaml       write_outside_scope.yaml    write_payload_too_large.yaml
          replace_error_codes.yaml    replace_fuzzy_unicode.yaml
          patch_fail_closed.yaml      patch_rollback.yaml
          patch_add_existing.yaml     patch_move_dest_exists.yaml   patch_update_requires_read.yaml
          glob_skips_noise.yaml       glob_cap_incomplete.yaml
          grep_gitignore_default.yaml grep_incomplete_scan.yaml   grep_pagination.yaml   grep_invalid_regex.yaml
          list_dir_truncate.yaml      virtual_mounts.yaml
          spill_large_tool_output.yaml   spill_session_id_sanitized.yaml          (+)
        shell/
          run_command.yaml            ← tests/evals/scenarios/tool_run_command.yaml (T2-SHELL-01)
          argv_forms.yaml             allowlist_deny.yaml   shell_wrapper_bypass.yaml
          unscreenable_script_fails_closed.yaml   home_escape.yaml   cwd_escape.yaml
          nonzero_exit.yaml           timeout_partial_output.yaml   large_stdout_spill_keeps_exit.yaml
          bg_start_await_cursor.yaml  bg_job_cap.yaml   bg_kill_command.yaml   bg_killed_at_turn_end.yaml
          mempalace_search_only.yaml  mempalace_refused_memory_off.yaml   (T2-SHELL-02..16, +)
        policy/
          unknown_binary_confirm_approve.yaml   unknown_binary_confirm_deny.yaml
          default_deny_patterns.yaml  path_grant_read.yaml   granted_folder_credential_denied.yaml
          symlink_in_grant_does_not_widen.yaml   command_grant_from_store.yaml
          grant_revocation_clears.yaml   permissions_last_match_wins.yaml
          permissions_missing_file_fail_open.yaml   start_loop_always_confirms.yaml   (T2-POL-01..11, +)
        coding/
          read_before_write.yaml      ← tests/evals/scenarios/coding_read_before_write.yaml (T2-CODE-01)
          gate_rejects_unverified.yaml ← tests/evals/scenarios/coding_gate_rejects_unverified.yaml (T2-CODE-02)
          gate_passes_when_verified.yaml ← tests/evals/scenarios/coding_gate_passes_when_verified.yaml (T2-CODE-03)
          fix_failing_test.yaml       ← tests/evals/scenarios/coding_fix_failing_test.yaml (T2-CODE-04)
          gate_masked_exit.yaml       gate_non_executing_wrapper.yaml   gate_background_verify_not_counted.yaml
          gate_write_after_verify.yaml   gate_open_todos_reject.yaml   gate_question_not_gated.yaml
          gate_cap_then_accept.yaml   gate_custom_verify_commands.yaml   gate_rejected_answer_not_persisted.yaml
          git_context_note.yaml       git_context_absent.yaml   git_env_isolation.yaml
          compaction_forgets_reads.yaml   subagent_has_ledger_no_gate.yaml   (T2-CODE-05..18, +)
        attachments/
          load_image_preview.yaml     ← tests/evals/scenarios/tool_load_file.yaml (T2-ATT-01)
          load_pdf.yaml   load_text_rejected.yaml   load_both_args.yaml   load_per_reply_cap.yaml
          freeze_descriptors_in_history.yaml   attachment_symlink_leaf_rejected.yaml   (T2-ATT-02..07, +)
        computer/                         # tagged darwin-only
          open_exec_suffix_denied.yaml   denied_dirs_and_globs.yaml   denied_apps_and_schemes.yaml
          clipboard_cap.yaml          move_trash_not_always_scoped.yaml   trash_huge_tree_refused.yaml   (+)
        subagents/
          subagent_roundtrip.yaml     ← tests/evals/scenarios/ (T2-SUB-01)
          nested_task_refused.yaml    expect_files_present.yaml   expect_files_absent.yaml
          expect_files_escape.yaml    unknown_subagent_type.yaml   subagent_timeout.yaml   parent_cancel.yaml
          child_errors_record_failed.yaml   large_ndjson_line.yaml   system_prompt_redacted_on_pipe.yaml
          parallel_task_cap.yaml      persona_applied.yaml   child_memory_read_only.yaml
          queue_pending_not_success.yaml   ledger_context_forwarded.yaml   (T2-SUB-02..14, 17, +)
        verifier/
          write_without_read_positive.yaml   write_without_read_negative.yaml
          error_streak_positive.yaml  error_streak_negative.yaml
          rewrite_churn_positive.yaml
          constraint_touch_positive.yaml   constraint_touch_negative.yaml
          free_text_constraint_never_matches.yaml   done_unmet_positive.yaml   repeat_correction_positive.yaml
          preempt_negative.yaml       supersession_negative.yaml
          no_progress_budget_burn_not_mailboxed.yaml   min_turn_before_verdict.yaml
          severity_cap_nudge.yaml     replan_one_turn.yaml   block_denies_mutating_tools.yaml
          mailbox_stale_checkpoint_dropped.yaml   sticky_nudge.yaml
          verdict_in_history_stripped_for_provider.yaml
          judge_replay_valid.yaml     judge_malformed_fails_open.yaml   judge_spend_ratio.yaml
          judge_rate_limits.yaml      rewrite_effects_branch_copy.yaml   verifier_off_no_effect.yaml
                                                                  (T2-VER-01..22, +; port of tests/evals/test_verifier_port.py)
        scheduler/
          enable_loops_progressive.yaml   loop_lifecycle_real_store.yaml   start_loop_requires_stop_guard.yaml
          loop_goal_isolation.yaml    create_goal_requires_slash_goal.yaml   one_open_goal_per_session.yaml
          tool_loops.yaml             ← tests/evals/scenarios/tool_loops.yaml (T2-SCHED-11)
                                                                  (T2-SCHED-01..04, 09, 10, +)
        skills/
          list_then_read_skill.yaml   skill_write_denied.yaml   skill_symlink_escape.yaml
          skills_in_prompt.yaml       skills_path_hot_reload.yaml   slash_unknown_skill.yaml   (+)
        mcp/
          enable_then_call.yaml       disable_removes_tools.yaml   meta_tools_only_when_connected.yaml
          resources_and_prompts.yaml  autoconnect_failure_is_status.yaml   strict_load_rolls_back.yaml
          name_prefix_split.yaml      env_interpolation_missing.yaml   catalog_diff_reload.yaml
          enable_unknown_server.yaml  disabled_server_not_catalogued.yaml   (+)
        web/
          web_search.yaml             ← tests/evals/scenarios/tool_web_search.yaml (T2-WEB-01)
          empty_query.yaml   backend_none_not_advertised.yaml   max_results.yaml
          browser_stop_on_disconnect.yaml   (+)
        memory/
          memory_write_inject.yaml    ← tests/evals/scenarios/ (T2-MEM-08)
          mempalace_search_real.yaml  ← tests/evals/scenarios/tool_search_memory.yaml (T2-MEM-06)
          outbox_ingest_then_inject.yaml   outbox_thread_isolation.yaml
          outbox_failure_does_not_fail_turn.yaml   memory_off.yaml   subagent_no_ingest.yaml
          ephemeral_palace_shared_outbox_rejected.yaml   (+)
        store/
          compaction_reset_row_ids.yaml   usage_recorded_on_turn_complete.yaml
          transcript_convertible.yaml (+)
        todo/
          todo_list.yaml              ← tests/evals/scenarios/tool_todo_list.yaml (T2-TODO-01)
          todo_list_steps.yaml        ← tests/evals/scenarios/tool_todo_list_steps.yaml (T2-TODO-01)
          single_in_progress.yaml   caps.yaml   ids_stable.yaml   reminder_once.yaml
          mirror_hydrate.yaml (+)
    gateway/
      scenarios/
        gw/
          scripted_turn_golden_sse.yaml   tool_turn_golden_sse.yaml   setup_failure_releases_lock.yaml
          reply_busy_409.yaml         reconnect_last_event_id.yaml   session_isolation.yaml
          cancel_during_confirmation.yaml   steer_flow.yaml   queue_flow.yaml   ask_user_route.yaml
          elicitation_and_frontend_tool_routes.yaml   history_rewrite_order.yaml   branches_routes.yaml
          delete_session_quiesce.yaml   usage_routes.yaml   workspace_and_history_api_toggles.yaml
          chat_title_route.yaml       health.yaml   (T2-GW-01..05, 08..20, +)
        attachments/
          upload_then_reply_ref.yaml   upload_limits.yaml   (T2-ATT-G1, G2, +)
        scheduler/
          invoke_tick_busy_409.yaml    routes_lifecycle.yaml   (T2-SCHED-G1, G2, +)
        store/
          rewrite_regenerate_replays_tool_safe.yaml   (T2-STORE-G1, +)
        config/
          reload_hot_key.yaml   reload_restart_key_reported.yaml   reload_mid_turn_pinned.yaml
          reload_auth.yaml      bootstrap_memory_failure_continues.yaml   (T2-CFG-G1..G5, +)
    realtime/
      scenarios/
        ws_text_turn.yaml   ws_tool_turn.yaml   ws_tool_confirmation.yaml   ws_interrupt.yaml
        ws_slot_limit.yaml  ws_guardrail_budget.yaml   ws_blocks_history_rewrite.yaml   (T2-RT-01..07, +)
    python/                           # T2 cases too procedural for YAML
      test_hooks_lifecycle.py     +  (T2-HOOK-01..07)
      test_grep_parity.py         +  (T2-FILE-27)
      test_jail.py                +  (T2-SHELL-17)
      test_subagent_worker_pool.py +  (T2-SUB-15, 16)
      test_drift_confusion_matrix.py + (T2-VER-23)
      test_scheduler_engine.py    +  (T2-SCHED-05..08, uses testkit.clock)
      test_memory_commit.py       +  (T2-MEM-03, fault injection)
      test_sse_stream_pressure.py +  (T2-GW-06, 07, 21)
      test_ops_cli.py             +  (T2-OPS-01..03)
      test_ops_observability.py   +  (T2-OPS-04..06)
      test_e2e_simple_reply.py        ← tests/integration/test_mb_e2e_simple_reply.py
      test_observability_integration.py ← tests/integration/
      test_prompt_caching_integration.py ← tests/integration/
      test_observability_acceptance.py  ← tests/acceptance/
  t3/                             +
    tools/
      cassette.py                 +   # load/validate cassette models
      from_transcript.py          +   # transcript.ndjson -> T3a cassette
      record_wire.py              +   # MONKEYBOT_RECORD_CASSETTES=1 recorder
      check_staleness.py          +  (T3-PROV-08)
    trajectories/                     # T3a cassettes + one YAML each (mode: replay)
      loop/replay_live_doom_loop.{cassette.json,yaml}      +  (T3-LOOP-01)
      loop/replay_live_truncation.{cassette.json,yaml}     +  (T3-LOOP-02)
      ctx/replay_live_compaction.{cassette.json,yaml}      +  (T3-CTX-01)
      shell/replay_live_pytest_run.{cassette.json,yaml}    +  (T3-SHELL-01)
      verifier/replay_drift_tape.{cassette.json,yaml}      +  (T3-VER-01)
    wire/                             # T3b provider wire cassettes
      openai/tool_split_with_and_without_index.json        +  (T3-PROV-01)
      nvidia/hermes_tool_call_split.json                   +  (T3-PROV-02)
      anthropic/input_json_and_signature.json              +  (T3-PROV-03)
      gemini/partial_args_thought_signature_no_usage.json  +  (T3-PROV-04)
      bedrock_converse/tool_input_string_and_object.json   +  (T3-PROV-05)
      <family>/thinking_only_completion.json               +  (T3-PROV-06, one per family)
      ollama_cloud/glm_tool_turn.json                      +  (T3-PROV-07)
    sse/                              # T3c golden streams (schema lives in tests/contracts/goldens/sse/)
      basic_turn.sse              +   # T2-GW-01
      tool_turn.sse               +   # T2-GW-02
      cassette_turn.sse           +   # T3-GW-01
    mcp/tools_list_call.json      +  (T3-MCP-01)
    subagents/child_tape.ndjson   +  (T3-SUB-01)
    memory/wake_up_snapshot.json  +  (T3-MEM-01)
    test_replay_trajectories.py   +
    test_replay_wire.py           +
    test_golden_sse.py            +  (T3-GW-01)
    test_replay_mcp.py            +
    test_replay_subagent_tape.py  +
    test_replay_wake_up.py        +
  t4/                             +
    __main__.py                   +   # python -m regression.t4 (CLI entry used by CI)
    runner.py                         ← evals/runner.py
    scenario_loader.py                ← evals/scenario_loader.py (thin; parsing is in regression/schema)
    models.py                         ← evals/models.py
    persistence.py                    ← evals/persistence.py
    diff.py                           ← evals/diff.py
    score.py                          ← evals/scorer.py (LLM judge)
    report.py                         ← evals/report.py (also writes ledger records via LedgerWriter, P4-02)
    eval_config.yaml                  ← evals/eval_config.yaml (judge_provider: ollama-cloud, judge_model: glm-5.3-flash)
    preflight.py                  +   # OLLAMA_API_KEY present + one cheap stream call before any live suite (5.11)
    agent/                            ← evals/smoke_agent/
      pyproject.toml                  ← evals/smoke_agent/pyproject.toml
      uv.lock                         ← evals/smoke_agent/uv.lock
      monkeybot_config/
        AGENT.md                      ← evals/smoke_agent/monkeybot_config/
        AGENT.coding.md               ←
        monkeybot.yaml                ←
        monkeybot.coding.yaml         ←
        monkeybot.verifier-tracker.yaml ← split from monkeybot.verifier-on.yaml (judge off)
        monkeybot.verifier-judge.yaml +   # judge on, max_severity: nudge
        command_allowlist.yaml        ←
        permissions.yaml              ←
        mcp.json                      ←   # points at regression/fixtures/mcp/fixture_mcp_server.py
        mcp.progressive.json          +   # for T4-MCP-04
      skills/browser/                 ← evals/smoke_agent/skills/browser/
      .env                            # optional, gitignored: OLLAMA_API_KEY if not exported in the shell
      data/                           # gitignored (monkeybot.db)
      workspace/                      # gitignored
    suites/
      smoke.yaml                      ← evals/suites/ (adds file/core_read_error)
      coding.yaml                     ←
      coding_hard.yaml                ←
      drift.yaml                      ←
      context.yaml                +
      memory.yaml                 +
      mcp.yaml                    +
      scheduler.yaml              +
      subagents.yaml              +
      nightly.yaml                +   # union of the above for the nightly job
    scenarios/
      file/
        core_read.yaml                ← evals/scenarios/tools/ (T4-FILE-01)
        core_read_error.yaml          ← evals/scenarios/tools/ (T4-FILE-02)
        core_write.yaml               ← evals/scenarios/tools/ (T4-FILE-03)
        grep_absence_honesty.yaml +  (T4-FILE-04)
      shell/
        core_run_command.yaml         ← evals/scenarios/tools/ (T4-SHELL-01)
        core_run_command_sandbox.yaml ← evals/scenarios/tools/ (T4-SHELL-02)
      coding/
        add_feature_with_tests.yaml   ← evals/scenarios/coding/
        fix_failing_test.yaml         ←
        navigate_repo.yaml            ←                        (T4-CODE-01..03)
      coding_hard/
        add_bulk_discount.yaml        ← evals/scenarios/coding_hard/
        fix_discount_rounding.yaml    ←
        honest_unfixable_failure.yaml ←
        multi_feature_long.yaml       ←
        navigate_intl_rate.yaml       ←
        noisy_test_failure.yaml       ←
        rename_calc_tax.yaml          ←                        (T4-CODE-04..10)
      context/
        summarization_trigger.yaml    ← evals/scenarios/context/ (T4-CTX-01, retuned)
        post_compaction_continuity.yaml + (T4-CTX-02)
      drift/
        declares_done.yaml            ← evals/scenarios/drift/
        path_constraint.yaml          ←
        preempt.yaml                  ←
        repeat_correction.yaml        ←
        supersession.yaml             ←                        (T4-VER-01..05)
      hooks/
        evidence_citation_live.yaml   +  (T4-HOOK-01)
      attachments/
        describe_image_live.yaml      +  (T4-ATT-01, optional)
      subagents/
        dispatch_complete.yaml        ← evals/scenarios/subagents/ (T4-SUB-01)
        persona_dispatch.yaml         +  (T4-SUB-02)
      scheduler/
        http_one_tick_live.yaml       +  (T4-SCHED-01)
        model_starts_loop.yaml        +  (T4-SCHED-02)
      skills/
        skill_invocation.yaml         ← evals/scenarios/skills/ (T4-SKILL-01, extended)
      mcp/
        get_prompt.yaml               ← evals/scenarios/mcp/
        list_resources.yaml           ←
        tool_invoke.yaml              ←                        (T4-MCP-01..03)
        enable_then_echo_live.yaml    +  (T4-MCP-04)
      memory/
        recall_single_session.yaml    ← evals/scenarios/memory/
        recall_cross_session.yaml     ←                        (T4-MEM-01..02)
        search_past_session.yaml      +  (T4-MEM-03)
      config/
        reload_memory_off_live.yaml   +  (T4-CFG-01)
      multi_turn/
        task_tracking.yaml            ← evals/scenarios/multi_turn/ (T4-TODO-01)
      gateway/
        live_resume.yaml              +  (T4-GW-02; T4-GW-01 is a global live invariant, not a file)
    python/                           # live cases that are not single conversations
      test_provider_live_matrix.py +  (T4-PROV-01; Ollama Cloud preflight, other providers only if their key is set)
      test_docker_image_health.py +  (T4-OPS-01, manual/nightly)
    baselines/
      smoke.json                      ← evals/baselines/smoke.json (T4 report token/latency baseline, not a T5 baseline)
      smoke.md                        ← evals/baselines/smoke.md
    runs/                             # gitignored (run JSON + scorecards)
  t5/                             +
    __main__.py                   +   # python -m regression.t5 --suite <t4 suite> --trials N [--update-baseline]
    trials.py                         ← evals/trials.py
    stats.py                      +   # Wilson interval, pass^N, verdicts (5.8)
    compare.py                    +   # baseline lookup by (t4 suite[@config], provider, model)
    suites/                       +   # class lists, not runnable suites: reliability / capability per scenario, plus T5 case IDs
      reliability.yaml            +   # T5-SKILL-01, T5-TODO-01, T5-SUB-01
      capability.yaml             +   # T5-CTX-01, T5-CODE-01, T5-SCHED-01, T5-MCP-01, T5-MEM-01
      drift.yaml                  +   # T5-VER-01, per verifier config
    baselines/                    +   # written only by --update-baseline (P5-02)
      <t4 suite>[@<config>]__ollama-cloud__glm-5.3-flash.json   # e.g. coding_hard__…, drift@verifier-judge__…
    python/
      test_cancel_storm.py        +  (T5-GW-01)
      test_reconnect_storm.py     +  (T5-GW-02)
  selftest/                       +   # tests of the regression machinery itself (marked t1, run with tests/)
    test_scenario_schema.py       +
    test_assertions.py                ← evals/test_assertions.py
    test_invariants.py            +
    test_steps.py                 +   # one tiny scenario per steps: action
    test_fixtures.py              +   # each 5.4 fixture has the property it exists for
    test_report.py                    ← evals/test_report.py
    test_runner_sse.py                ← evals/test_runner_sse.py
    test_diff.py                      ← evals/test_diff.py
    test_scorer.py                    ← evals/test_scorer.py
    test_judge.py                     ← evals/test_judge.py (marked live: calls the judge model)
    test_coding_hard_oracle.py        ← evals/test_coding_hard_oracle.py
    test_from_transcript.py       +
    test_trials_stats.py          +
```

`pytest.ini` gets `testpaths = tests regression`. T4/T5 are never collected unless `--live` is passed. YAML files listed on one line above are separate files; they are grouped only to keep the tree readable.

## 4.5 Removed after cutover (Phase 6)

| Path | Why it can go |
|---|---|
| `tests/core/` | Every file re-homed under `tests/unit/core/`; helper modules `memory/helpers.py`, `memory/in_memory_palace.py` moved to `testkit/`. |
| `tests/gateway/`, `tests/providers/`, `tests/observability/`, `tests/computer/`, `tests/scheduler/`, `tests/todo_list/`, `tests/web_search/`, `tests/cli/`, `tests/skills/` | Moved one level down to `tests/unit/`. |
| `tests/test_cold_start.py`, `tests/test_packaging_config.py`, `tests/test_release_script.py` | Moved to `tests/unit/packaging/`. |
| `tests/evals/` | Scenarios to `regression/t2/loop/scenarios/`, runner to `regression/drivers/`, verifier port to T2-VER YAML. |
| `tests/integration/`, `tests/acceptance/` | Moved to `regression/t2/python/`; the `integration` and `acceptance` markers become `t2`. |
| `tests/profiling/`, `tests/gateway/sse/fixtures/` | Empty except `__pycache__`. |
| `evals/` (incl. `knowledge_layer/`, `monkeybot_evals.egg-info/`, `TEST_COVERAGE.md`, `runs/`) | Code to `regression/t4` and `regression/t5`; the coverage doc is replaced by section 6 of this plan. |

## 4.6 Config and script changes that make the move work

- `pytest.ini`: `testpaths = tests regression`; `addopts = -v -p testkit.failure_ledger -p pytester` (P0A-01), plus `-p regression.plugin` (P0B-04); keep `pythonpath = .` (this is what makes `testkit` and `regression` importable); register markers `t1`..`t5`, `live`, `needs_postgres`, `needs_firestore`, `darwin_only`, `slow`, `quarantine`, `case`; drop `integration`, `acceptance`. No conftest except `tests/conftest.py` defines `pytest_plugins` (pytest 9 rejects it in nested conftests).
- Every test folder gets an `__init__.py`. This matters because the move creates duplicate basenames (`tests/unit/core/llm/test_pricing.py` and `tests/unit/providers/test_pricing.py`); with packages, pytest's default import mode handles them. The alternative is `--import-mode=importlib`, which breaks the current `from tests...` imports while they still exist, so packages win.
- `tests/unit/conftest.py` and `tests/contracts/conftest.py` mark their items `t1`; `regression/selftest` items are also `t1`.
- `pyproject.toml`: add `files = ["src/monkeybot", "testkit"]` to `[tool.mypy]` and `testkit`, `regression` to ruff's isort `known-first-party` (there are no path lists today); the `evals` optional-dependency extra (deepeval, langfuse, google-genai) stays, used by `regression/t4/score.py`. `evals/monkeybot_evals.egg-info` is a stale build artifact and is deleted with `evals/`.
- `.pre-commit-config.yaml`: the `pytest` hook's `files:` regex becomes `^(src/monkeybot|testkit|tests|regression)/.*\.py$`. `scripts/precommit-pytest.sh` itself runs changed test files by path, so it needs no mapping change; its collection-only fallback becomes `uv run pytest tests regression --co -q`.
- `scripts/ci-local.sh`: `uv run pytest tests/` becomes `uv run pytest tests regression -m "t1 or t2 or t3" -q -n auto`. It exports `MONKEYBOT_TEST_RUN_GROUP` and `MONKEYBOT_TEST_TRIGGER` once at the top, and runs the `cli/` and `integrations/browser-mcp/` suites with `-p testkit.failure_ledger --ledger-dir $ROOT/test-runs` and the repo root on `PYTHONPATH`.
- `.gitignore`: add `/test-runs/`, `/regression/t4/runs/`, `/regression/t4/agent/.env`, `/.cursor/hooks/.state/`; drop the `/evals/...` entries at cutover.
- `.cursor/hooks.json`: add `postToolUse` → `.cursor/hooks/test-failure-notice.py` with matcher `Shell`, no `failClosed`.
- `.github/workflows/live-eval-smoke.yml`: paths move to `regression/t4/agent`. No provider secret (D1). The smoke suite runs locally (P4-06).
- `.github/workflows/live-trials.yml`: not added. The release-gate compare runs locally (P5-03, D1).
- `.github/workflows/ci.yml`: the same pytest command as `ci-local.sh` (P0A-05), the `evals/test_*.py` lines removed (P4-01), and the `cassette-staleness` step (P3-03).
- `docs/live-evals.md`: paths updated to `regression/t4`.
