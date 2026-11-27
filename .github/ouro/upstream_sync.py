"""Decide how far the Ourio `main` mirror may fast-forward towards Rio.

Branch strategy D (E337): `main` mirrors raphamorim/rio and only ever
fast-forwards; `ouro/main` is the product branch. This script only reads
git state and prints a decision; the workflow does the push.

The target is the last commit of the run of Rio first-parent commits after
the mirror whose committer dates are all at least `--min-age-days` old (the
7-day cooldown that matches the Cargo dependency cooldown). The first commit
that is too new ends the run, even if a later one claims an older date.

Statuses:
  fast_forward  mirror is an ancestor of the target: push target to main
  up_to_date    mirror equals Rio's main
  too_new       the next Rio commit is not old enough yet
  diverged      mirror has commits upstream does not: fail, never rewrite

Exit code 0 for every status except `diverged` and errors (exit 1).
"""

import argparse
import json
import subprocess
import sys
import time

DAY = 86400


class SyncError(Exception):
    """A git lookup failed or an argument is invalid."""


def _git(repo_dir, *args):
    proc = subprocess.run(
        ["git", "-C", repo_dir, *args], capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise SyncError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _is_ancestor(repo_dir, older, newer):
    proc = subprocess.run(
        ["git", "-C", repo_dir, "merge-base", "--is-ancestor", older, newer],
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    raise SyncError(f"git merge-base --is-ancestor: {proc.stderr.strip()}")


def _resolve(repo_dir, ref):
    return _git(repo_dir, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")


def plan(repo_dir, upstream_ref, mirror_ref, min_age_days, now):
    """Return {"status", "target", "mirror"} for the given refs."""
    if min_age_days < 0:
        raise SyncError("min_age_days must not be negative")
    upstream = _resolve(repo_dir, upstream_ref)
    mirror = _resolve(repo_dir, mirror_ref)
    cutoff = now - min_age_days * DAY

    if not _is_ancestor(repo_dir, mirror, upstream):
        return {"status": "diverged", "target": None, "mirror": mirror}
    if mirror == upstream:
        return {"status": "up_to_date", "target": None, "mirror": mirror}

    # Walk Rio's first-parent history forward from the mirror and stop at the
    # first commit that is too new: a later commit with an older (backdated)
    # committer date must not pull a new commit in before its cooldown.
    target = None
    log = _git(
        repo_dir, "log", "--first-parent", "--reverse", "--format=%H %ct", f"{mirror}..{upstream}"
    )
    for line in log.splitlines():
        sha, _, stamp = line.partition(" ")
        if int(stamp) > cutoff:
            break
        if _is_ancestor(repo_dir, mirror, sha):
            target = sha

    if target is None:
        return {"status": "too_new", "target": None, "mirror": mirror}
    return {"status": "fast_forward", "target": target, "mirror": mirror}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Decide how far the Ourio main mirror may fast-forward.")
    parser.add_argument("--repo-dir", default=".")
    parser.add_argument("--upstream-ref", required=True)
    parser.add_argument("--mirror-ref", required=True)
    parser.add_argument("--min-age-days", type=int, default=7)
    parser.add_argument("--now", type=int, default=None, help="epoch seconds (tests)")
    parser.add_argument("--github-output", default=None, help="file for key=value outputs")
    args = parser.parse_args(argv)

    now = args.now if args.now is not None else int(time.time())
    try:
        result = plan(args.repo_dir, args.upstream_ref, args.mirror_ref, args.min_age_days, now)
    except SyncError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1

    print(json.dumps(result))
    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as out:
            out.write(f"status={result['status']}\n")
            out.write(f"target={result['target'] or ''}\n")
            out.write(f"mirror={result['mirror']}\n")
    if result["status"] == "diverged":
        print("error: main has commits Rio does not; fix by hand, never force-push", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
