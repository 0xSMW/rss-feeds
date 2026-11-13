"""Publish generated XML, replacing only an identified feed-only checkpoint."""

import os
import subprocess
from pathlib import PurePosixPath

BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "github-actions[bot]@users.noreply.github.com"
MARKER = "Feed-Publication: rolling-v1"


def git(*args: str, **kwargs) -> str:
    return subprocess.check_output(["git", *args], text=True, **kwargs).rstrip("\n")


def is_feed(path: str) -> bool:
    parsed = PurePosixPath(path)
    return (
        parsed.parent == PurePosixPath("feeds")
        and parsed.name.startswith("feed_")
        and parsed.suffix == ".xml"
    )


def paths(*args: str) -> list[str]:
    return [path for path in git(args[0], "-z", *args[1:]).split("\0") if path]


def checkpoint_parent(tip: str) -> str | None:
    parents = git("rev-list", "--parents", "-n", "1", tip).split()[1:]
    if len(parents) != 1:
        return None
    author, email, committer, committer_email, body = git(
        "show", "-s", "--format=%an%x00%ae%x00%cn%x00%ce%x00%B", tip
    ).split("\0", 4)
    if (author, email, committer, committer_email) != (
        BOT_NAME,
        BOT_EMAIL,
        BOT_NAME,
        BOT_EMAIL,
    ) or MARKER not in git("interpret-trailers", "--parse", input=body).splitlines():
        return None
    changed = paths("diff", "--no-renames", "--name-only", parents[0], tip)
    if changed and all(is_feed(path) for path in changed):
        return parents[0]
    return None


def main() -> None:
    tip = git("rev-parse", "HEAD")
    # Refuse to incorporate any source edit, even if it was already staged.
    changed = paths("diff", "--no-renames", "--name-only", "HEAD")
    if any(not is_feed(path) for path in changed):
        raise RuntimeError("Refusing publication: tracked changes outside feed XML")

    candidates = paths(
        "ls-files", "--cached", "--others", "--exclude-standard", "--", "feeds/"
    )
    feeds = sorted({path for path in candidates if is_feed(path)})
    if feeds:
        git("add", "--all", "--", *feeds)
    staged = paths("diff", "--cached", "--no-renames", "--name-only")
    if any(not is_feed(path) for path in staged):
        raise RuntimeError("Refusing publication: staged changes outside feed XML")
    if not staged:
        print("Feed XML unchanged; no publication needed", flush=True)
        return

    previous_parent = checkpoint_parent(tip)
    parent = previous_parent or tip
    tree = git("write-tree")
    if previous_parent and tree == git("rev-parse", f"{parent}^{{tree}}"):
        # Returning to the source snapshot needs no publication commit. Keeping
        # an empty checkpoint would prevent the next refresh from replacing it.
        commit = parent
        action = "Removing redundant"
    else:
        env = os.environ.copy()
        env.update(
            GIT_AUTHOR_NAME=BOT_NAME,
            GIT_AUTHOR_EMAIL=BOT_EMAIL,
            GIT_COMMITTER_NAME=BOT_NAME,
            GIT_COMMITTER_EMAIL=BOT_EMAIL,
        )
        commit = git(
            "commit-tree",
            tree,
            "-p",
            parent,
            input=f"Update RSS feeds\n\n{MARKER}\n",
            env=env,
        )
        action = "Replacing" if previous_parent else "Creating"
    print(f"{action} feed checkpoint {tip} -> {commit}", flush=True)
    # An explicit expected value also protects against background Git fetches.
    subprocess.run(
        [
            "git",
            "push",
            f"--force-with-lease=refs/heads/main:{tip}",
            "origin",
            f"{commit}:refs/heads/main",
        ],
        check=True,
    )
    git("update-ref", "HEAD", commit, tip)


if __name__ == "__main__":
    main()
