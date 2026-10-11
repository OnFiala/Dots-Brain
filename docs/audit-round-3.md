# Audit round 3 disposition ledger

This is the implementation record for PR #1, branch `codex/shared-memory-safety`,
against exact baseline `555f615b615757a5bb111ee99d0b8f92d0de40ff`.
It contains one row for each of the 21 R3 findings and each of the 21 previously
open, partial or side-effect RV findings. It is newly authored from implementation
and verification evidence; supplied reports and their corpora are not included.

`FIXED` means the stated source behavior was corrected and checked within the
listed boundary. `PARTIAL` means a material reported limitation remains.
`OPEN` means no accepted correction was delivered. `CLEANUP` has source-diff
evidence rather than a behavioral red test. These are source dispositions,
not production deployment or merge approval.

All named regression functions are in the linked test modules below. A baseline
failure can expose a missing new API/format; the package evidence separately
identifies behavioral failures, controls and structural acceptance checks.
Rows with no applicable red test say so explicitly. In particular, the RV-07
boundary control is not claimed as a fix and the rejected RV-35 experiment is
not claimed as a passing regression.

## Findings

| ID | Disposition | Commit | Test that fails on 555f615 | Remaining boundary / reason |
| --- | --- | --- | --- | --- |
| R3-01 | FIXED | `9420d99` | [test_category_scans_bound_prefix_runs_and_repeated_units](../tests/test_privacy_categories.py) | Category scans are bounded at the enforced 32,000-character input limit; generated prefix/run/repetition tests include the former quadratic prose case. |
| R3-02 | FIXED | `800eeda` | [test_actual_7085841_touch_can_upgrade_without_changing_canonical_rows](../tests/test_round3_migration.py) | Authentic synthetic v1/v2 fixtures touched by 7085841 migrate. No production store was opened or migrated. |
| R3-03 | FIXED | `f34b90e` | [test_socket_forwarded_headers_cannot_reset_onboarding_limiter](../tests/test_round3_oauth_http.py) | Caller-controlled forwarding headers no longer select a limiter peer. A shared proxy peer still has the RV-13 limitation. |
| R3-04 | FIXED | `f34b90e` | [test_one_service_two_sockets_keep_static_private_and_share_oauth_store](../tests/test_round3_oauth_http.py) | Private listener accepts static tokens and OAuth; public listener accepts OAuth only. Operators must configure the gateway to reach the public listener. |
| R3-05 | PARTIAL | `9420d99` | [test_complete_placeholders_references_and_noncredential_flags_are_retained](../tests/test_privacy_categories.py); [test_store_warning_preserves_exact_content_digest_revisions_and_replay](../tests/test_privacy_warnings.py) | Category design, linear scans and uncertainty warnings replaced corpus-specific matching. Independent labels still disagree: one high-confidence URI case is accepted and some ambiguous inputs lack a warning or are rejected. Heuristic detection is incomplete. |
| R3-06 | FIXED | `956084a` | [test_status_aggregates_structure_without_reading_numeric_vectors](../tests/test_round3_semantic.py) | Status uses SQL aggregates without Python vector materialization. It still examines SQL rows and does not prove numeric finiteness. |
| R3-07 | FIXED | `956084a` | [test_sdk_loopback_oversize_does_not_erase_concurrent_committed_write_results](../tests/test_round3_bridge.py); [test_shared_loss_marks_only_sent_writes_unknown_with_stable_reference](../tests/test_round3_bridge.py) | Request-local failures preserve sibling outcomes. Shared-session loss can leave already-sent writes unknown; they are not automatically replayed. |
| R3-08 | FIXED | `e0d933a` | [test_accepted_http_event_progresses_then_remains_a_valid_review_anchor](../tests/test_round3_audit_review.py) | Bounded review projections advance across accepted large events. Original hash verification occurs in the trusted owner CLI; omitted content is an explicit finding, not locally reviewed content. Host-side historical-record processing is not universally memory bounded. |
| R3-09 | FIXED | `f34b90e` | [test_corrupt_config_diagnosis_and_cli_recovery_preserve_state](../tests/test_round3_oauth_http.py); [test_recover_publication_crash_selects_db_commit_and_preserves_prior_grant](../tests/test_round3_oauth_state.py) | Invalid configuration is unhealthy and recoverable. Ambiguous/corrupt journals require explicit owner replacement and grant revocation. |
| R3-10 | FIXED | `dd99257` | [test_calibration_fixture_is_the_reviewed_neutral_synthetic_dataset](../tests/test_calibration_fixture.py) | Current fixture is independently authored neutral synthetic data with pinned provenance/hash. Earlier public history remains unchanged by owner instruction. |
| R3-11 | FIXED | `60aaa89` | [test_uninstall_absent_installation_leaves_directory_untouched_and_allows_initialize](../tests/test_round3_removal.py); [test_deleted_installation_does_not_claim_a_living_server_was_stopped](../tests/test_round3_removal.py) | Absent/empty installations gain no disabling files. Missing evidence yields unknown, never a claimed successful stop. |
| R3-12 | FIXED | `60aaa89` | [test_unrelated_default_config_cannot_turn_owned_removal_into_partial](../tests/test_round3_removal.py) | Disconnect is scoped to the requested provider. Unrelated discovery is informational; unreadable or changed owned configuration remains a material partial result. |
| R3-13 | FIXED | `800eeda` | [test_plan_and_apply_both_reject_canonical_corruption_without_outputs](../tests/test_round3_migration.py); [test_corrupt_plan_names_recovery_command_and_valid_backup_copy_can_upgrade](../tests/test_round3_migration.py) | Planning validates canonical integrity and provides a recovery route. Apply rechecks because the store may change after planning. Corrupt source requires a valid backup or reviewed offline repair. |
| R3-14 | FIXED | `800eeda` | [test_rejected_store_preserves_main_and_wal_after_abnormal_writer_close](../tests/test_round3_migration.py) | Version/schema refusal preserves main database and committed WAL bytes. SQLite read-only access can still create empty coordination files. |
| R3-15 | FIXED | `60aaa89` | [test_inspection_cannot_reject_a_shared_writer_during_descriptor_close](../tests/test_round3_runtime.py); [test_unavailable_kernel_inspection_is_unknown_and_never_takes_a_lock](../tests/test_round3_runtime.py) | Inspection reads kernel lock evidence without acquiring the contested lease. Linux /proc supports holder evidence; unavailable inspection is explicitly unknown. |
| R3-16 | FIXED | `800eeda`; `054b098` | [test_failed_migration_removes_only_its_new_backup_and_partial_outputs](../tests/test_round3_migration.py); [test_backup_fsync_failure_removes_only_own_published_output](../tests/test_round3_migration.py) | Ordinary failed attempts remove only outputs they can prove they own. Explicit backups, uncertain commits and force-kill leftovers are preserved; disk requirements and reviewed cleanup of potentially stale deleted terms are documented. |
| R3-17 | FIXED | `956084a` | [test_fulltext_keeps_unicode_tokenizer_word_boundaries](../tests/test_round3_retrieval.py) | Query normalization now matches SQLite tokenizer word boundaries for the covered Unicode cases. |
| R3-18 | FIXED | `956084a` | [test_forbidden_write_is_independent_of_other_callers_discovery](../tests/test_round3_bridge.py) | Authorization uses the current caller scope independently of shared SDK discovery cache ordering. |
| R3-19 | FIXED | `f34b90e` | [test_registration_capacity_is_bounded_and_expiry_cleanup_frees_slot](../tests/test_round3_oauth_state.py) | Unapproved registrations are capped. Grant provenance remains retained; this is not a total historical-client storage quota. |
| R3-20 | FIXED | `60aaa89`; `3ad2a14`; `02c0028` | [test_parser_diagnostic_names_known_option_without_echoing_value](../tests/test_round3_operator.py); [test_disabled_store_names_resume_command_and_can_resume](../tests/test_round3_operator.py); [test_corrupt_managed_credential_has_state_error_and_preserving_replacement](../tests/test_round3_structure_runtime.py) | State/capability/busy errors identify executable diagnostics or recovery. Historical backup copy-upgrade and client registry/credential recovery preserve originals. Production operator recovery was not executed. |
| R3-21 | CLEANUP | `f34b90e` | NONE: unreachable-branch and stale-text removal | Diff inspection verifies removal. Live pairing behavior is covered by browser tests; there is no separate meaningful behavioral test that turns red solely because dead code is restored. |
| RV-01 | FIXED | `f34b90e` | [test_chromium_pairing_form_reaches_cross_origin_loopback_callback](../tests/test_oauth_browser.py) | IPv6 callback and delayed submission variants fail on 555f615 and pass in actual Chromium. Other browser engines were not tested. |
| RV-02 | FIXED | `800eeda` | [test_actual_7085841_touch_can_upgrade_without_changing_canonical_rows](../tests/test_round3_migration.py) | Same migration boundary as R3-02; source/fixture evidence does not authorize production migration. |
| RV-04 | FIXED | `60aaa89` | [test_supported_legacy_managed_versions_keep_exact_identity_validation](../tests/test_round3_runtime.py) | Linux detection covers historical module/console/PATH and equals-option forms with stable process evidence. Unmanaged writers are reported and block maintenance, never silently adopted or killed. Other platforms can remain unknown. |
| RV-06 | FIXED | `9420d99` | [test_category_scans_bound_prefix_runs_and_repeated_units](../tests/test_privacy_categories.py) | Same bounded timing regression as R3-01; no guarantee for inputs outside enforced size limits. |
| RV-07 | PARTIAL | `e0d933a` | NONE for cross-source suppression; test_forget_suppresses_same_identity_but_other_capture_scope_can_import is a passing boundary control | Same-source deletion protection and cursor upgrade are tested. Different path/account/project can still reimport equivalent content. This limitation is restored explicitly in docs/activity.md; global content suppression was not introduced. |
| RV-08 | FIXED | `956084a`; `054b098` | [test_long_task_keeps_short_boundary_terms_and_reports_omissions](../tests/test_round3_retrieval.py) | First/last 16 unique terms retain short identifiers and expose total/used/omitted counts. Middle terms may still be omitted; this is documented. |
| RV-11 | FIXED | `e0d933a` | [test_accepted_http_event_progresses_then_remains_a_valid_review_anchor](../tests/test_round3_audit_review.py); [test_unsupported_or_changed_projection_cannot_advance_checkpoint](../tests/test_round3_audit_review.py) | Trusted owner CLI, supported projection format and intact chain/anchor are required. Explicit omissions do not assert content review. |
| RV-13 | PARTIAL | `f34b90e` | [test_socket_forwarded_headers_cannot_reset_onboarding_limiter](../tests/test_round3_oauth_http.py) | Header spoofing is fixed, but clients behind one reverse-proxy peer still share its bucket. No trusted external-client address propagation was introduced. |
| RV-14 | FIXED | `f34b90e` | [test_socket_forwarded_headers_cannot_reset_onboarding_limiter](../tests/test_round3_oauth_http.py) | Limiter identity comes from the socket peer; user forwarding headers are not trusted. |
| RV-15 | FIXED | `f34b90e` | [test_one_service_two_sockets_keep_static_private_and_share_oauth_store](../tests/test_round3_oauth_http.py) | Listener configuration controls access policy, never request headers; public deployment remains an operator action. |
| RV-16 | FIXED | `f34b90e` | [test_corrupt_configuration_is_invalid_and_read_only](../tests/test_round3_oauth_state.py); [test_recover_publication_crash_selects_db_commit_and_preserves_prior_grant](../tests/test_round3_oauth_state.py) | Supported interrupted publication recovers automatically; ambiguous evidence stays preserved for explicit recovery. |
| RV-18 | FIXED | `f34b90e` | [test_purge_preserves_legitimate_pending_client_and_authorization](../tests/test_round3_oauth_state.py) | Purge preserves live pending authorization/code clients and registration has a cap. The shared-proxy rate limit in RV-13 remains. |
| RV-19 | FIXED (bounded) | `956084a` | [test_uuid_removal_does_not_walk_unrelated_fulltext_rows](../tests/test_round3_retrieval.py) | Canonical UUID deletion uses indexed exact identity; unusual historical IDs use a safe linear fallback. Schema v4 adds FTS disk cost and slightly changes hybrid ranking; measurements are published. |
| RV-24 | FIXED | `956084a`; `054b098` | [test_sdk_loopback_oversize_does_not_erase_concurrent_committed_write_results](../tests/test_round3_bridge.py); [test_absolute_deadline_includes_local_budget_check](../tests/test_round3_bridge.py) | Eight in-flight requests/128 pending slots remain deliberate limits. Await a write before a causally dependent read; blocked calls can occupy all slots. |
| RV-25 | FIXED | `956084a` | [test_failed_backlog_has_global_retry_rate_and_new_source_priority](../tests/test_round3_semantic.py); [test_cli_index_finishes_repair_pass_beyond_first_valid_slice](../tests/test_round3_semantic.py) | Automatic backoff is process-local and resets on restart. Explicit --retry-failed resets it; bounded background repair takes time to traverse large stores. |
| RV-26 | FIXED | `956084a`; `054b098` | [test_existing_receipt_blocks_write_without_replaying_or_overwriting](../tests/test_round3_bridge.py); [test_probe_cli_refusal_has_recover_then_explicit_archive_path](../tests/test_round3_cli_recovery.py) | Recovery validates observed target identity before deletion. Explicit archive preserves evidence and can leave an unknown probe target. Successful probes still create deliberate audit/suppression records. |
| RV-27 | FIXED | `60aaa89` | [test_startup_failure_surfaces_lease_evidence_without_reflecting_logs](../tests/test_round3_runtime.py); [test_readiness_failure_without_lease_evidence_names_safe_local_diagnostics](../tests/test_round3_runtime.py) | Startup returns observed lease/PID evidence where available; otherwise it names scoped doctor/log commands without reflecting sensitive raw log contents. |
| RV-33 | PARTIAL | `9420d99` | [test_complete_placeholders_references_and_noncredential_flags_are_retained](../tests/test_privacy_categories.py); [test_store_warning_preserves_exact_content_digest_revisions_and_replay](../tests/test_privacy_warnings.py) | Same independent-classification limitations as R3-05. A privacy heuristic cannot establish absence of all secrets. |
| RV-34 | FIXED | `054b098` | [test_typed_errors_have_reference_and_actionable_recovery](../tests/test_documentation_round3.py) | Error codes and recovery are documented. Tests check all requested codes against current runtime classes and reference text. |
| RV-35 | OPEN | `054b098 (evidence only)` | NONE: proposed policy failed independent one-word acceptance | Unchanged floor 0.20. Proposed length-dependent floor returned candidates for 24/24 unrelated single words and retained only 17/24 relevant cross-language queries, identical to 555f615. Candidate was rejected; no query-driven retuning or corpus edit followed. |
| RV-37 | FIXED | `f34b90e` | [test_chromium_pairing_form_reaches_cross_origin_loopback_callback](../tests/test_oauth_browser.py) | The delayed double-submit browser variant fails on 555f615 and passes after the fix. Approval remains single-use. |

## Package evidence and reversals

Each package was frozen independently. Negative checks ran on exact baseline
source and a candidate with only that package's source changes reversed. W1
was directly based on 555f615, so its source reversal is also the exact baseline
source; the candidate tests and documentation remain present. The working branch was not reset. Expected negative results are retained
as evidence, not suppressed test failures. No supplied audit data was committed.

| Package | Commit | Candidate evidence | Negative control |
| --- | --- | --- | --- |
| W1 privacy | `9420d99` | Generated category timing and private sealed classification corpus; uncertainty returned through actual write paths | Selected reversal: 17 fail / 5 controls pass; legacy behavior and missing new API failures distinguished |
| W2 migrations | `800eeda` | Historical code produced v1/v2 touched fixtures; plan/apply, WAL custody and recovery checks | Exact baseline and source reversal: 20 fail / 3 controls pass |
| W8a neutral fixture | `dd99257` | Neutral fixture provenance/hash, current and baseline retrieval measured on the same machine | Fixture guard fails baseline and reversal |
| W3 OAuth | `f34b90e` | Actual dual sockets, Chromium IPv4/IPv6/delayed submit, journal recovery and scoped security mutation coverage | Source reversal: 34 fail / 38 controls pass; baseline Chromium: 2 fail / 1 pass |
| W4 memory/bridge | `956084a` | SDK loopback, schema v4 and five historical fixtures, 50k/100k reproducible storage measurements | Exact baseline and source reversal: 74 fail / 4 controls pass; scoped bridge mutations 24/24 caught |
| W5 reviewer/capture | `e0d933a` | Large-event projection/anchor, format upgrades and malformed-state recovery | Exact baseline and source reversal: 86 fail / 6 controls pass |
| W6 lifecycle | `60aaa89` | Linux historical-process detection, lock lifetime, removal and actual recovery commands | Baseline: 70 fail / 10 controls / 9 platform skips; source reversal: 71 fail / 10 controls / 8 skips; stripping serve locks turns the lease test red |
| W7 relevance | No adopted source change | Separately sealed main and one-word corpora; candidate frozen before evaluation | Candidate failed predeclared one-word unrelated-candidate gate; see [retrieval evaluation](using-memory.md#independent-threshold-evaluation) |
| W8b documentation | `054b098` | 148 targeted checks plus 6 documented-command parser checks; wheel/sdist and project checks | New docs tests: baseline and old-doc reversal 37 fail; pre-W8b docs reversal 32 fail / 5 controls |
| W9 structure/taxonomy | `3ad2a14` + `02c0028` | Scoped independent review, behavior comparisons, real Chromium and installed wheel; see below | Final 44 new cases: baseline 35 fail / 9 controls; source reversal 32 fail / 12 controls. Three extra baseline failures are earlier W1 warnings, not W9 corrections |

W9 splits the five requested entry points into smaller responsibilities, uses a
typed CORTEX receipt in the actual write path, and classifies runtime/client
state failures separately from invalid input. It preserves serialized formats;
no additional disk-format revision was needed. Intentional behavior changes are
the error taxonomy/recovery and closing a capture descriptor when rewrite-barrier
acknowledgement fails. Structural checks are explicitly structural; they are not
presented as independent behavioral bug reproductions.

Independent W9 checks included 500 ordered sanitizer comparisons, two ordered
capture checkpoint/event scenarios and 360 ordered receipt comparisons across
the implementation and review evidence. These finite checks support the exercised
cases, not universal equivalence. Main ran the complete initial frozen suite:
1,038 passed / 27 skipped. A corrected historical-source environment then passed
all 38 historical tests, including six initially skipped. The bounded final
review repair passed 83 relevant tests / 10 platform skips. Actual Chromium passed
all three cases; the isolated installed wheel passed five historical fixture
migrations, retained grants, CRUD, suppression and restore. Initial test-harness
mistakes and reviewer findings were retained with their corrections.

### CI regression and correction

The requirement that every intermediate commit be green was not fully met:
`3ad2a14` failed [CI 38134780795](https://github.com/OnFiala/Dots-Brain/actions/runs/38134780795).
The final taxonomy repair changed a registry error to `StateError`, but removal's
caller still caught only `InputError`. Each Linux version reported 1,065 passed,
one failed and three skipped. Main reproduced the same failure locally. The
targeted pre-commit repair checks had omitted this existing removal test.

`02c0028` extends only that registry catch and expands the existing
uninstall preservation/disable/revocation test to malformed JSON, non-object JSON,
invalid UTF-8 and an unsupported registry version. All four cases fail after
reversing this correction and pass on the original `555f615`: this fixes an
introduced W9 regression, so it is not represented as a new baseline defect.
The initial failure remains in history and in retained evidence; history was not
rewritten to hide it. The complete corrected local suite passed 1,052 tests with
21 platform/browser skips. Ruff, project validation and wheel/sdist build passed.

The corrected source commit `02c0028` passed
[Linux CI 38135232863](https://github.com/OnFiala/Dots-Brain/actions/runs/38135232863):
Python 3.11, 3.12 and 3.13 each passed 1,069 tests with three skips, plus the
separate managed/direct latency check. Chromium passed three cases; the pinned
model job passed 39 tests; gateway validation and installed-wheel acceptance
passed. Local checks used a MacBook Pro, macOS 26.6.2 arm64 and Python 3.11.14;
CI used GitHub's Ubuntu runners. Passing source checks do not establish any live
production state.

## Reproduction and scope

The shipped tests and `scripts/benchmark_storage.py` are the reproducible public
artifacts. [Stress measurements](stress-tests.md) give machine, baseline, sample
counts and measured costs; no performance claim here is inferred from fewer
lines. CI exports authentic historical sources and tests the built wheel outside
the source checkout. The pinned real-model and Chromium checks run separately.

Private verification records retain immutable tree/file hashes, raw test output,
baseline/reversal output, scoped mutation results and independent reviewer reports.
Sealed private corpora remain outside the repository. Reviewers were procedurally
read-only and before/after hashes matched; technical read-only enforcement and
backend model identity were not independently established.

Version is `0.4.0a4`, database schema is v4, capture cursor and reviewer checkpoint
formats are version 1 with legacy upgrade tests. Support for unregistered
`0.2.0-alpha.1` client entries ended by owner decision; the file is named with
manual removal/reconnect instructions. Migration from supported 0.3+ stores remains.

No Dots Brain production host/store was accessed, changed or migrated in this round. No
merge, release, tag or history rewrite was performed. Source rollback is a new
revert commit; database rollback requires the retained pre-upgrade snapshot and
the deletion-preserving procedure in [upgrading](upgrading.md). Source CI does not
prove deployment identity, production backup validity or real client acceptance.

Remaining material issues are R3-05/RV-33 classification, RV-07 cross-source
reimport, RV-13 shared-proxy limiting and RV-35 one-word relevance. The three
original stop-merge prerequisites have source corrections and regression evidence;
this ledger does not grant approval to merge or migrate production. Whether
7085841 previously touched a real store remains unconfirmed; resolve that owner
input before planning a production upgrade.
