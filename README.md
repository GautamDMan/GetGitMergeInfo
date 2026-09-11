# get_last_merge_info.py

Resets a set of local git repos to a specified branch and extracts info about
the last merge on that branch (who merged it, the merge message, and the
branch that was merged in), writing the results to a CSV.

## What it does

For each row in `input.csv`, the script:

1. Resolves the repo's local path — either directly from `repo_path`, or by
   searching under `--search-root` for a directory named after the `repo`
   value that contains a `.git` folder (see **Input file formats** below).
2. Sets the local git identity (`user.name` / `user.email`) from `git_login.json`.
3. Fetches from the remote (`origin` by default). If the remote is HTTPS and a
   token is provided, the token is temporarily embedded in the remote URL for
   the fetch, then the original URL is restored immediately after.
4. Checks out the specified branch and runs `git reset --hard origin/<branch>`.
5. Reads the most recent merge commit on that branch and parses out:
   - **merge_owner** — the PR submitter if the commit message matches
     `Merge pull request #N from owner/branch`, otherwise falls back to the
     commit author's name.
   - **merge_message** — the commit subject line.
   - **merged_branch** — the branch name parsed from the commit message
     (handles both `Merge pull request #N from owner/branch` and
     `Merge branch 'branch-name'` styles).
   - **merge_commit** — the full commit hash.
   - **merged_at** — the commit's ISO 8601 timestamp.
6. Writes one row per repo to `output.csv`.

## Requirements

- Python 3
- `git` available on `PATH`
- No third-party Python packages required (uses only the standard library)

## Usage

```bash
python get_last_merge_info.py --input input.csv --creds git_login.json --output output.csv
```

The parent directory to search (when using name-based `repo` entries) is
read from `search_root` in `git_login.json` — you don't need to pass it on
the command line, which means the script folder and the repo folder can
live in completely separate locations. Optional flags:

- `--remote <name>` — remote name to fetch from (default: `origin`)
- `--search-root <path>` — overrides `search_root` from `git_login.json` if
  you need to point at a different directory for a particular run. Falls
  back to the current directory if neither is set.

## Input file formats

### input.csv

Two supported styles — pick whichever matches how you have your data:

**Style A — search by name.** Use this when you don't know (or don't want to
maintain) exact paths, and your repos live somewhere under a common parent
directory (e.g. a folder where you keep all your clones, or a monorepo of
checkouts):

```csv
repo,branch
myservice,main
other-repo,develop
```

The script recursively walks `--search-root` looking for a directory named
`myservice` (etc.) that contains a `.git` folder.

- If exactly **one** match is found, it's used automatically.
- If **no** match is found, or **multiple** directories share that name, the
  row is written to `output.csv` with an `ERROR:` message (ambiguous matches
  list every path found so you can pick the right one and switch to Style B
  for that row, or add it to `repo_paths` in `git_login.json`).

**Style B — exact path.** Use this when you already know precisely where
each repo lives:

```csv
repo_path,branch
/path/to/repo1,main
/path/to/repo2,develop
```

- `repo_path` — absolute or relative path to a local git repository.
- `branch` — the branch to reset the repo to before reading merge info.

You can mix both styles across rows in the same file (a row with `repo_path`
filled in skips searching even if the `repo` column also exists).

### git_login.json

```json
{
  "username": "yourname",
  "email": "you@example.com",
  "token": "ghp_xxx",
  "search_root": "/path/to/parent/dir",
  "repo_paths": {
    "myservice": "/exact/path/to/myservice"
  }
}
```

- `username` / `email` — set as the local git identity in each repo.
- `token` — optional. Only used if the remote uses HTTPS and requires
  authentication for `git fetch`.
- `search_root` — optional. Root directory to search under when `input.csv`
  uses the `repo` (name) column. This lets the config file — not the script's
  own location — hold the environment-specific path to where your repos live,
  so the Python script folder can live anywhere. A `--search-root` CLI flag,
  if given, overrides this.
- `repo_paths` — optional. A dict mapping a specific repo name to an exact
  path, skipping the directory search entirely for that name. Useful for one-
  off overrides without switching that row to the `repo_path` CSV column.

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

- **Search mode assumes one repo = one directory name.** If you have several
  unrelated repos that happen to share the same folder name under
  `--search-root`, the row will come back as an ambiguous-match error rather
  than guessing — resolve it manually with an exact `repo_path` for that row.
- **Search performance:** the search walks the entire `--search-root` tree
  (skipping into `.git` internals), so pointing it at a very large directory
  tree (e.g. your whole home folder) will be slower than pointing it at a
  smaller parent folder that directly contains your repos.

## Possible follow-ups

- Support non-GitHub merge message conventions (GitLab, Bitbucket, Azure
  DevOps commit message formats).
- Support SSH-based auth explicitly (currently only HTTPS + token is handled;
  SSH remotes are left untouched and rely on your existing SSH agent/config).
- Add a `remote` column to `input.csv` if different repos need different
  remote names.
