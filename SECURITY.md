# Security boundaries

This alpha CLI runs under a trusted local operator. It is not an OS sandbox,
multi-tenant service or credential store. Read the documented trust and execution
boundaries before running checks or importing receipts.

Do not put real secrets, private captures or sensitive reproduction data in public
issues, pull requests, workflow logs or evidence bundles. Report non-sensitive
control failures with synthetic fixtures. A secret scanner or log redactor is a
best-effort check, not permission to publish unreviewed runtime artifacts.

No release, merge, deployment, provider purchase or live recovery operation is
automatically authorized by this CLI or by a successful local test run.
