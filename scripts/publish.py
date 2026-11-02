"""Test, inspect, commit and push only project source to the private origin."""
import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = "peca12968/personal-ai-assistant"
ALLOWED_DIRS = {"assistant", "prompts", "tests", "scripts", "evals", "docs", ".github"}
ALLOWED_ROOT = {".gitignore", ".gitattributes", "README.md", "AGENTS.md", "Start.cmd", "Publish.cmd"}
SECRET = re.compile(rb"(?:sk-[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{24,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)")


def run(*args, capture=False):
    result = subprocess.run(args, cwd=ROOT, check=True, stdout=subprocess.PIPE if capture else None,
                            encoding="utf-8", errors="replace")
    return result.stdout.strip() if capture else ""


def validate_path(name):
    path = Path(name)
    return (name in ALLOWED_ROOT or (path.parts and path.parts[0] in ALLOWED_DIRS)) and not any(
        p in {"__pycache__", "data", ".venv"} or p.startswith(".env") for p in path.parts
    ) and path.suffix.lower() not in {".db", ".sqlite3", ".log", ".pem", ".key", ".pyc"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--message", default="Update personal assistant with verified changes")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    run(sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v")
    run(sys.executable, "scripts/evaluate.py")
    run(sys.executable, "scripts/evaluate_context.py")
    if run("gh", "repo", "view", REPO, "--json", "visibility", "--jq", ".visibility", capture=True) != "PRIVATE":
        raise RuntimeError("Upload stopped: destination must be PRIVATE.")
    remote = run("git", "remote", "get-url", "--push", "origin", capture=True)
    if remote not in {f"https://github.com/{REPO}.git", f"https://github.com/{REPO}", f"git@github.com:{REPO}.git"}:
        raise RuntimeError("Upload stopped: origin is not the configured private repository.")
    tracked = run("git", "ls-files", "-z", capture=True).split("\0")
    if any(name and not validate_path(name) for name in tracked):
        raise RuntimeError("Upload stopped: tracked files outside approved source directories.")
    run("git", "add", "--", *sorted(ALLOWED_ROOT), *sorted(ALLOWED_DIRS))
    staged = run("git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z", capture=True).split("\0")
    for name in filter(None, staged):
        if not validate_path(name):
            raise RuntimeError(f"Upload stopped: unexpected file {name}")
        content = subprocess.check_output(["git", "show", ":" + name], cwd=ROOT)
        if len(content) > 2_000_000 or SECRET.search(content):
            raise RuntimeError(f"Upload stopped: large file or possible credential in {name}")
    if args.check_only:
        print("Validation passed; changes are staged, not committed or pushed.")
        return
    if run("git", "diff", "--cached", "--name-only", capture=True):
        run("git", "commit", "-m", args.message)
    branch = run("git", "branch", "--show-current", capture=True)
    if not branch:
        raise RuntimeError("Detached HEAD: select a branch before publishing.")
    run("git", "push", "-u", "origin", branch)
    print("Verified update uploaded to private GitHub repository.")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
