#!/usr/bin/env python3
"""
For each repo listed in input.csv, resets a local git working copy to the
specified branch (fetching from origin first) and extracts the last merge
commit's info (owner/author, merge message, merged-in branch).

Usage:
    python get_last_merge_info.py --input input.csv --creds git_login.json --output output.csv --search-root /path/to/parent/dir

input.csv format (either style works):

    # Style A: give the repo name/object and let the script find it
    repo,branch
    myservice,main
    other-repo,develop

    # Style B: give the exact local path directly (skips searching)
    repo_path,branch
    /path/to/repo1,main
    /path/to/repo2,develop

When input.csv uses the "repo" column, --search-root is walked recursively to
find a directory matching that name which contains a .git folder. If exactly
one match is found, it's used automatically. If zero or multiple matches are
found, the row is recorded as an error in output.csv (ambiguous matches are
all listed so you can disambiguate).

git_login.json format:
    {
        "username": "yourname",
        "email": "you@example.com",
        "token": "ghp_xxx",        // optional, only needed for HTTPS remotes that require auth
        "search_root": "/path/to/parent/dir",   // optional, used when input.csv has a 'repo' name column
        "repo_paths": {                          // optional, per-name path overrides (skip searching)
            "myservice": "/exact/path/to/myservice"
        }
    }

--search-root on the command line, if given, takes priority over the
"search_root" value in git_login.json.

output.csv format:
    repo_path,branch,merge_owner,merge_message,merged_branch,merge_commit,merged_at
"""

import argparse
import csv
import json
import os
import re
import subprocess
import sys
from urllib.parse import urlsplit, urlunsplit

# Matches: "Merge pull request #123 from owner/branch-name"
PR_MERGE_RE = re.compile(r"Merge pull request #\d+ from ([^\s/]+)/(\S+)")
# Matches: "Merge branch 'branch-name' into target" (or without "into target")
BRANCH_MERGE_RE = re.compile(r"Merge branch '([^']+)'")


def run_git(repo_path, args, check=True):
    result = subprocess.run(
        ["git", "-C", repo_path] + args,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def load_credentials(creds_path):
    with open(creds_path, "r") as f:
        return json.load(f)


def load_rows(input_csv):
    """Supports two input.csv styles:
      - columns 'repo_path','branch'  -> exact local path given directly
      - columns 'repo','branch'       -> repo name/object to search for
    """
    rows = []
    with open(input_csv, "r", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = set(reader.fieldnames or [])
        has_path = "repo_path" in fieldnames
        has_name = "repo" in fieldnames
        if "branch" not in fieldnames or not (has_path or has_name):
            raise ValueError(
                "input.csv must have a 'branch' column plus either 'repo_path' "
                f"(exact path) or 'repo' (name to search for). Found: {reader.fieldnames}"
            )
        for row in reader:
            branch = row["branch"].strip()
            if has_path and row.get("repo_path", "").strip():
                rows.append({
                    "identifier": row["repo_path"].strip(),
                    "mode": "path",
                    "branch": branch,
                })
            elif has_name and row.get("repo", "").strip():
                rows.append({
                    "identifier": row["repo"].strip(),
                    "mode": "search",
                    "branch": branch,
                })
    return rows


def find_repo_path(search_root, repo_name):
    """Recursively search search_root for a directory named repo_name that
    contains a .git folder. Returns (path_or_None, list_of_all_matches)."""
    matches = []
    for dirpath, dirnames, _filenames in os.walk(search_root):
        # Don't descend into .git internals - no need to search inside them
        if ".git" in dirnames:
            if os.path.basename(dirpath) == repo_name:
                matches.append(dirpath)
            # prevent walking into this repo's .git directory contents
            dirnames.remove(".git")
    if len(matches) == 1:
        return matches[0], matches
    return None, matches


def set_local_identity(repo_path, creds):
    if creds.get("username"):
        run_git(repo_path, ["config", "user.name", creds["username"]])
    if creds.get("email"):
        run_git(repo_path, ["config", "user.email", creds["email"]])


def inject_token_into_remote(repo_path, remote, token):
    """If the remote is HTTPS, temporarily embed the token for auth.
    Returns the original URL so it can be restored afterwards (or None)."""
    original_url = run_git(repo_path, ["remote", "get-url", remote], check=False)
    if not original_url or not token:
        return None
    parts = urlsplit(original_url)
    if parts.scheme not in ("http", "https"):
        return None  # SSH or other - leave untouched
    new_netloc = f"{token}@{parts.netloc}"
    new_url = urlunsplit((parts.scheme, new_netloc, parts.path, parts.query, parts.fragment))
    run_git(repo_path, ["remote", "set-url", remote, new_url])
    return original_url


def restore_remote(repo_path, remote, original_url):
    if original_url:
        run_git(repo_path, ["remote", "set-url", remote, original_url])


def fetch_and_reset(repo_path, branch, remote="origin"):
    run_git(repo_path, ["fetch", remote])
    # Create/switch to a local branch tracking the remote one if needed
    run_git(repo_path, ["checkout", "-B", branch, f"{remote}/{branch}"])
    run_git(repo_path, ["reset", "--hard", f"{remote}/{branch}"])


def get_last_merge_info(repo_path):
    """Find the most recent merge commit and parse owner/message/branch from it."""
    log_format = "%H%x01%an%x01%s%x01%cI"
    output = run_git(
        repo_path,
        ["log", "--merges", "-1", f"--pretty=format:{log_format}"],
        check=False,
    )
    if not output:
        return {"error": "no merge commits found on this branch"}

    commit_hash, author, subject, committed_at = output.split("\x01")

    merged_branch = ""
    pr_owner = None
    m = PR_MERGE_RE.search(subject)
    if m:
        pr_owner, merged_branch = m.group(1), m.group(2)
    else:
        m = BRANCH_MERGE_RE.search(subject)
        if m:
            merged_branch = m.group(1)

    return {
        "merge_owner": pr_owner or author,
        "merge_message": subject,
        "merged_branch": merged_branch,
        "merge_commit": commit_hash,
        "merged_at": committed_at,
    }


def main():
    parser = argparse.ArgumentParser(description="Reset local git repos and extract last merge info")
    parser.add_argument("--input", default="input.csv", help="Path to input.csv")
    parser.add_argument("--creds", default="git_login.json", help="Path to git login JSON")
    parser.add_argument("--output", default="output.csv", help="Path to output.csv")
    parser.add_argument("--remote", default="origin", help="Remote name (default: origin)")
    parser.add_argument(
        "--search-root",
        default=None,
        help="Root directory to search under when input.csv uses a 'repo' name column "
             "instead of 'repo_path'. Overrides 'search_root' in git_login.json if given. "
             "Falls back to current directory if neither is set.",
    )
    args = parser.parse_args()

    try:
        creds = load_credentials(args.creds)
    except Exception as e:
        print(f"Error loading credentials: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        rows_in = load_rows(args.input)
    except Exception as e:
        print(f"Error loading input.csv: {e}", file=sys.stderr)
        sys.exit(1)

    search_root = args.search_root or creds.get("search_root") or "."
    repo_path_overrides = creds.get("repo_paths", {})

    output_rows = []
    for row in rows_in:
        identifier, mode, branch = row["identifier"], row["mode"], row["branch"]
        result_row = {"repo_path": identifier, "branch": branch}

        if mode == "search":
            if identifier in repo_path_overrides:
                repo_path = repo_path_overrides[identifier]
                print(f"Using overridden path for '{identifier}': {repo_path}")
            else:
                print(f"Searching for repo '{identifier}' under {search_root}...")
                repo_path, matches = find_repo_path(search_root, identifier)
                if repo_path is None:
                    if not matches:
                        err = f"no repo named '{identifier}' found under {search_root}"
                    else:
                        err = f"ambiguous: found {len(matches)} repos named '{identifier}': {matches}"
                    result_row.update(
                        {"merge_owner": "", "merge_message": f"ERROR: {err}",
                         "merged_branch": "", "merge_commit": "", "merged_at": ""}
                    )
                    output_rows.append(result_row)
                    continue
            result_row["repo_path"] = repo_path
        else:
            repo_path = identifier

        print(f"Processing {repo_path} -> branch '{branch}'...")

        original_remote_url = None
        try:
            set_local_identity(repo_path, creds)
            original_remote_url = inject_token_into_remote(repo_path, args.remote, creds.get("token"))
            fetch_and_reset(repo_path, branch, remote=args.remote)
            info = get_last_merge_info(repo_path)
            if "error" in info:
                result_row.update(
                    {"merge_owner": "", "merge_message": f"ERROR: {info['error']}",
                     "merged_branch": "", "merge_commit": "", "merged_at": ""}
                )
            else:
                result_row.update(info)
        except Exception as e:
            result_row.update(
                {"merge_owner": "", "merge_message": f"ERROR: {e}",
                 "merged_branch": "", "merge_commit": "", "merged_at": ""}
            )
        finally:
            if original_remote_url:
                restore_remote(repo_path, args.remote, original_remote_url)

        output_rows.append(result_row)

    with open(args.output, "w", newline="") as f:
        fieldnames = ["repo_path", "branch", "merge_owner", "merge_message",
                      "merged_branch", "merge_commit", "merged_at"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(output_rows)

    print(f"\nDone. Wrote {len(output_rows)} rows to {args.output}")


if __name__ == "__main__":
    main()
