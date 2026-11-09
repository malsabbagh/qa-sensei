# Publication checklist

This source-only bootstrap uses the MIT license. Private workflow evidence and
original repository history are not part of the public source snapshot.

Before publishing any update:

- Review the complete tracked source and commit history for secrets and private data.
- Include only synthetic, portable examples; exclude account and provider settings.
- Exclude workflow databases, raw logs, captures, local evidence bundles and caches.
- Check the contents of both wheel and source distribution, including MIT notices.
- Run the deterministic controls and smoke-test a clean installed wheel.
- Record the exact commit and validation outcomes in the milestone issue.
- Keep acceptance, hosted qualification, CI and release state separate.

Secret scanning and best-effort log redaction do not guarantee that arbitrary
runtime output is safe for publication. Never upload private execution evidence
without reviewing and sanitizing it for the intended audience.

Initial preparation: the portable source was copied from an isolated bootstrap,
private/environment-specific documentation was removed, fixtures were reviewed,
and a new source-only history was started. No historical runtime captures or
private journal state were imported.
