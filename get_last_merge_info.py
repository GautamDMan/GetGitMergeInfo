#!/usr/bin/env python3
"""
Optimized version of get_last_merge_info.py

For each file name listed in input.csv, finds that file inside a single local
git repository and reports the last merge commit that touched it: who merged
it, who pushed the code, the merge message, and the branch it came from.

What changed vs. the original (same CLI, same output columns):
  * The file tree is indexed ONCE with `git ls-tree` (no os.walk per file).
  * History is scanned in ONE streaming `git log --first-parent -m --name-only`
    pass for all files, and stops early as soon as every file has been resolved
    (original: one path-filtered `git log` per file).
  * "pushed_by" lookups are batched into a single git call
    (original: 2 git calls per file).
  * "branch_merges" is built from ONE `git log --merges --all` pass
    (original: one full pass per distinct branch).
  * Read-only by default: no checkout / `reset --hard`. If "branch" is given
    in git_login.json it is simply used as the ref to read, so uncommitted
    work in the repo is never discarded.

Usage:
    python get_last_merge_info.py --input input.csv --creds git_login.json --output output.csv

input.csv:
    file_name
    config.py
    utils.py

git_login.json:
    {
      "repo_path": "/path/to/the/repo",
      "branch": "main"        // optional - ref whose history is read (default: HEAD)
    }

output.csv columns:
    file_name, resolved_path, merge_owner, pushed_by, pushed_by_email,
    merge_message, merged_branch, merge_commit, merged_at, branch_merges

Optional one-time speedup for very large repos (Bloom filters for path queries):
    git commit-graph write --reachable --changed-paths
"""

import argparse
import csv
import json
import os
import re
import subprocess
import sys
from collections import defaultdict

SEP = "\x01"      # field separator inside git --pretty formats
MARK = "\x02"     # marks a commit header line in the streamed log

# "Merge pull request #123 from owner/branch-name"
PR_MERGE_RE = re.compile(r"Merge pull request #\d+ from ([^\s/]+)/(\S+)")
# "Merge branch 'branch-name' into target"
BRANCH_MERGE_RE = re.compile(r"Merge branch '([^']+)'")

FIELDNAMES = [
    "file_name", "resolved_path", "merge_owner", "pushed_by", "pushed_by_email",
    "merge_message", "merged_branch", "merge_commit", "merged_at", "branch_merges",
]
EMPTY_INFO = {k: "" for k in FIELDNAMES[2:]}


# --------------------------------------------------------------------------- #
# git helpers
# --------------------------------------------------------------------------- #
def git_cmd(repo_path, args):
    # quotepath=false keeps non-ASCII file names readable instead of "\303\251"
    return ["git", "-C", repo_path, "-c", "core.quotepath=false"] + args


def run_git(repo_path, args, stdin_text=None, check=True):
    result = subprocess.run(
        git_cmd(repo_path, args),
        input=stdin_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def parse_subject(subject):
    """Return (pr_owner_or_None, merged_branch_or_'')."""
    m = PR_MERGE_RE.search(subject)
    if m:
        return m.group(1), m.group(2)
    m = BRANCH_MERGE_RE.search(subject)
    if m:
        return None, m.group(1)
    return None, ""


# --------------------------------------------------------------------------- #
# inputs
# --------------------------------------------------------------------------- #
def load_credentials(creds_path):
    with open(creds_path, "r", encoding="utf-8") as f:
        creds = json.load(f)
    repo_path = creds.get("repo_path")
    if not repo_path:
        raise ValueError("git_login.json must contain a 'repo_path' field")
    if not os.path.isdir(repo_path):
        raise ValueError(f"'{repo_path}' is not a directory")
    out = run_git(repo_path, ["rev-parse", "--is-inside-work-tree"], check=False).strip()
    if out != "true":
        raise ValueError(f"'{repo_path}' does not look like a git repository")
    return repo_path, creds.get("branch")  # branch is optional


def load_file_names(input_csv):
    names = []
    with open(input_csv, "r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if "file_name" not in (reader.fieldnames or []):
            raise ValueError(
                f"input.csv must have a 'file_name' column. Found: {reader.fieldnames}"
            )
        for row in reader:
            name = (row["file_name"] or "").strip()
            if name:
                names.append(name)
    return names


# --------------------------------------------------------------------------- #
# step 1: index tracked files once
# --------------------------------------------------------------------------- #
def build_name_index(repo_path, ref):
    """basename -> [repo-relative paths] for every file tracked at `ref`."""
    out = run_git(repo_path, ["ls-tree", "-r", "--name-only", "-z", ref])
    index = defaultdict(list)
    for path in out.split("\0"):
        if path:
            index[path.rsplit("/", 1)[-1]].append(path)
    return index


# --------------------------------------------------------------------------- #
# step 2: one streaming pass over merge history for all target paths
# --------------------------------------------------------------------------- #
def find_last_merges(repo_path, ref, target_paths):
    """Return {path: (hash, parents, author, subject, committed_at)} for the most
    recent merge commit touching each path. Stops as soon as all are found.

    --first-parent walks only the branch's own line of history and, together
    with -m, diffs each merge against its FIRST parent only. So --name-only
    lists exactly the files whose content the merge brought into the branch.
    (Diffing against both parents would also list files merely changed on the
    branch itself, crediting the merge for changes it did not bring.)"""
    remaining = set(target_paths)
    found = {}
    if not remaining:
        return found

    fmt = f"{MARK}%H{SEP}%P{SEP}%an{SEP}%s{SEP}%cI"
    proc = subprocess.Popen(
        git_cmd(repo_path, ["log", ref, "--first-parent", "--merges", "-m",
                            "--name-only", f"--pretty=format:{fmt}"]),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        current = None
        for raw in proc.stdout:
            line = raw.rstrip("\n").rstrip("\r")
            if line.startswith(MARK):
                parts = line[1:].split(SEP)
                current = tuple(parts) if len(parts) == 5 else None
            elif line and current is not None and line in remaining:
                found[line] = current
                remaining.discard(line)
                if not remaining:
                    break  # everything resolved - no need to read older history
    finally:
        if proc.poll() is None:
            proc.terminate()
        proc.stdout.close()
        proc.wait()
    return found


# --------------------------------------------------------------------------- #
# step 3: batched "who pushed it" lookup
# --------------------------------------------------------------------------- #
def batch_pushed_by(repo_path, merges):
    """For each merge, the author of the merged-in branch tip (2nd parent);
    falls back to the merge commit itself if it has fewer than 2 parents.
    One git call for all of them. Returns {target_hash: (name, email)}."""
    targets = set()
    for commit_hash, parents, *_ in merges:
        ph = parents.split()
        targets.add(ph[1] if len(ph) >= 2 else commit_hash)
    if not targets:
        return {}
    out = run_git(
        repo_path,
        ["log", "--no-walk=unsorted", "--stdin", f"--pretty=format:%H{SEP}%an{SEP}%ae"],
        stdin_text="\n".join(sorted(targets)) + "\n",
        check=False,
    )
    result = {}
    for line in out.splitlines():
        parts = line.split(SEP)
        if len(parts) == 3:
            result[parts[0]] = (parts[1], parts[2])
    return result


# --------------------------------------------------------------------------- #
# step 4: branch -> all merges, from a single pass
# --------------------------------------------------------------------------- #
def build_branch_merges_map(repo_path):
    """{branch_name: '<short_hash> (<date>); ...'} across all merges in the repo."""
    out = run_git(
        repo_path,
        ["log", "--merges", "--all", f"--pretty=format:%H{SEP}%s{SEP}%cI"],
        check=False,
    )
    entries = defaultdict(list)
    for line in out.splitlines():
        parts = line.split(SEP)
        if len(parts) != 3:
            continue
        commit_hash, subject, committed_at = parts
        _, branch_name = parse_subject(subject)
        if branch_name:
            entries[branch_name].append(f"{commit_hash[:8]} ({committed_at})")
    return {b: "; ".join(v) for b, v in entries.items()}


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def error_row(file_name, resolved_path, message):
    row = {"file_name": file_name, "resolved_path": resolved_path, **EMPTY_INFO}
    row["merge_message"] = f"ERROR: {message}"
    return row


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

    ref = branch or "HEAD"
    print(f"Reading history of '{ref}' in {repo_path} (read-only, no checkout/reset)...")

    try:
        name_index = build_name_index(repo_path, ref)
    except Exception as e:
        print(f"Error reading tree at '{ref}': {e}", file=sys.stderr)
        sys.exit(1)

    # Resolve every input name to a unique path (or an error row).
    rows = {}            # file_name -> row dict (final or partially filled)
    to_lookup = {}       # file_name -> resolved relative path
    for file_name in dict.fromkeys(file_names):  # de-dupe, keep order
        matches = name_index.get(file_name, [])
        if not matches:
            rows[file_name] = error_row(file_name, "", f"file '{file_name}' not found in repo")
        elif len(matches) > 1:
            rows[file_name] = error_row(
                file_name, "",
                f"ambiguous: found {len(matches)} files named '{file_name}': {sorted(matches)}",
            )
        else:
            to_lookup[file_name] = matches[0]

    print(f"Scanning merge history for {len(to_lookup)} file(s)...")
    try:
        found = find_last_merges(repo_path, ref, set(to_lookup.values()))
    except Exception as e:
        print(f"Error scanning history: {e}", file=sys.stderr)
        sys.exit(1)

    pushed = batch_pushed_by(repo_path, list(found.values()))
    branch_map = build_branch_merges_map(repo_path) if found else {}

    for file_name, rel_path in to_lookup.items():
        merge = found.get(rel_path)
        if merge is None:
            rows[file_name] = error_row(
                file_name, rel_path, "no merge commit found that touched this file"
            )
            continue

        commit_hash, parents, author, subject, committed_at = merge
        pr_owner, merged_branch = parse_subject(subject)
        ph = parents.split()
        tip = ph[1] if len(ph) >= 2 else commit_hash
        pushed_name, pushed_email = pushed.get(tip, ("", ""))

        rows[file_name] = {
            "file_name": file_name,
            "resolved_path": rel_path,
            "merge_owner": pr_owner or author,
            "pushed_by": pushed_name,
            "pushed_by_email": pushed_email,
            "merge_message": subject,
            "merged_branch": merged_branch,
            "merge_commit": commit_hash,
            "merged_at": committed_at,
            "branch_merges": branch_map.get(merged_branch, "") if merged_branch else "",
        }

    # Write one row per input line, in the original order (duplicates repeated).
    with open(args.output, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows[name] for name in file_names)

    print(f"\nDone. Wrote {len(file_names)} rows to {args.output}")


if __name__ == "__main__":
    main()
