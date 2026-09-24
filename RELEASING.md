# Releasing Jaime

> **Status: proposed.** This document describes the release process we intend
> to build. The workflows it references (`release.yml`, `promote.yml`) do not
> exist yet. It is written down first so the design can be reviewed and agreed
> before any CI or store action is taken. The concrete tasks live in
> `TASKS.md` under 5.4.

Jaime ships two charms from one repository:

| Charm | `charmcraft.yaml` | CharmHub name | CharmHub status |
|---|---|---|---|
| machine subordinate | `charms/machine/charmcraft.yaml` | `jaime` | published, `latest/edge` |
| Kubernetes standalone | `charms/k8s/charmcraft.yaml` | `jaime-k8s` | not published yet |

## Versioning

- **One version for the repository.** Both charms are released together under a
  single version. There is no per-charm version.
- **SemVer** `MAJOR.MINOR.PATCH`, git tags carrying a `v` prefix: `v0.1.0`.
  (Older tags `0.0.6` and `0.0.7` predate this convention and are not
  retro-triggered.)
- **`version:` in each `charmcraft.yaml` is the displayed version.** `charmcraft
  pack` has no `--version` flag and there is no `craftctl set-version`, so the
  value is whatever the file says. It must equal the tag.
- **`CHANGELOG.md`** gets a `## [X.Y.Z] - YYYY-MM-DD` section, cut from
  `[Unreleased]`, in the same release pull request.

Two CharmHub terms that are easy to conflate:

- **revision** — an integer assigned by the store on each upload (`rev 5`). It is
  unrelated to the version.
- **track / risk / branch** — a channel like `0.1/edge`. The `track` is `0.1`,
  the `risk` is `edge`. Tracks beyond `latest` are created per charm and gated by
  a store guardrail.

## Channel model

The channel track is **derived from the version**: track = `MAJOR.MINOR`.

| Tag | Track | Channels updated by the release |
|---|---|---|
| `v0.1.0` | `0.1` | `0.1/edge` and `latest/edge` |
| `v0.1.1` | `0.1` | `0.1/edge` and `latest/edge` |
| `v0.2.0` | `0.2` | `0.2/edge` and `latest/edge` |

- Every release also updates `latest/edge`, so anyone tracking the tip keeps
  getting updates.
- `latest/stable` is **not** updated automatically. Stable is a deliberate
  promotion (see below), and is gated on 4.2, 4.4 and 4.6 landing.
- `charmcraft upload` accepts `--release` more than once, so one upload sets both
  channel pointers.

## Prerequisites (one-time, manual)

These are outside CI and need to be done before the first tag.

### 1. Register both charm names

`jaime` is registered. Confirm `jaime-k8s`:

```bash
charmcraft names                 # names registered to your account
juju info jaime-k8s              # does the store already know it?
charmcraft register jaime-k8s    # only if it is missing
```

CharmHub discusses registrations on Discourse, so approval is not always
instant.

### 2. Request the track guardrail

A track can only be created if it matches a **guardrail** approved for that
charm. Request a guardrail that allows `MAJOR.MINOR` tracks (for example a
pattern matching `\d+\.\d+`) plus `latest`, for both `jaime` and `jaime-k8s`, in
the CharmHub requests category at <https://discourse.charmhub.io>.

This is the long pole and the only step with an external reviewer. Until it is
approved, `0.1/edge` cannot exist.

### 3. Create the track for each new minor

Once the guardrail is approved, create the track the first time a minor is
released. This is a manual checklist step rather than something CI does, so CI
credentials can stay narrowly scoped:

```bash
charmcraft create-track --name jaime     --track 0.1
charmcraft create-track --name jaime-k8s --track 0.1
```

### 4. CI credentials

Create a store credential scoped to both charms and export it:

```bash
charmcraft login --export /secure/charmhub-creds \
    --charm jaime --charm jaime-k8s \
    --ttl <seconds>
```

`--ttl` defaults to **30 hours**, which is too short for a CI secret; choose a
long value (or plan a rotation). Add the file contents as the repository secret
`CHARMCRAFT_AUTH`, and put the publishing jobs behind a protected GitHub
`environment:` so a release needs approval.

## Trigger

A release is started by pushing a version tag:

```bash
git tag v0.1.0
git push origin v0.1.0
```

The tag is the single event. The workflow validates the tag, packs the charms,
publishes them, and creates the GitHub Release. There is also a
`workflow_dispatch` entry point with a `dry_run` input so the pipeline can be
exercised without publishing.

## Workflows to add

### `.github/workflows/release.yml`

```yaml
on:
  push:
    tags: ['v*']
  workflow_dispatch:
    inputs:
      version:   { required: true }
      dry_run:   { type: boolean, default: true }
```

**`validate`**
1. Derive `VERSION` from the tag (`v` stripped) and `TRACK` as
   `MAJOR.MINOR`.
2. Assert `CHANGELOG.md` contains a `## [VERSION] ` heading.
3. Assert both `charms/*/charmcraft.yaml` `version:` values equal `VERSION`.
4. Run `./scripts/test.sh` so a tag cut from a broken commit cannot publish.

**`publish`** — matrix over the two charms:
1. Install charmcraft and initialise LXD the same way `ci.yml`'s pack job
   does, then pack (`make pack-all`).
2. Check whether `TRACK` exists on the charm
   (`charmcraft status <name> --format=json`).
3. Upload and release to the versioned track and to `latest/edge`:
   ```bash
   charmcraft upload --release "${TRACK}/edge" --release latest/edge <artifact>
   ```
   If `TRACK` does not exist yet, fall back to `latest/edge` only and emit a
   warning. This is the first-release path: publish the tip, add the versioned
   track once the guardrail lands, then release the same revision there.

**`github-release`**
1. Extract the `## [VERSION]` section from `CHANGELOG.md` into a notes file.
2. `gh release create "v${VERSION}" --notes-file <notes>`.

Job hygiene: `permissions: contents: write` for the release job only, the
`charmhub` environment for the publishing job, `concurrency` keyed on the tag,
and third-party actions pinned to a commit SHA as `ci.yml` already does for
`actions-operator`.

### `.github/workflows/promote.yml`

A manual `workflow_dispatch` workflow to move a released revision along the
risks:

```yaml
on:
  workflow_dispatch:
    inputs:
      charm:    { type: choice, options: [jaime, jaime-k8s, both] }
      version:  { description: "X.Y.Z (selects the X.Y track)" }
      risk:     { type: choice, options: [beta, stable] }
      revision: { description: "optional; defaults to the revision on X.Y/edge" }
      dry_run:  { type: boolean, default: true }
```

Resolve the revision from `charmcraft status <name> --format=json` when not
given, then:

```bash
charmcraft release <name> --revision <N> --channel "${TRACK}/${risk}"
```

## Release checklist

1. Land the feature work on `main` with green CI.
2. Open the release pull request:
   - move `CHANGELOG.md`'s `[Unreleased]` entries into `## [X.Y.Z] - <date>`;
   - set `version: "X.Y.Z"` in both `charms/*/charmcraft.yaml`.
3. Merge.
4. For a new minor, create the track (Prerequisites step 3).
5. `git tag vX.Y.Z && git push origin vX.Y.Z`.
6. Watch `release.yml`; confirm the GitHub Release exists.
7. Verify:
   ```bash
   charmcraft status jaime
   charmcraft status jaime-k8s
   juju info jaime
   gh release view vX.Y.Z
   ```
8. Promote to `beta`, then `stable`, via `promote.yml` once 4.2, 4.4 and 4.6
   have landed and the edge revision has soaked.

## Rollback

Promotion moves a channel pointer, so a bad promotion is undone by re-releasing
the previous revision to that channel:

```bash
charmcraft release <name> --revision <previous> --channel "${TRACK}/${risk}"
```

`charmcraft close <name> --channel <channel>` closes a channel entirely if it
must be withdrawn. Neither removes an uploaded revision.

## Security notes

- CI credentials are attenuated to the two charms. Track creation stays manual
  precisely so the CI token does not need store-wide track permission.
- `CHARMCRAFT_AUTH` lives in a protected environment; publishing requires
  approval.
- Do not run the publishing steps under `set -x`, and never echo the credential.
- Pin third-party actions; prefer the preinstalled `gh` CLI over a third-party
  release action.

## Open questions

- **`latest/stable` policy.** Should `latest/stable` ever move, or does stable
  only ever exist on versioned tracks? The current plan leaves `latest` at edge
  and puts stable on the versioned track.
- **Credential TTL and rotation.** The 30-hour default is unusable for CI;
  confirm the accepted long-lived value and a rotation reminder.
- **`charmcraft promote` vs `charmcraft release`.** `charmcraft promote` exists
  and may express "move a revision from `X.Y/edge` to `X.Y/beta`" more directly
  than `release --revision`. Decide during implementation.
- **`jaime-k8s` registration.** Confirm the name is available and approved
  before relying on the first upload.

## Current state (for reference)

- Versions: `jaime` `0.0.7`, `jaime-k8s` `0.0.1`.
- Tags: `0.0.6`, `0.0.7` (no `v` prefix).
- `jaime` is on `latest/edge` at revision 5.
- Only `.github/workflows/ci.yml` exists; it runs on pull requests and pushes to
  `main`, not on tags.