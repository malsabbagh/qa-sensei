# QA pilot seam

This component owns acceptance execution and evidence, independently from product
implementation and ReviewSensei assessment. The selected placement is a standalone
stdlib Python CLI in this QA workspace. An alternative was importing TaskAlong
internals for process execution. That would couple QA to the implementation
controller and its private state; the small local process runner keeps the QA
oracle independently executable. No provider supervisor is added.

`plan` binds a reviewed CheckSpec, clean Git candidate, fixture content and local
executable. `run` requires the exact reviewed plan file SHA-256 and a new evidence
directory. `report` inspects existing evidence without executing checks. `gate`
translates verified QA acceptance into a CI exit code. Protocol completion and QA
acceptance are separate outcomes. Required missing, skipped, flaky or not-run
evidence cannot produce a passing verdict.

Check IDs serve as criterion IDs in this bootstrap. Each check emits a structured
observation with an effect trace. Expected observations are frozen before product
implementation. A process exit of zero alone cannot prove behavior. Tests may
invoke production serialization, but independent assertions must compare expected
state, byte counts and effects; invoking one helper twice is insufficient.

Source and check identities are rechecked before dispatch and source is checked
again after execution. Dirty and untracked source is refused in the initial
version. This conservative boundary avoids attributing a verdict to a different
candidate. Ignored build artifacts are outside source identity; dependency and
tool fingerprints must describe them separately when relevant.

The trusted operator reviews the plan and supplies its digest. This is not a
sandbox: trusted checks run as that operator. The runner omits ambient secrets
and routing environment variables and redacts common credential-bearing log
patterns. Redaction is best effort; checks must not intentionally print secrets.
Hashes establish byte identity within the retained bundle, not authenticity
against a same-user actor able to rewrite the bundle and all hashes.

An output directory is created exclusively before execution. An interrupted or
incomplete bundle is reported blocked and is never resumed or redispatched by
this CLI. Manager retains its operation record and decides the next action after
reconciliation. A completed bundle can be fetched repeatedly without rerunning
checks. Provider attempts, ReviewSensei accounting, publication and live ledger
recovery are outside this component.

Independent CLI controls cover pass, behavioral failure despite exit zero,
missing or duplicate-key observations, skip/flaky/not-run, bounded capture and
timeout, stale or modified source, reviewed-plan mismatch, tampered logs and
receipts, unfinished bundles and duplicate dispatch refusal. These are bootstrap
runner controls, not G34 product qualification.

The repository has no established ADR policy. This document records a reversible
bootstrap placement and proposed local interface; it does not mark a cross-project
ADR accepted. Future shared schemas and adapter extraction should follow the
integration owner's policy after real composition exposes a need.
