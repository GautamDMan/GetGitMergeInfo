# get_last_merge_info.py

Resets a set of local git repos to a specified branch and extracts info about
the last merge on that branch (who merged it, the merge message, and the
branch that was merged in), writing the results to a CSV.

## What it does

For each row in `input.csv`, the script:

1. Sets the local git identity (`user.name` / `user.email`) from `git_login.json`.
2. Fetches from the remote (`origin` by default). If the remote is HTTPS and a
   token is provided, the token is temporarily embedded in the remote URL for
   the fetch, then the original URL is restored immediately after.
3. Checks out the specified branch and runs `git reset --hard origin/<branch>`.
4. Reads the most recent merge commit on that branch and parses out:
   - **merge_owner** — the PR submitter if the commit message matches
     `Merge pull request #N from owner/branch`, otherwise falls back to the
     commit author's name.
   - **merge_message** — the commit subject line.
   - **merged_branch** — the branch name parsed from the commit message
     (handles both `Merge pull request #N from owner/branch` and
     `Merge branch 'branch-name'` styles).
   - **merge_commit** — the full commit hash.
   - **merged_at** — the commit's ISO 8601 timestamp.
5. Writes one row per repo to `output.csv`.

## Requirements

- Python 3
- `git` available on `PATH`
- No third-party Python packages required (uses only the standard library)

## Usage

```bash
python get_last_merge_info.py --input input.csv --creds git_login.json --output output.csv
```

Optional flag:

- `--remote <name>` — remote name to fetch from (default: `origin`)

## Input file formats

### input.csv

```csv
repo_path,branch
/path/to/repo1,main
/path/to/repo2,develop
```

- `repo_path` — absolute or relative path to a local git repository.
- `branch` — the branch to reset the repo to before reading merge info.

### git_login.json

```json
{
  "username": "yourname",
  "email": "you@example.com",
  "token": "ghp_xxx"
}
```

- `username` / `email` — set as the local git identity in each repo.
- `token` — optional. Only used if the remote uses HTTPS and requires
  authentication for `git fetch`.

## Output file format

### output.csv

```csv
repo_path,branch,merge_owner,merge_message,merged_branch,merge_commit,merged_at
```

If a repo can't be processed (bad path, invalid branch, no merge commits on
the branch, auth failure, etc.), the row is still written with the error
recorded in `merge_message` (prefixed with `ERROR:`) and the remaining fields
left blank, so one bad repo won't stop the rest of the batch.

## Important caveats

- **Destructive operation:** `git reset --hard` discards any local
  uncommitted changes in each repo before reading merge info. Don't point
  this at a repo with work you haven't committed or stashed.
- **"Owner" heuristic:** the script assumes GitHub-style merge commit
  messages (`Merge pull request #N from owner/branch`) to identify the PR
  submitter. If your repos use squash merges, a different git host, or a
  custom merge message format, `merge_owner` will fall back to the commit
  author and `merged_branch` may be blank.
- **Only the most recent merge commit is inspected** — regular (non-merge)
  commits on top of a merge are ignored when looking for "last merge info."
- Tested against a local bare-repo + clone setup (see script history); not
  yet tested against a real authenticated HTTPS remote — verify the token
  injection path works with your git host before running on production repos.

## Possible follow-ups

- Support non-GitHub merge message conventions (GitLab, Bitbucket, Azure
  DevOps commit message formats).
- Support SSH-based auth explicitly (currently only HTTPS + token is handled;
  SSH remotes are left untouched and rely on your existing SSH agent/config).
- Add a `remote` column to `input.csv` if different repos need different
  remote names.
