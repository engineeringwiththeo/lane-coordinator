# Releases (`scripts/release.py`)

The coordinator ships features in periodic releases. Code moves from `integration` to `main`, a release-candidate tag goes to **staging**, and then that same commit is tagged for **production**. Production never gets a build that staging didn't run.

```
integration ──promote (PR "Release v1.5.0")──▶ main ──merge commit──▶ v1.5.0-rc.1 ─▶ staging
                                                                         │ (fix forward → v1.5.0-rc.2 …)
                                                                         └── same commit ─▶ v1.5.0 ─▶ production
tickets:  merged ───────────────────────────────────────────────────▶ staging ───────────▶ deployed ─▶ done
```

## Cadence

Run `plan` at the cadence the owner sets, for example weekly or at the end of each sprint. Use the `/schedule` or `/loop` skill to make it recurring. If `plan` shows merged tickets, promote them.

## Steps

1. **`release.py plan`** (read-only) shows the next version, the commits that `integration` has and `main` lacks, and the `status:merged` tickets grouped by milestone and epic.
2. **`release.py promote`** opens, or refreshes, the PR from `integration` to `main`, titled `Release vX.Y.Z`. Its body holds the release notes and `Refs #N` lines. It uses Refs rather than Closes so that GitHub doesn't close the issues; the coordinator closes them after review.
3. **Review and merge it with a merge commit:** `gh pr merge <N> --merge`. A squash merge rewrites history, so `main` and `integration` diverge and the next promotion shows the same commits again. `staging` warns if this happens.
4. **`release.py staging --pr <N>`**:
   - tags the merge commit `vX.Y.Z-rc.K` and publishes a GitHub **prerelease**;
   - moves the PR's tickets merged → staging and labels them `release:vX.Y.Z`;
   - runs `release.staging_cmd`, if set.
5. **Verify staging.** If it fails, fix forward on `integration` and promote again. The pending version is reused with the next rc number (`-rc.2`), and the release label gathers every ticket across the candidates.
6. **`release.py prod [--version vX.Y.Z]`**:
   - tags the newest candidate's commit `vX.Y.Z` and publishes it as the **latest** release;
   - moves every ticket labelled `release:vX.Y.Z` staging → deployed;
   - runs `release.prod_cmd`.
7. **Close after review:** `tracker.py status N done`, or `prod --done` once the owner has signed off on the whole release.

**`release.py status`** shows the version in production, the candidate on staging, and how many tickets are merged, staging and deployed.

## Versions

Versions are semver tags with `release.tag_prefix` (default `v`). Feature releases bump **minor** (`release.bump`); pass `--bump patch` or `--bump major` to override. The first release is `v0.1.0`. A candidate whose version isn't in production yet is "pending", and the next promotion reuses it.

## Config (`config.json` → `release`)

| Key | Default | Meaning |
|---|---|---|
| `main_branch` | `main` | the production branch (must differ from `integration_branch`) |
| `bump` | `minor` | default bump for a feature release |
| `tag_prefix` | `v` | tag prefix |
| `staging_cmd` | `null` | shell command run after the rc tag; `{tag}`, `{sha}`, `{version}` are substituted (shell-quoted) |
| `prod_cmd` | `null` | the same, after the production tag |

## Deploying

Either set the `*_cmd` hooks (for example `vercel deploy --prod --yes` from a checkout of `{sha}`, or a `gh workflow run` call), or let CI react to the releases. For example, `.github/workflows/deploy.yml`:

```yaml
on:
  release:
    types: [published]
jobs:
  deploy:
    runs-on: ubuntu-latest
    environment: ${{ github.event.release.prerelease && 'staging' || 'production' }}
    steps:
      - uses: actions/checkout@v4
        with: { ref: "${{ github.event.release.tag_name }}" }
      - run: ./scripts/deploy.sh "${{ github.event.release.prerelease && 'staging' || 'production' }}"
```

GitHub environments let you require approval for `production` and keep secrets separate per environment.

## Hotfixes

For an urgent production fix: branch from the production tag, PR into `main`, merge, then run `release.py promote --bump patch` after merging the fix back into `integration`. Alternatively, tag the fix commit by hand. `release.py` doesn't automate hotfixes, so record them in the sprint report.
