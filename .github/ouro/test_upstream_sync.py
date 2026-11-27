"""Offline tests for upstream_sync.py, using throwaway git repositories.

Run: python3 -m unittest discover -s .github/ouro -p 'test_*.py'
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import upstream_sync  # noqa: E402

DAY = 86400
NOW = 1_800_000_000


def git(repo, *args, env=None):
    full_env = dict(os.environ)
    full_env.update(
        {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        }
    )
    if env:
        full_env.update(env)
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=full_env,
    ).stdout.strip()


class Fixture:
    def __init__(self, tmp):
        self.repo = Path(tmp) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "upstream")

    def commit(self, name, age_days):
        stamp = f"@{NOW - int(age_days * DAY)} +0000"
        (self.repo / name).write_text(name)
        git(self.repo, "add", name)
        git(
            self.repo,
            "commit",
            "-q",
            "-m",
            name,
            env={"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp},
        )
        return git(self.repo, "rev-parse", "HEAD")

    def mirror_at(self, sha):
        git(self.repo, "branch", "-f", "mirror", sha)

    def plan(self, min_age_days=7):
        return upstream_sync.plan(
            repo_dir=str(self.repo),
            upstream_ref="upstream",
            mirror_ref="mirror",
            min_age_days=min_age_days,
            now=NOW,
        )


class PlanTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = Fixture(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_fast_forward_to_newest_commit_old_enough(self):
        a = self.fx.commit("a", 30)
        b = self.fx.commit("b", 10)
        self.fx.commit("c", 3)
        self.fx.mirror_at(a)
        self.assertEqual(self.fx.plan(), {"status": "fast_forward", "target": b, "mirror": a})

    def test_commit_exactly_at_cutoff_counts_as_old_enough(self):
        a = self.fx.commit("a", 30)
        b = self.fx.commit("b", 7)
        self.fx.mirror_at(a)
        self.assertEqual(self.fx.plan()["target"], b)

    def test_too_new_when_only_new_commits_remain(self):
        a = self.fx.commit("a", 30)
        self.fx.commit("b", 2)
        self.fx.mirror_at(a)
        self.assertEqual(self.fx.plan(), {"status": "too_new", "target": None, "mirror": a})

    def test_up_to_date_when_mirror_equals_upstream(self):
        # Mirror was synced by hand to a commit newer than the cutoff.
        self.fx.commit("a", 30)
        b = self.fx.commit("b", 2)
        self.fx.mirror_at(b)
        self.assertEqual(self.fx.plan(), {"status": "up_to_date", "target": None, "mirror": b})

    def test_too_new_when_mirror_is_ahead_of_the_cutoff(self):
        self.fx.commit("a", 30)
        b = self.fx.commit("b", 2)
        self.fx.commit("c", 1)
        self.fx.mirror_at(b)
        self.assertEqual(self.fx.plan()["status"], "too_new")

    def test_too_new_when_no_upstream_commit_is_old_enough(self):
        a = self.fx.commit("a", 3)
        self.fx.commit("b", 1)
        self.fx.mirror_at(a)
        self.assertEqual(self.fx.plan(), {"status": "too_new", "target": None, "mirror": a})

    def test_backdated_commit_does_not_pull_in_a_new_one(self):
        # b is 1 day old; c claims 20 days. Taking c would bring b in early.
        a = self.fx.commit("a", 30)
        self.fx.commit("b", 1)
        self.fx.commit("c", 20)
        self.fx.mirror_at(a)
        self.assertEqual(self.fx.plan()["status"], "too_new")

    def test_stops_before_the_first_new_commit(self):
        a = self.fx.commit("a", 30)
        b = self.fx.commit("b", 10)
        self.fx.commit("c", 1)
        self.fx.commit("d", 20)
        self.fx.mirror_at(a)
        self.assertEqual(self.fx.plan(), {"status": "fast_forward", "target": b, "mirror": a})

    def _stray_mirror(self):
        base = self.fx.commit("a", 30)
        self.fx.commit("b", 20)
        git(self.fx.repo, "checkout", "-q", "-b", "side", base)
        stray = self.fx.commit("stray", 15)
        git(self.fx.repo, "checkout", "-q", "upstream")
        self.fx.mirror_at(stray)
        return stray

    def test_diverged_mirror_is_reported(self):
        stray = self._stray_mirror()
        result = self.fx.plan()
        self.assertEqual(result["status"], "diverged")
        self.assertEqual(result["mirror"], stray)

    def test_diverged_when_mirror_is_upstream_plus_a_stray_commit(self):
        self.fx.commit("a", 30)
        b = self.fx.commit("b", 20)
        git(self.fx.repo, "checkout", "-q", "-b", "side", b)
        stray = self.fx.commit("stray", 15)
        git(self.fx.repo, "checkout", "-q", "upstream")
        self.fx.mirror_at(stray)
        self.assertEqual(self.fx.plan()["status"], "diverged")

    def test_diverged_when_off_upstream_and_everything_is_new(self):
        base = self.fx.commit("a", 3)
        self.fx.commit("b", 2)
        git(self.fx.repo, "checkout", "-q", "-b", "side", base)
        stray = self.fx.commit("stray", 1)
        git(self.fx.repo, "checkout", "-q", "upstream")
        self.fx.mirror_at(stray)
        self.assertEqual(self.fx.plan()["status"], "diverged")

    def test_merge_commit_date_gates_its_side_branch(self):
        # A merged side branch with an old commit must not come in while the
        # merge commit itself is too new.
        a = self.fx.commit("a", 30)
        git(self.fx.repo, "checkout", "-q", "-b", "side")
        self.fx.commit("side", 20)
        git(self.fx.repo, "checkout", "-q", "upstream")
        stamp = f"@{NOW - 1 * DAY} +0000"
        git(
            self.fx.repo,
            "merge",
            "-q",
            "--no-ff",
            "-m",
            "merge",
            "side",
            env={"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp},
        )
        self.fx.mirror_at(a)
        self.assertEqual(self.fx.plan()["status"], "too_new")

    def test_mirror_on_a_merged_side_branch_fast_forwards_past_the_merge(self):
        a = self.fx.commit("a", 30)
        git(self.fx.repo, "checkout", "-q", "-b", "side")
        side = self.fx.commit("side", 25)
        git(self.fx.repo, "checkout", "-q", "upstream")
        self.fx.commit("main2", 24)
        stamp = f"@{NOW - 20 * DAY} +0000"
        git(self.fx.repo, "merge", "-q", "--no-ff", "-m", "merge", "side",
            env={"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp})
        merge = git(self.fx.repo, "rev-parse", "HEAD")
        self.fx.mirror_at(side)
        self.assertEqual(self.fx.plan(), {"status": "fast_forward", "target": merge, "mirror": side})
        del a

    def test_mirror_on_a_side_branch_waits_for_its_merge(self):
        # The commit before the merge is old enough but does not contain the
        # mirror; the merge is too new. Nothing may be planned.
        self.fx.commit("a", 30)
        git(self.fx.repo, "checkout", "-q", "-b", "side")
        side = self.fx.commit("side", 25)
        git(self.fx.repo, "checkout", "-q", "upstream")
        self.fx.commit("main2", 24)
        stamp = f"@{NOW - 1 * DAY} +0000"
        git(self.fx.repo, "merge", "-q", "--no-ff", "-m", "merge", "side",
            env={"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp})
        self.fx.mirror_at(side)
        self.assertEqual(self.fx.plan(), {"status": "too_new", "target": None, "mirror": side})

    def test_unknown_ref_raises(self):
        a = self.fx.commit("a", 30)
        self.fx.mirror_at(a)
        with self.assertRaises(upstream_sync.SyncError):
            upstream_sync.plan(str(self.fx.repo), "missing", "mirror", 7, NOW)

    def test_negative_age_is_rejected(self):
        a = self.fx.commit("a", 30)
        self.fx.mirror_at(a)
        with self.assertRaises(upstream_sync.SyncError):
            self.fx.plan(min_age_days=-1)


class CliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = Fixture(self._tmp.name)
        self.a = self.fx.commit("a", 30)
        self.b = self.fx.commit("b", 10)

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, mirror):
        self.fx.mirror_at(mirror)
        out = Path(self._tmp.name) / "out.txt"
        code = upstream_sync.main(
            [
                "--repo-dir",
                str(self.fx.repo),
                "--upstream-ref",
                "upstream",
                "--mirror-ref",
                "mirror",
                "--now",
                str(NOW),
                "--github-output",
                str(out),
            ]
        )
        return code, out.read_text() if out.exists() else ""

    def test_cli_writes_outputs_and_exits_zero(self):
        code, out = self.run_cli(self.a)
        self.assertEqual(code, 0)
        self.assertIn("status=fast_forward\n", out)
        self.assertIn(f"target={self.b}\n", out)

    def test_cli_exits_one_when_diverged(self):
        git(self.fx.repo, "checkout", "-q", "-b", "side", self.a)
        stray = self.fx.commit("stray", 15)
        code, out = self.run_cli(stray)
        self.assertEqual(code, 1)
        self.assertIn("status=diverged\n", out)

    def test_cli_exits_one_on_error_without_outputs(self):
        out = Path(self._tmp.name) / "out.txt"
        code = upstream_sync.main(
            ["--repo-dir", str(self.fx.repo), "--upstream-ref", "nope", "--mirror-ref", "upstream",
             "--github-output", str(out)]
        )
        self.assertEqual(code, 1)
        self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
