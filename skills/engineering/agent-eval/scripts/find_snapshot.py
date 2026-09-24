#!/usr/bin/env python3
"""Find the commit a checkout was actually on at a given moment.

The checkout's own HEAD reflog is the evidence: the last move at or before the
moment is the snapshot. The first-parent `origin/master` guess is printed only as
a fallback and is often wrong (a checkout lags or leads the remote).

    python find_snapshot.py --checkout ~/projects/myrepo --at 2026-09-23T16:37:43Z
"""
import argparse
import datetime as dt
import subprocess


def parse(ts):
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--checkout", required=True)
    ap.add_argument("--at", required=True, help="ISO time of the correction, e.g. 2026-09-23T16:37:43Z")
    ap.add_argument("--remote-ref", default="refs/remotes/origin/master")
    args = ap.parse_args()
    at = parse(args.at)
    log = subprocess.run(["git", "-C", args.checkout, "reflog", "show", "HEAD", "--date=iso-strict",
                          "--format=%H %gd %gs"], capture_output=True, text=True).stdout.splitlines()
    moves = []
    for line in log:
        sha, rest = line.split(" ", 1)
        stamp = rest[rest.index("{") + 1:rest.index("}")]
        moves.append((parse(stamp), sha, rest[rest.index("}") + 2:]))
    before = [m for m in moves if m[0] <= at]
    after = [m for m in moves if m[0] > at]
    if before:
        t, sha, what = max(before)
        print(f"snapshot  {sha}  (HEAD since {t.isoformat()}: {what})")
    else:
        print("snapshot  UNKNOWN: reflog has no entry before that moment")
    if after:
        t, sha, what = min(after)
        print(f"next move {sha[:12]} at {t.isoformat()}: {what}")
    guess = subprocess.run(["git", "-C", args.checkout, "rev-list", "-1", "--first-parent",
                            f"--before={args.at}", args.remote_ref], capture_output=True, text=True).stdout.strip()
    print(f"fallback  {guess or 'none'}  (first-parent {args.remote_ref} before that time; not evidence)")


if __name__ == "__main__":
    main()
