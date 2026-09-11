# get_last_merge_info.py

Given a list of file names, finds each file inside a single local git repo
and reports the last merge that touched it — who merged it, the merge
message, and the branch it came from.

## What it does

For each file name in `input.csv`, the script:

1. If `branch` is set in `git_login.json`, first checks out that branch and
   runs `git reset --hard <branch>` to discard any local changes and align
   the working tree exactly to that branch's tip — before any searching or
   merge-info lookup happens. If `branch` is omitted, this step is skipped
   and the repo is read exactly as it currently sits on disk.
2. Searches the repo (given by `repo_path` in `git_login.json`) for a file
   with that exact name, anywhere in the tree.
3. If exactly one match is found, looks up the most recent **merge commit**
   that touched that file and parses out:
   - **merge_owner** — the PR submitter if the merge commit message matches
     `Merge pull request #N from owner/branch`, otherwise falls back to the
     commit author's name.
   - **merge_message** — the merge commit's subject line.
   - **merged_branch** — the branch name parsed from the commit message
     (handles both `Merge pull request #N from owner/branch` and
     `Merge branch 'branch-name'` styles).
   - **merge_commit** — the full commit hash.
   - **merged_at** — the commit's ISO 8601 timestamp.
4. Writes one row per file name to `output.csv`.

No `git fetch` is performed — only a local checkout + hard reset to whatever
`branch` currently points to on disk. If you need the branch itself updated
from a remote first, run `git fetch`/`git pull` yourself before running this
script.

## Requirements

- Python 3
- `git` available on `PATH`
- No third-party Python packages required (uses only the standard library)

## Usage

```bash
python get_last_merge_info.py --input input.csv --creds git_login.json --output output.csv
```

## Input file formats

### input.csv

```csv
file_name
config.py
utils.py
README.md
```

Just the bare file name (not a path) — the script searches the whole repo
tree for it.

### git_login.json

```json
{
  "repo_path": "/path/to/the/repo",
  "branch": "main"
}
```

- `repo_path` — the local path to the one git repository to search within.
- `branch` — optional. If given, the repo is hard-reset to this branch
  (`git checkout <branch>` + `git reset --hard <branch>`) before any file
  searching happens. **This discards uncommitted local changes** in the repo.
  If omitted, no reset is performed and the repo is read as-is.

No actual git credentials/tokens are required here despite the file's name —
everything the script does is local (checkout, reset, log).

## Output file format

### output.csv

```csv
file_name,resolved_path,merge_owner,merge_message,merged_branch,merge_commit,merged_at
```

If a file can't be resolved or has no merge history, the row is still
written with the error recorded in `merge_message` (prefixed with `ERROR:`),
so one problem file won't stop the rest of the batch. Two error cases:

- **Not found** — no file with that name exists anywhere in the repo.
- **Ambiguous** — more than one file shares that name (e.g. `utils.py` in
  both `src/` and `tests/`); the error message lists every matching path
  found so you can pick which one you meant.

## Important caveats

- **Destructive operation:** when `branch` is set, `git reset --hard`
  discards any local uncommitted changes in the repo before searching for
  files. Don't point this at a repo with work you haven't committed or
  stashed, unless you're fine losing it.
- **No remote sync:** the reset targets whatever `branch` already points to
  locally — it does not fetch or pull first. If your local branch is behind
  the remote, you'll get merge info as of the local branch tip, not the
  latest remote state.

## Important technical note

Git hides merge commits from path-filtered history (`git log -- <path>`) by
default whenever the merge's result is identical to one of its parents —
which is the normal case for a clean, non-conflicting merge. This script
adds `--full-history` alongside `--merges` specifically to avoid missing
real merges because of that simplification. Without it, a perfectly valid
merge that introduced a file's changes can silently be skipped.

## Possible follow-ups

- Support matching by partial path or glob pattern instead of exact bare
  file name, for cases where you know the subdirectory but want to disambiguate.
- Report *all* merges that touched a file (a history), not just the most
  recent one.
- Accept a directory (rather than a single file) and report merge info for
  everything inside it.
