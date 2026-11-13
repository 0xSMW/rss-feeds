"""Exercise the Actions publisher against real repositories and a local bare origin."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PUBLISHER = Path(__file__).resolve().parents[1] / "scripts" / "publish_feeds.py"
BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "github-actions[bot]@users.noreply.github.com"
TRAILER = "Feed-Publication: rolling-v1"


class PublishFeedsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "checkout"
        self.origin = self.root / "origin.git"
        self.repo.mkdir()
        self.env = os.environ.copy()
        # Ignore developer/global identity, hooks, signing, and remote configuration.
        for key in list(self.env):
            if key.startswith("GIT_"):
                del self.env[key]
        self.env.update(
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_TERMINAL_PROMPT="0",
            GIT_ALLOW_PROTOCOL="file",
        )
        self.git("init", "--initial-branch=main")
        self.git("config", "user.name", "Human Maintainer")
        self.git("config", "user.email", "human@example.test")
        self.git("init", "--bare", "--initial-branch=main", str(self.origin))
        self.write("source.py", "# source v1\n")
        self.write("feeds/feed_example.xml", "<rss>initial</rss>\n")
        self.commit("Initial source")
        self.git("remote", "add", "origin", str(self.origin))
        self.git("push", "--set-upstream", "origin", "main")
        self.initial = self.git("rev-parse", "HEAD")

    def git(self, *args, cwd=None, env=None):
        result = subprocess.run(
            ["git", *args],
            cwd=cwd or self.repo,
            env=env or self.env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def write(self, name, value):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")

    def commit(self, message, *, author_bot=False, committer_bot=False):
        env = self.env.copy()
        if author_bot:
            env.update(GIT_AUTHOR_NAME=BOT_NAME, GIT_AUTHOR_EMAIL=BOT_EMAIL)
        if committer_bot:
            env.update(GIT_COMMITTER_NAME=BOT_NAME, GIT_COMMITTER_EMAIL=BOT_EMAIL)
        self.git("add", "--all")
        self.git("commit", "-m", message, env=env)
        return self.git("rev-parse", "HEAD")

    def publish(self, success=True):
        result = subprocess.run(
            [sys.executable, str(PUBLISHER)],
            cwd=self.repo,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        diagnostic = result.stdout + result.stderr
        if success:
            self.assertEqual(result.returncode, 0, diagnostic)
        else:
            self.assertNotEqual(result.returncode, 0, diagnostic)
        return result

    def remote_head(self):
        return self.git("--git-dir", str(self.origin), "rev-parse", "refs/heads/main")

    def assert_published(self, expected_parent, content):
        head = self.remote_head()
        self.assertEqual(self.git("rev-parse", head + "^"), expected_parent)
        self.assertEqual(self.git("show", head + ":feeds/feed_example.xml"), content)
        self.assertEqual(
            self.git("show", "-s", "--format=%an%n%ae%n%cn%n%ce", head),
            "\n".join([BOT_NAME, BOT_EMAIL, BOT_NAME, BOT_EMAIL]),
        )
        self.assertEqual(
            self.git("show", "-s", "--format=%s", head), "Update RSS feeds"
        )
        self.assertIn(TRAILER, self.git("show", "-s", "--format=%B", head))
        self.assertEqual(
            self.git("diff-tree", "--no-commit-id", "--name-only", "-r", head),
            "feeds/feed_example.xml",
        )
        return head

    def test_append_then_replace_checkpoint_then_identical_bytes_are_noop(self):
        self.write("feeds/feed_example.xml", "<rss>first</rss>\n")
        self.publish()
        first = self.assert_published(self.initial, "<rss>first</rss>")
        self.assertEqual(self.git("rev-list", "--count", first), "2")
        self.write("feeds/feed_example.xml", "<rss>latest</rss>\n")
        self.publish()
        latest = self.assert_published(self.initial, "<rss>latest</rss>")
        self.assertNotEqual(first, latest)
        self.assertEqual(self.git("rev-list", "--count", latest), "2")
        self.publish()
        self.assertEqual(self.remote_head(), latest)
        self.assertEqual(self.git("rev-parse", "HEAD"), latest)

    def test_return_to_source_baseline_removes_checkpoint_without_history_growth(self):
        for cycle in range(2):
            with self.subTest(cycle=cycle):
                content = f"<rss>changed cycle {cycle}</rss>"
                self.write("feeds/feed_example.xml", content + "\n")
                self.publish()
                self.assert_published(self.initial, content)
                self.assertEqual(
                    self.git("rev-list", "--count", self.remote_head()), "2"
                )
                self.write("feeds/feed_example.xml", "<rss>initial</rss>\n")
                self.publish()
                self.assertEqual(self.remote_head(), self.initial)
                self.assertEqual(self.git("rev-parse", "HEAD"), self.initial)
                self.assertEqual(
                    self.git("rev-list", "--count", self.remote_head()), "1"
                )
        self.write("feeds/feed_example.xml", "<rss>final change</rss>\n")
        self.publish()
        self.assert_published(self.initial, "<rss>final change</rss>")
        self.assertEqual(self.git("rev-list", "--count", self.remote_head()), "2")

    def test_clean_source_tip_is_noop(self):
        self.publish()
        self.assertEqual(self.remote_head(), self.initial)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial)

    def test_noncheckpoint_tips_are_preserved(self):
        cases = [
            ("mixed bot checkpoint", True, True, True, True),
            ("human feed change", False, False, True, False),
            ("bot without marker", True, True, False, False),
            ("human committer", True, False, True, False),
            ("human author", False, True, True, False),
        ]
        for label, author, committer, marker, mixed in cases:
            with self.subTest(label=label):
                self.write("feeds/feed_example.xml", label + "\n")
                if mixed:
                    self.write("source.py", "# mixed source change\n")
                tip = self.commit(
                    "Update RSS feeds" + ("\n\n" + TRAILER if marker else ""),
                    author_bot=author,
                    committer_bot=committer,
                )
                self.git("push", "origin", "HEAD:main")
                self.write(
                    "feeds/feed_example.xml", "<rss>published " + label + "</rss>\n"
                )
                self.publish()
                self.assert_published(tip, "<rss>published " + label + "</rss>")

    def test_shallow_actions_checkout_replaces_checkpoint_preserving_source(self):
        self.write("source.py", "# newest source remains published\n")
        source_tip = self.commit("Newest source change")
        self.git("push", "origin", "HEAD:main")
        self.write("feeds/feed_example.xml", "<rss>checkpoint</rss>\n")
        self.publish()
        original = self.remote_head()
        shallow = self.root / "shallow"
        self.git("clone", "--depth=2", self.origin.as_uri(), str(shallow))
        self.assertEqual(
            self.git("rev-parse", "--is-shallow-repository", cwd=shallow), "true"
        )
        self.assertEqual(self.git("rev-list", "--count", "HEAD", cwd=shallow), "2")
        self.repo = shallow
        self.write("feeds/feed_example.xml", "<rss>from shallow checkout</rss>\n")
        self.publish()
        latest = self.assert_published(source_tip, "<rss>from shallow checkout</rss>")
        self.assertNotEqual(original, latest)
        self.assertEqual(
            self.git("--git-dir", str(self.origin), "show", "main:source.py"),
            "# newest source remains published",
        )
        self.assertEqual(
            self.git("--git-dir", str(self.origin), "rev-list", "--count", "main"), "3"
        )

    def test_marker_in_commit_prose_is_not_a_publication_trailer(self):
        self.write("feeds/feed_example.xml", "<rss>historical feed</rss>\n")
        tip = self.commit(
            "Feed update\n\n"
            + TRAILER
            + "\n\nFurther explanatory prose after the marker.",
            author_bot=True,
            committer_bot=True,
        )
        self.git("push", "origin", "HEAD:main")
        self.write("feeds/feed_example.xml", "<rss>new publication</rss>\n")
        self.publish()
        self.assert_published(tip, "<rss>new publication</rss>")

    def test_marked_bot_merge_tip_is_preserved(self):
        original_tree = self.git("rev-parse", "HEAD^{tree}")
        side = self.git(
            "commit-tree", original_tree, "-p", self.initial, "-m", "Side history"
        )
        self.write("feeds/feed_example.xml", "<rss>merge snapshot</rss>\n")
        self.git("add", "feeds/feed_example.xml")
        merge_tree = self.git("write-tree")
        env = self.env.copy()
        env.update(
            GIT_AUTHOR_NAME=BOT_NAME,
            GIT_AUTHOR_EMAIL=BOT_EMAIL,
            GIT_COMMITTER_NAME=BOT_NAME,
            GIT_COMMITTER_EMAIL=BOT_EMAIL,
        )
        merge = self.git(
            "commit-tree",
            merge_tree,
            "-p",
            self.initial,
            "-p",
            side,
            "-m",
            "Update RSS feeds\n\n" + TRAILER,
            env=env,
        )
        self.git("update-ref", "HEAD", merge)
        self.git("push", "origin", "HEAD:main")
        self.write("feeds/feed_example.xml", "<rss>after merge</rss>\n")
        self.publish()
        self.assert_published(merge, "<rss>after merge</rss>")

    def test_empty_marked_bot_commit_is_preserved(self):
        env = self.env.copy()
        env.update(
            GIT_AUTHOR_NAME=BOT_NAME,
            GIT_AUTHOR_EMAIL=BOT_EMAIL,
            GIT_COMMITTER_NAME=BOT_NAME,
            GIT_COMMITTER_EMAIL=BOT_EMAIL,
        )
        self.git(
            "commit", "--allow-empty", "-m", "Update RSS feeds\n\n" + TRAILER, env=env
        )
        tip = self.git("rev-parse", "HEAD")
        self.git("push", "origin", "HEAD:main")
        self.write("feeds/feed_example.xml", "<rss>after empty checkpoint</rss>\n")
        self.publish()
        self.assert_published(tip, "<rss>after empty checkpoint</rss>")

    def test_staged_untracked_nonfeed_is_rejected(self):
        self.write("feeds/nested/feed_other.xml", "<rss>nested</rss>\n")
        self.git("add", "feeds/nested/feed_other.xml")
        self.write("feeds/feed_example.xml", "<rss>new</rss>\n")
        self.publish(success=False)
        self.assertEqual(self.remote_head(), self.initial)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial)

    def test_concurrent_remote_source_commit_is_preserved(self):
        other = self.root / "other"
        self.git("clone", str(self.origin), str(other))
        self.git("config", "user.name", "Other Maintainer", cwd=other)
        self.git("config", "user.email", "other@example.test", cwd=other)
        (other / "source.py").write_text("# concurrent source change\n")
        self.git("add", "source.py", cwd=other)
        self.git("commit", "-m", "Concurrent source change", cwd=other)
        newer = self.git("rev-parse", "HEAD", cwd=other)
        self.git("push", "origin", "HEAD:main", cwd=other)
        self.write("feeds/feed_example.xml", "<rss>stale checkout</rss>\n")
        self.publish(success=False)
        self.assertEqual(self.remote_head(), newer)
        self.assertEqual(
            self.git("--git-dir", str(self.origin), "show", "main:source.py"),
            "# concurrent source change",
        )

    def test_staged_source_change_is_rejected(self):
        self.write("source.py", "# staged change\n")
        self.git("add", "source.py")
        self.write("feeds/feed_example.xml", "<rss>new</rss>\n")
        self.publish(success=False)
        self.assertEqual(self.remote_head(), self.initial)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial)
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "source.py")

    def test_unstaged_tracked_source_change_is_rejected(self):
        self.write("source.py", "# unstaged change\n")
        self.write("feeds/feed_example.xml", "<rss>new</rss>\n")
        self.publish(success=False)
        self.assertEqual(self.remote_head(), self.initial)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial)
        self.assertEqual((self.repo / "source.py").read_text(), "# unstaged change\n")

    def test_only_direct_feed_xml_files_are_staged(self):
        self.write("feeds/feed_example.xml", "<rss>new</rss>\n")
        self.write("feeds/nested/feed_other.xml", "nested must stay untracked\n")
        self.write("feeds/notes.xml", "nonfeed must stay untracked\n")
        self.write("notes.txt", "unrelated must stay untracked\n")
        self.publish()
        self.assert_published(self.initial, "<rss>new</rss>")
        self.assertEqual(
            self.git("ls-tree", "-r", "--name-only", self.remote_head()),
            "feeds/feed_example.xml\nsource.py",
        )


if __name__ == "__main__":
    unittest.main()
