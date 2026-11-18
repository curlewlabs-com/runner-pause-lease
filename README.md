# runner-pause-lease

## What it is

A command that takes a self-hosted GitHub Actions runner host out of job
routing for a bounded window, and puts it back by itself. A pause removes the
host's runners' routing labels and leaves the jobs they are already running
alone. It records what it removed in a GitHub issue, the lease. A `reconcile`
run on a schedule puts the labels back when the window ends, or as soon as
someone closes the issue.

It is standard-library Python and makes every GitHub call through the
[`gh`](https://cli.github.com/) CLI.

## Why it exists

Some runner hosts need to stop taking work for a while. A laptop that is also
a runner should not start a build while its owner is presenting. A VM should
not take a job just before its hypervisor reboots. The usual remedies are to
stop the runner service or remove the runner. Either one interrupts a job that
is running, and either one leaves the host out of the pool until somebody
remembers to bring it back.

Removing labels avoids the first problem. Every `runs-on` selects on labels, so
a runner with none of the labels a workflow asks for is never assigned a new
job. A runner that already has a job keeps it and finishes it. The host goes
quiet as its current work drains, and nothing is cancelled.

The second problem is the dangerous one. A runner without labels does not go
red, fail, or appear in any alert. It is simply never scheduled again. A
restore driven by a one-shot timer (`at`, a launchd job, a scheduled dispatch)
fails silently when the timer does not fire. So this tool does not use a timer.
A pause writes a lease with an expiry, and a reconciler restores every lease
that has ended. A missed reconcile is retried by the next one, and a restore
that fails leaves the lease in place and makes the run fail.

## How to use it

### Name your hosts with a label

The tool finds a host's runners by one label of the form `<prefix><host>`, for
example `host:build-1` on every runner of `build-1`. That label is never
removed, so a paused host can still be found. Add it when you register each
runner (`./config.sh --labels host:build-1,...`) or in the repository's runner
settings.

### Give it two credentials

- **The workflow token**, with `issues: write`, for the lease issue. `gh`
  reads it from `GH_TOKEN`.
- **`RUNNER_ADMIN_TOKEN`**, for the runner label calls. Those endpoints need
  repository Administration (read and write), which a workflow token cannot be
  granted at any `permissions:` setting. Use a fine-grained personal access
  token, or a GitHub App token, scoped to that one permission on that one
  repository. Outside Actions, leave it unset and the `gh` login is used for
  everything.

### Run it from a workflow

The workflow needs both legs. The dispatch pauses or resumes a host; the
schedule is the reconciler that brings every host back.

```yaml
name: Runner pause

on:
  workflow_dispatch:
    inputs:
      action:
        type: choice
        options: [pause, resume]
        required: true
      host:
        type: string
        required: true
  schedule:
    - cron: "7,27,47 * * * *"

permissions:
  contents: read
  issues: write

# One group for every run: a reconcile restoring a host while a pause of the
# same host is half done would have both writing the lease at once. Never
# cancel in progress: a cancelled pause can stop between two label removals.
concurrency:
  group: runner-pause
  cancel-in-progress: false

jobs:
  pause:
    # A GitHub-hosted runner, so no pause can strip the runner that has to run
    # the restore.
    runs-on: ubuntu-24.04
    steps:
      - name: Ensure the lease label exists
        env:
          GH_TOKEN: ${{ github.token }}
        run: gh label create ci-quiet --force --repo "$GITHUB_REPOSITORY" --color FBCA04 --description "A runner pause owns this host's routing"
      - name: Install
        run: pipx install git+https://github.com/curlewlabs-com/runner-pause-lease@v0.2.0
      - name: Pause, resume, or reconcile
        env:
          GH_TOKEN: ${{ github.token }}
          RUNNER_ADMIN_TOKEN: ${{ secrets.RUNNER_ADMIN_TOKEN }}
          # Through the environment, so an input is never part of the
          # command's grammar.
          ACTION: ${{ inputs.action }}
          HOST: ${{ inputs.host }}
        run: |
          set -eu
          if [ "$GITHUB_EVENT_NAME" = schedule ]; then
            runner-pause-lease --host-label-prefix host: reconcile
          else
            runner-pause-lease --host-label-prefix host: "$ACTION" --host "$HOST"
          fi
```

If the workflow runs on your own runners instead, keep at least one runner
that you never pause able to take it. A paused pool cannot run its own
restore.

### Or from a shell

```sh
runner-pause-lease --repo OWNER/NAME --host-label-prefix host: pause --host build-1
runner-pause-lease --repo OWNER/NAME --host-label-prefix host: pause --host build-1 build-2
runner-pause-lease --repo OWNER/NAME --host-label-prefix host: resume --host build-1
runner-pause-lease --repo OWNER/NAME --host-label-prefix host: status --host build-1
runner-pause-lease --repo OWNER/NAME --host-label-prefix host: reconcile
```

### Commands

- **`pause --host H...`** reconciles first, then pauses each host for four
  hours. Pausing a host that is already paused extends its window, but never
  past twelve hours from when that window first opened. A pause after a
  window ends, whether by expiry or by the cap, restores the host and opens a
  new window. It refuses a host whose earlier lease still owes its restore,
  and refuses every host while any lease issue will not parse, since that
  lease could be the host's; either way it changes nothing.
- **`resume --host H...`** closes the named hosts' lease issues, restores
  them, then reconciles the rest. A restore that fails is retried by the next
  reconcile, because a closed issue that keeps its label is a resume request.
- **`status --host H`** exits 3 while the host is inside a live window. When
  it is not, it exits 0, or 1 if some lease issue cannot be read, since that
  lease could be this host's.
- **`reconcile`** retries any unfinished strip, and restores every lease whose
  window ended or whose issue was closed.

Ending a pause needs no tooling: close the lease issue, and the next reconcile
restores the host.

### Options

| option                             | default              | why it exists                                                                                                                                                                                                                                                                                                                         |
| ---------------------------------- | -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--repo OWNER/NAME`                | `$GITHUB_REPOSITORY` | The repository whose runners and issues to use.                                                                                                                                                                                                                                                                                       |
| `--host-label-prefix P`            | none; required       | How your runners name their host. A host is every runner carrying `P<host>`, and those labels are never removed.                                                                                                                                                                                                                      |
| `--keep-label-prefix Q`            | none                 | Repeatable. A pause has to remove every label a workflow could select on, and it cannot see your workflows, so it removes every custom label except the host label. If your runners carry a label that identifies them and routes no job, such as a VM name an inventory tool reads, name its prefix here, or a pause removes it too. |
| `RUNNER_ADMIN_TOKEN` (environment) | the `gh` login       | The credential for the runner label calls; see above.                                                                                                                                                                                                                                                                                 |

The window (four hours), the cap (twelve hours) and the lease label
(`ci-quiet`) are fixed.

## How it works

**What comes off.** Every custom label on the host's runners, except the host
label and any `--keep-label-prefix` label. That includes `self-hosted`, `Linux`,
`macOS`, `X64` and `ARM64` on a runner registered with `--no-default-labels`,
where they are custom labels: left on, they would still route `runs-on:
self-hosted` jobs to the host. The default labels of a runner registered the
ordinary way are read-only, so GitHub does not allow removing them; a workflow
that selects only on those still reaches the host.

**The capture is written before any label comes off.** The lease issue is
created first, with a hidden one-line marker in its body that holds every
runner's id, name and custom labels. If writing it fails, nothing is stripped.
Labels are removed one at a time, so the host label survives, and a removal
that fails is reported without stopping the rest.

**A partial strip converges.** Until one pass reads the runners and finds none
of them still routable, each reconcile removes the labels it still sees. A
runner or label it finds that the capture lacks is added to the capture before
it is removed, so the eventual restore puts it back. That clean read is
recorded in the lease, and after it the reconciler stops reading runner state
for that lease.

**A second pause never re-captures.** On a paused host it would capture the
stripped set, which is empty, and the restore would put nothing back. It
extends the existing lease instead.

**The label is the state, not the issue's open or closed flag.** The
`ci-quiet` label on the issue means the lease still owns the host's routing,
and only a successful restore removes it. A closed issue that still carries the
label is a resume the reconciler has not carried out yet. A reader keyed on
open issues would treat closing the issue as the end of the pause, and the host
would be left stripped.

**Restores keep going.** A runner that cannot be restored is reported, the rest
are restored, the lease stays labelled, and the command exits non-zero. A
repeated restore is safe, because adding a label a runner already has changes
nothing. A captured runner that was unregistered during the window is skipped
and named in the closing comment. An empty read of the runner list is never
taken to mean that every runner is gone, since closing the lease on it would
discard the only record of what to restore. The restore is attempted for each
runner instead.

## What it does not do

- **It does not stop jobs that are already running.** The host goes quiet when
  its current jobs end.
- **It does not refuse to pause the last host in a pool.** Pausing every host
  that serves a label queues that label's jobs until a window ends. A tool
  that refuses when someone needs the pause is a tool they work around.
- **A lease has no owner.** A second pause extends it, and any resume ends it
  for everyone.
- **A runner registered after the strip was confirmed is not paused.** Once a
  pass has found the host clean, the reconciler stops polling for the rest of
  the window.
- **A lease whose marker will not parse cannot be restored automatically.**
  Nothing can know what to put back, so `reconcile` and `status` report it and
  exit non-zero, rather than skipping it, and `pause` refuses until it is
  repaired: put the host's labels back by hand, then fix the marker or take the
  `ci-quiet` label off the issue.
- **It does not serialize itself.** Run every invocation under one concurrency
  group, as in the workflow above, or hold your own lock around it.

## Requirements

- Python 3.12 or newer.
- `gh` on `PATH`.

## License

Licensed under either of

- Apache License, Version 2.0 ([LICENSE-APACHE](LICENSE-APACHE))
- MIT license ([LICENSE-MIT](LICENSE-MIT))

at your option. Releases published under the MIT license alone may also be used
under either license.

### Contribution

By submitting a contribution for inclusion in this project, you agree that it
is dual licensed as above, without any additional terms or conditions.
