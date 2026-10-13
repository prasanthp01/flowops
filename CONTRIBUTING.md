# Contributing

Use Python 3.12+, uv and Docker Compose. Follow the isolated setup and verification commands in README. Tests use only `flowops_test`; never point them at another database. Preserve the public synthetic boundary, scoped handoffs, deterministic authorization and truthful mode/cost labels.

For behavior changes, add a regression that demonstrates the user-visible outcome or security/recovery invariant. Keep evaluation ground truth outside the runtime. Regenerate typed schemas after contract edits. Verify rendered behavior and keyboard/mobile states for UI changes, and update the evidence report with actual commands/results.

Do not commit `.local`, credentials, private endpoints, databases, browser state or screenshots containing unapproved information. Submit changes through a pull request. Describe the behavior changed, validation and any deployment implications.
