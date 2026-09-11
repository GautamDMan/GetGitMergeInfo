#!/usr/bin/env python3
"""
For each file name listed in input.csv, searches for that file inside a
single local git repository, then reads the file's git history to find the
last merge commit that touched it - reporting who merged it, the merge
message, and the branch it came from. No fetch or reset is performed; the
repo is read exactly as it currently sits on disk.

Usage:
    python get_last_merge_info.py --input input.csv --creds git_login.json --output output.csv

input.csv format:
    file_name
    config.py
    utils.py
    README.md

git_login.json format:
    {
        "repo_path": "/path/to/the/repo",
        "branch": "main"    // optional - if given, the repo is hard-reset to this branch first
    }

output.csv format:
    file_name,resolved_path,merge_owner,merge_message,merged_branch,merge_commit,merged_at
"""

import argparse
import csv
import json
import os
import re
import subprocess
import sys

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
        creds = json.load(f)
    repo_path = creds.get("repo_path")
    if not repo_path:
        raise ValueError("git_login.json must contain a 'repo_path' field")
    if not os.path.isdir(os.path.join(repo_path, ".git")):
        raise ValueError(f"'{repo_path}' does not look like a git repository (no .git folder)")
    branch = creds.get("branch")  # optional
    return repo_path, branch


def hard_reset_to_branch(repo_path, branch):
    """Checks out the given branch and hard-resets the working tree to it,
    discarding any local changes/commits that aren't on that branch tip."""
    run_git(repo_path, ["checkout", branch])
    run_git(repo_path, ["reset", "--hard", branch])


def load_file_names(input_csv):
    names = []
    with open(input_csv, "r", newline="") as f:
        reader = csv.DictReader(f)
        if "file_name" not in (reader.fieldnames or []):
            raise ValueError(
                f"input.csv must have a 'file_name' column. Found: {reader.fieldnames}"
            )
        for row in reader:
            name = row["file_name"].strip()
            if name:
                names.append(name)
    return names


def find_file_in_repo(repo_path, file_name):
    """Search repo_path for a file named file_name (excluding .git internals).
    Returns (relative_path_or_None, list_of_all_relative_matches)."""
    matches = []
    for dirpath, dirnames, filenames in os.walk(repo_path):
        if ".git" in dirnames:
            dirnames.remove(".git")
        if file_name in filenames:
            full_path = os.path.join(dirpath, file_name)
            rel_path = os.path.relpath(full_path, repo_path)
            matches.append(rel_path)
    if len(matches) == 1:
        return matches[0], matches
    return None, matches


def get_last_merge_info_for_file(repo_path, rel_file_path):
    """Find the most recent merge commit that touched rel_file_path and parse
    owner/message/branch from it. Uses --full-history alongside --merges
    because git's default path-based history simplification hides merge
    commits whose result is identical to one parent (the common case for a
    clean, non-conflicting merge) - without it, real merges get missed."""
    log_format = "%H%x01%an%x01%s%x01%cI"
    output = run_git(
        repo_path,
        ["log", "--merges", "--full-history", "-1", f"--pretty=format:{log_format}", "--", rel_file_path],
        check=False,
    )
    if not output:
        return {"error": "no merge commit found that touched this file"}

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
    parser = argparse.ArgumentParser(
        description="Find last merge info for each file name listed in input.csv"
    )
    parser.add_argument("--input", default="input.csv", help="Path to input.csv")
    parser.add_argument("--creds", default="git_login.json", help="Path to git_login.json")
    parser.add_argument("--output", default="output.csv", help="Path to output.csv")
    args = parser.parse_args()

    try:
        repo_path, branch = load_credentials(args.creds)
    except Exception as e:
        print(f"Error loading credentials: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        file_names = load_file_names(args.input)
    except Exception as e:
        print(f"Error loading input.csv: {e}", file=sys.stderr)
        sys.exit(1)

    if branch:
        print(f"Hard-resetting {repo_path} to branch '{branch}'...")
        try:
            hard_reset_to_branch(repo_path, branch)
        except Exception as e:
            print(f"Error resetting to branch '{branch}': {e}", file=sys.stderr)
            sys.exit(1)

    output_rows = []
    for file_name in file_names:
        print(f"Searching for '{file_name}' in {repo_path}...")
        result_row = {"file_name": file_name, "resolved_path": ""}

        rel_path, matches = find_file_in_repo(repo_path, file_name)
        if rel_path is None:
            if not matches:
                err = f"file '{file_name}' not found in repo"
            else:
                err = f"ambiguous: found {len(matches)} files named '{file_name}': {matches}"
            result_row.update(
                {"merge_owner": "", "merge_message": f"ERROR: {err}",
                 "merged_branch": "", "merge_commit": "", "merged_at": ""}
            )
            output_rows.append(result_row)
            continue

        result_row["resolved_path"] = rel_path
        try:
            info = get_last_merge_info_for_file(repo_path, rel_path)
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

        output_rows.append(result_row)

    with open(args.output, "w", newline="") as f:
        fieldnames = ["file_name", "resolved_path", "merge_owner", "merge_message",
                      "merged_branch", "merge_commit", "merged_at"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(output_rows)

    print(f"\nDone. Wrote {len(output_rows)} rows to {args.output}")


if __name__ == "__main__":
    main()
