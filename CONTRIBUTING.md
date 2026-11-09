# Contributing

Keep changes small and link them to a tracked acceptance criterion. Record the
exact source commit and commands used for validation. Update PROGRESS.md when a
milestone changes and ROADMAP.md when scope changes. Implemented, locally tested,
hosted-qualified and released are distinct states.

Open an implementation issue before overlapping changes to a shared runtime seam.
Use draft pull requests for work in progress. Do not imply that a passing command
proves acceptance unless the explicit gate and required evidence pass.

Never commit credentials, account configuration, raw provider captures, private
workflow databases, real user fixtures, private repository source or private chat
links. Share sanitized examples and bounded summaries. Before publication, review
the complete tracked tree, history and source distribution, not only the diff.

## Local validation

Run `python3 -m unittest discover -s . -p test_qasensei.py -v`.
Run `python3 -m build --no-isolation --sdist --wheel` and inspect both archives.
Install the wheel in a fresh environment and smoke-test its documented CLI.

## License

Contributions are distributed under this repository's MIT license.
