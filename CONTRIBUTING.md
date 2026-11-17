# Contributing

Thanks for your interest in improving `runner-pause-lease`. The bar for a
change is whether it keeps the one promise the tool makes: a host it pauses
comes back, and nothing it does can leave a host stripped with no record of
what to put back. A case where it gets that wrong, a sharper message, and
clearer docs are all welcome.

## Reporting a case it gets wrong

Include what you ran, what it printed, and the runner labels before and after
(the lease issue's marker holds the capture). Leave out tokens and anything
else private.

## Running the checks

These are the checks CI runs on every push and pull request:

```sh
python3 -m unittest discover -s tests -t .
python3 -m pip install -r requirements-dev.txt
ruff format --check . && ruff check . && mypy
```

## Conventions

- **Tests ship with the change.** A bug fix or a feature includes a test in
  the same pull request. A test that guards a failure mode says which one.
- **Fail loudly.** A read that cannot be trusted is a non-zero exit, never an
  empty result that lets a lease close.
- **Keep the lease marker readable by older releases' leases.** A lease can be
  open across an upgrade, so a new version must read every marker an earlier
  one wrote.
- **Standard library only at runtime.** `gh` is the only other requirement.
- **Pin dependencies exactly.** An action reference is its release's commit
  SHA with the version beside it, since a tag can be moved. The tools in
  `requirements-dev.txt` are exact versions. Renovate proposes their updates.
- **Comments explain why, not what.**
- **Keep tracked text ASCII.**

## Submitting

Keep each pull request focused on one change, make sure the checks pass, and
describe the why in the pull request body. CI must be green before merge.

## Releasing

A patch or minor release needs no approval: whoever cuts it follows the steps
below in order, on a `main` commit whose required checks have passed. A major
version, a 1.0, or a minor before 1.0 that changes an interface consumers use
needs the maintainer's explicit approval before its tag is pushed.

Every release gets a fixed `vMAJOR.MINOR.PATCH` tag. The repository blocks
updates and deletions of those tags, and publishing the GitHub Release makes
the tag immutable.

1. Bump `version` in `pyproject.toml` and merge that to `main`.
2. Tag the merge commit `vMAJOR.MINOR.PATCH` and push the tag.
3. Publish it: `gh release create vMAJOR.MINOR.PATCH --verify-tag`.
