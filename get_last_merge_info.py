#!/usr/bin/env python3
"""
Optimized bulk-scan version of get_latest_file_changes.py.

For each file name listed in input.csv, resolves the file at the configured
Git ref and reports the latest commit that changed it. Commit history is read
once for all requested paths, rather than running one `git log` per file.

Usage:
    python get_latest_file_changes.py \
        --input input.csv \
        --creds git_login.json \
        --output output.csv

input.csv:
    file_name
    config.py
    utils.py

The file_name value may be either:
  * a basename, such as config.py; every matching path is included
  * a repository-relative path, such as src/config.py

git_login.json:
    {
      "repo_path": "/path/to/the/repo",
      "branch": "main"
    }

The branch field is optional. HEAD is used when it is omitted.

output.csv columns:
    file_name, resolved_path, last_commit, last_commit_message,
    last_modified_by, last_modified_email, last_modified_at

Behavior:
  * The tracked file tree is indexed once with `git ls-tree`.
  * Commit history is streamed once with `git log --name-only`.
  * The first occurrence of a requested path is its latest change.
  * Scanning stops as soon as all requested paths have been resolved.
  * The repository is read-only: no checkout, pull, fetch, or reset is done.
  * Bulk mode reports history for the current path. It does not trace a file
    through an older pre-rename path because Git supports --follow for only one
    path at a time.
"""

import argparse
import csv
import json
import os
import subprocess
import sys
from collections import defaultdict

SEP = "\x01"
MARK = "\x02"

FIELDNAMES = [
    "file_name",
    "resolved_path",
    "last_commit",
    "last_commit_message",
    "last_modified_by",
    "last_modified_email",
    "last_modified_at",
]

EMPTY_INFO = {key: "" for key in FIELDNAMES[2:]}


def git_cmd(repo_path, args):
    """Build a Git command that keeps non-ASCII paths readable."""
    return ["git", "-C", repo_path, "-c", "core.quotepath=false"] + args


def run_git(repo_path, args, stdin_text=None, check=True):
    """Run Git and return stdout as UTF-8 text."""
    result = subprocess.run(
        git_cmd(repo_path, args),
        input=stdin_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    if check and result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed: {result.stderr.strip()}"
        )

    return result.stdout


def load_credentials(creds_path):
    """Load and validate repository settings."""
    with open(creds_path, "r", encoding="utf-8") as file_handle:
        creds = json.load(file_handle)

    repo_path = creds.get("repo_path")
    if not repo_path:
        raise ValueError("git_login.json must contain a 'repo_path' field")

    repo_path = os.path.abspath(os.path.expanduser(repo_path))
    if not os.path.isdir(repo_path):
        raise ValueError(f"'{repo_path}' is not a directory")

    inside_work_tree = run_git(
        repo_path,
        ["rev-parse", "--is-inside-work-tree"],
        check=False,
    ).strip()

    if inside_work_tree != "true":
        raise ValueError(f"'{repo_path}' does not look like a Git repository")

    branch = creds.get("branch")
    return repo_path, branch


def validate_ref(repo_path, ref):
    """Verify that the configured branch, tag, or commit exists."""
    result = subprocess.run(
        git_cmd(repo_path, ["rev-parse", "--verify", f"{ref}^{{commit}}"]),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    if result.returncode != 0:
        raise ValueError(
            f"Git ref '{ref}' could not be resolved: {result.stderr.strip()}"
        )


def load_file_names(input_csv):
    """Load non-empty values from the file_name CSV column."""
    names = []

    with open(input_csv, "r", newline="", encoding="utf-8-sig") as file_handle:
        reader = csv.DictReader(file_handle)

        if "file_name" not in (reader.fieldnames or []):
            raise ValueError(
                "input.csv must have a 'file_name' column. "
                f"Found: {reader.fieldnames}"
            )

        for row in reader:
            name = (row.get("file_name") or "").strip()
            if name:
                names.append(name)

    return names


def build_file_indexes(repo_path, ref):
    """Return the tracked path set and basename-to-path index at ref."""
    output = run_git(
        repo_path,
        ["ls-tree", "-r", "--name-only", "-z", ref],
    )

    tracked_paths = set()
    basename_index = defaultdict(list)

    for path in output.split("\0"):
        if not path:
            continue

        tracked_paths.add(path)
        basename_index[path.rsplit("/", 1)[-1]].append(path)

    return tracked_paths, basename_index


def normalize_input_path(value):
    """Normalize user-provided repository paths without touching the file system."""
    normalized = value.strip().replace("\\", "/")

    while normalized.startswith("./"):
        normalized = normalized[2:]

    return normalized.strip("/")


def resolve_input_files(file_names, tracked_paths, basename_index):
    """Resolve each unique CSV value to all matching tracked paths."""
    errors = {}
    resolved = {}

    for input_name in dict.fromkeys(file_names):
        normalized = normalize_input_path(input_name)

        if not normalized:
            errors[input_name] = error_row(input_name, "", "empty file name")
            continue

        # A value containing a directory component is treated as an exact
        # repository-relative path. A basename expands to every matching path.
        if "/" in normalized:
            if normalized in tracked_paths:
                resolved[input_name] = [normalized]
            else:
                errors[input_name] = error_row(
                    input_name,
                    normalized,
                    f"file path '{normalized}' not found at the selected Git ref",
                )
            continue

        matches = sorted(basename_index.get(normalized, []))

        if not matches:
            errors[input_name] = error_row(
                input_name,
                "",
                f"file '{input_name}' not found at the selected Git ref",
            )
        else:
            resolved[input_name] = matches

    return errors, resolved


def find_latest_changes(repo_path, ref, target_paths):
    """
    Return the newest commit information for every target path.

    Git emits commits newest-first. Once a target path appears beneath a commit
    header, that commit is the latest change for the path. The process stops as
    soon as all requested paths are found.
    """
    remaining = set(target_paths)
    found = {}

    if not remaining:
        return found

    pretty_format = (
        f"{MARK}%H{SEP}%an{SEP}%ae{SEP}%s{SEP}%cI"
    )

    process = subprocess.Popen(
        git_cmd(
            repo_path,
            [
                "log",
                ref,
                "--name-only",
                "--no-renames",
                "--date-order",
                f"--pretty=format:{pretty_format}",
                "--",
            ],
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    current_commit = None
    stderr_text = ""

    try:
        if process.stdout is None:
            raise RuntimeError("Unable to read Git history output")

        for raw_line in process.stdout:
            line = raw_line.rstrip("\r\n")

            if line.startswith(MARK):
                parts = line[1:].split(SEP, 4)
                current_commit = tuple(parts) if len(parts) == 5 else None
                continue

            if not line or current_commit is None:
                continue

            if line in remaining:
                found[line] = current_commit
                remaining.remove(line)

                if not remaining:
                    break
    finally:
        if process.poll() is None:
            process.terminate()

        if process.stdout is not None:
            process.stdout.close()

        try:
            _, stderr_text = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            _, stderr_text = process.communicate()

    # A non-zero return code is expected when we terminate early after finding
    # all paths. Treat it as an error only when the scan did not complete early.
    if remaining and process.returncode not in (0, None):
        raise RuntimeError(
            f"git log failed: {(stderr_text or '').strip()}"
        )

    return found


def error_row(file_name, resolved_path, message):
    """Create a CSV row containing an error in the message column."""
    row = {
        "file_name": file_name,
        "resolved_path": resolved_path,
        **EMPTY_INFO,
    }
    row["last_commit_message"] = f"ERROR: {message}"
    return row


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Find the latest Git commit for every file listed in input.csv "
            "using one bulk history scan"
        )
    )

    parser.add_argument(
        "--input",
        default="input.csv",
        help="Path to input.csv",
    )
    parser.add_argument(
        "--creds",
        default="git_login.json",
        help="Path to git_login.json",
    )
    parser.add_argument(
        "--output",
        default="output.csv",
        help="Path to output.csv",
    )
    args = parser.parse_args()

    try:
        repo_path, branch = load_credentials(args.creds)
        file_names = load_file_names(args.input)
    except Exception as exc:
        print(f"Input error: {exc}", file=sys.stderr)
        sys.exit(1)

    ref = branch or "HEAD"

    try:
        validate_ref(repo_path, ref)
    except Exception as exc:
        print(f"Reference error: {exc}", file=sys.stderr)
        sys.exit(1)

    print(
        f"Reading history of '{ref}' in {repo_path} "
        "(read-only, bulk scan)..."
    )

    try:
        tracked_paths, basename_index = build_file_indexes(repo_path, ref)
    except Exception as exc:
        print(f"Error reading tree at '{ref}': {exc}", file=sys.stderr)
        sys.exit(1)

    errors, resolved = resolve_input_files(
        file_names,
        tracked_paths,
        basename_index,
    )

    unique_target_paths = {
        path
        for paths in resolved.values()
        for path in paths
    }
    print(f"Scanning commit history for {len(unique_target_paths)} unique path(s)...")

    try:
        changes = find_latest_changes(repo_path, ref, unique_target_paths)
    except Exception as exc:
        print(f"Error scanning Git history: {exc}", file=sys.stderr)
        sys.exit(1)

    rows_by_input = {}
    for input_name, paths in resolved.items():
        output_rows = []

        for rel_path in paths:
            change = changes.get(rel_path)

            if change is None:
                output_rows.append(
                    error_row(
                        input_name,
                        rel_path,
                        "no commit history found for this path",
                    )
                )
                continue

            commit_hash, author, email, subject, committed_at = change
            output_rows.append(
                {
                    "file_name": input_name,
                    "resolved_path": rel_path,
                    "last_commit": commit_hash,
                    "last_commit_message": subject,
                    "last_modified_by": author,
                    "last_modified_email": email,
                    "last_modified_at": committed_at,
                }
            )

        rows_by_input[input_name] = output_rows

    output_rows = []
    for input_name in file_names:
        if input_name in errors:
            output_rows.append(errors[input_name])
        else:
            output_rows.extend(rows_by_input[input_name])

    try:
        with open(args.output, "w", newline="", encoding="utf-8") as file_handle:
            writer = csv.DictWriter(file_handle, fieldnames=FIELDNAMES)
            writer.writeheader()
            writer.writerows(output_rows)
    except Exception as exc:
        print(f"Error writing output CSV: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Done. Wrote {len(output_rows)} row(s) to {args.output}")


if __name__ == "__main__":
    main()
