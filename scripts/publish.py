"""Test, inspect, commit and push only project source to the private origin."""
import argparse
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = "peca12968/personal-ai-assistant"
ALLOWED_DIRS = {"assistant", "prompts", "tests", "scripts", "evals", "docs", ".github"}
ALLOWED_ROOT = {".gitignore", ".gitattributes", "README.md", "AGENTS.md", "Start.cmd", "Publish.cmd", "requirements.txt"}
SECRET = re.compile(rb"(?:sk-[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{24,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)")


def run(*args, capture=False):
    # -X utf8 on this process is not inherited by nested Python CLIs.
    # Keep validation output UTF-8 through every child generation on Windows.
    child_env = {**os.environ, 'PYTHONUTF8': '1'}
    result = subprocess.run(args, cwd=ROOT, check=True, stdout=subprocess.PIPE if capture else None,
                            encoding="utf-8", errors="replace", env=child_env)
    return result.stdout.strip() if capture else ""


def validate_path(name):
    path = Path(name)
    return (name in ALLOWED_ROOT or (path.parts and path.parts[0] in ALLOWED_DIRS)) and not any(
        p in {"__pycache__", "data", ".venv"} or p.casefold() in {'autonomy-artifacts','learning-batches','local-engine-workspace'} or p.casefold().endswith('.attempts') or p.startswith(".env") for p in path.parts
    ) and path.suffix.lower() not in {".db", ".sqlite3", ".paibackup", ".ics", ".log", ".pem", ".key", ".pyc"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--message", default="Update personal assistant with verified changes")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument('--expected-head', help='Self-improvement base commit; reject concurrent revisions')
    parser.add_argument('--expected-file', action='append', help='Exact allowed changed file for a self-improvement release')
    parser.add_argument('--expected-sha256', action='append', help='path=sha256 of each tested self-improvement file')
    args = parser.parse_args()
    run(sys.executable, "scripts/check_workflow.py")
    run(sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v")
    run(sys.executable, "scripts/evaluate_chat_calculation.py")
    run(sys.executable, "scripts/evaluate_chat_transcription.py")
    run(sys.executable, "scripts/evaluate_chat_evidence.py")
    run(sys.executable, "scripts/evaluate_daily_conversations.py")
    run(sys.executable, "scripts/evaluate_device_components.py")
    run(sys.executable, "scripts/learning/prepare_epistemic.py")
    run(sys.executable, "scripts/evaluate.py")
    run(sys.executable, "scripts/evaluate_context.py")
    run(sys.executable, "scripts/evaluate_documents.py")
    run(sys.executable, "scripts/evaluate_search.py")
    run(sys.executable, "scripts/evaluate_understanding.py")
    run(sys.executable, "scripts/evaluate_understanding.py", "--suite", "evals/claim_states.json")
    run(sys.executable, "scripts/evaluate_understanding.py", "--suite", "evals/function_claim_extraction.json")
    run(sys.executable, "scripts/evaluate_understanding.py", "--suite", "evals/communication_context.json")
    run(sys.executable, "scripts/evaluate_understanding.py", "--suite", "evals/companion_profile.json")
    run(sys.executable, "scripts/evaluate_understanding.py", "--suite", "evals/product_help.json")
    run(sys.executable, "scripts/evaluate_understanding.py", "--suite", "evals/delivery_generalization.json")
    run(sys.executable, "scripts/evaluate_understanding.py", "--suite", "evals/transcription.json")
    run(sys.executable, "scripts/evaluate_understanding.py", "--suite", "evals/transcription_fidelity.json")
    run(sys.executable, "scripts/evaluate_understanding.py", "--suite", "evals/transcription_sources.json")
    run(sys.executable, "scripts/evaluate_understanding.py", "--suite", "evals/transcription_escapes.json")
    run(sys.executable, "scripts/evaluate_understanding.py", "--suite", "evals/work_comprehension.json")
    run(sys.executable, "scripts/evaluate_understanding.py", "--suite", "evals/evidence_reasoning.json")
    run(sys.executable, "scripts/evaluate_understanding.py", "--suite", "evals/evidence_reasoning_holdout.json")
    run(sys.executable, "scripts/evaluate_understanding.py", "--suite", "evals/work_context_budget.json")
    run(sys.executable, "scripts/evaluate_understanding.py", "--suite", "evals/local_chat_budget.json")
    run(sys.executable, "scripts/evaluate_understanding.py", "--suite", "evals/local_chat_continuity.json")
    run(sys.executable, "scripts/evaluate_understanding.py", "--suite", "evals/local_document_budget.json")
    run(sys.executable, "scripts/evaluate_understanding.py", "--suite", "evals/document_partial.json")
    run(sys.executable, "scripts/evaluate_understanding.py", "--suite", "evals/topic_context.json")
    run(sys.executable, "scripts/evaluate_goal_drafts.py")
    run(sys.executable, "scripts/evaluate_goal_drafts.py", "--suite", "evals/goal_draft_inputs.json")
    run(sys.executable, "scripts/evaluate_goal_drafts.py", "--suite", "evals/goal_draft_planning.json")
    run(sys.executable, "scripts/evaluate_goal_drafts.py", "--suite", "evals/goal_draft_decisions.json")
    run(sys.executable, "scripts/evaluate_goal_drafts.py", "--suite", "evals/goal_draft_named_topics.json")
    run(sys.executable, "scripts/evaluate_goal_handoff.py")
    run(sys.executable, "scripts/evaluate_goal_handoff.py", "--suite", "evals/open_work_drafts.json", "--checks", "evals/open_work_handoff.json")
    run(sys.executable, "scripts/evaluate_goal_replies.py")
    run(sys.executable, "scripts/evaluate_goal_replies.py", "--suite", "evals/goal_multisource_replies.json")
    run(sys.executable, "scripts/evaluate_goal_replies.py", "--suite", "evals/goal_source_conflicts.json")
    run(sys.executable, "scripts/evaluate_goal_replies.py", "--suite", "evals/goal_progressive_replies.json")
    run(sys.executable, "scripts/evaluate_goal_replies.py", "--suite", "evals/goal_plan_dependencies.json")
    run(sys.executable, "scripts/evaluate_goal_replies.py", "--suite", "evals/goal_plan_delivery_review.json")
    run(sys.executable, "scripts/evaluate_goal_replies.py", "--suite", "evals/goal_plan_item_evidence.json")
    run(sys.executable, "scripts/evaluate_goal_replies.py", "--suite", "evals/goal_plan_operand_evidence.json")
    run(sys.executable, "scripts/evaluate_goal_replies.py", "--suite", "evals/goal_plan_operand_conversion.json")
    run(sys.executable, "scripts/evaluate_goal_replies.py", "--suite", "evals/goal_quantity_resolution.json")
    run(sys.executable, "scripts/evaluate_goal_replies.py", "--suite", "evals/goal_quantity_reuse.json")
    run(sys.executable, "scripts/evaluate_goal_replies.py", "--suite", "evals/goal_cross_document_rework.json")
    run(sys.executable, "scripts/evaluate_goal_replies.py", "--suite", "evals/goal_followup_field_scope.json")
    run(sys.executable, "scripts/evaluate_goal_replies.py", "--suite", "evals/goal_omitted_field_references.json")
    run(sys.executable, "scripts/evaluate_goal_replies.py", "--suite", "evals/goal_pending_dependencies.json")
    run(sys.executable, "scripts/evaluate_goal_replies.py", "--suite", "evals/goal_scoped_amendments.json")
    run(sys.executable, "scripts/evaluate_goal_replies.py", "--suite", "evals/goal_withdrawn_questions.json")
    run(sys.executable, "scripts/evaluate_goal_replies.py", "--suite", "evals/goal_decision_questions.json")
    run(sys.executable, "scripts/evaluate_goal_replies.py", "--suite", "evals/goal_decision_coverage.json")
    run(sys.executable, "scripts/evaluate_goal_replies.py", "--suite", "evals/goal_source_decision_transcription.json")
    run(sys.executable, "scripts/evaluate_goal_replies.py", "--suite", "evals/goal_declared_rule_ranges.json")
    run(sys.executable, "scripts/evaluate_goal_replies.py", "--suite", "evals/goal_source_rule_versions.json")
    run(sys.executable, "scripts/evaluate_goal_replies.py", "--suite", "evals/goal_competing_version_adoptions.json")
    run(sys.executable, "scripts/evaluate_goal_replies.py", "--suite", "evals/goal_version_clarification_order.json")
    run(sys.executable, "scripts/evaluate_goal_replies.py", "--suite", "evals/goal_grounded_roles.json")
    run(sys.executable, "scripts/evaluate_goal_replies.py", "--suite", "evals/goal_source_fidelity.json")
    run(sys.executable, "scripts/evaluate_goal_replies.py", "--suite", "evals/goal_scoped_document_lookup.json")
    run(sys.executable, "scripts/evaluate_goal_replies.py", "--suite", "evals/goal_batch_document_lookup.json")
    run(sys.executable, "scripts/evaluate_goal_replies.py", "--suite", "evals/goal_multi_document_lookup.json")
    run(sys.executable, "scripts/evaluate_goal_replies.py", "--suite", "evals/goal_evidence_sufficiency.json")
    run(sys.executable, "scripts/evaluate_goal_replies.py", "--suite", "evals/goal_priority_rules.json")
    run(sys.executable, "scripts/evaluate_goal_replies.py", "--suite", "evals/goal_partial_rule_precedence.json")
    run(sys.executable, "scripts/evaluate_goal_replies.py", "--suite", "evals/goal_scoped_rule_precedence.json")
    run(sys.executable, "scripts/evaluate_goal_replies.py", "--suite", "evals/goal_decision_revision.json")
    run(sys.executable, "scripts/evaluate_goal_replies.py", "--suite", "evals/goal_decision_revision_comparison.json")
    run(sys.executable, "scripts/evaluate_goal_replies.py", "--suite", "evals/goal_batch_decision_revision.json")
    run(sys.executable, "scripts/evaluate_goal_replies.py", "--suite", "evals/goal_decision_replacement.json")
    run(sys.executable, "scripts/evaluate_goal_replies.py", "--suite", "evals/goal_natural_decision_updates.json")
    run(sys.executable, "scripts/evaluate_goal_replies.py", "--suite", "evals/goal_mixed_scope_updates.json")
    run(sys.executable, "scripts/evaluate_json_documents.py")
    run(sys.executable, "scripts/evaluate_json_statistics.py")
    run(sys.executable, "scripts/evaluate_tool_recovery.py")
    run(sys.executable, "scripts/evaluate_tool_recovery.py", "--suite", "evals/goal_source_handoffs.json")
    run(sys.executable, "scripts/evaluate_date_months.py")
    run(sys.executable, "scripts/evaluate_docx_reading.py")
    run(sys.executable, "scripts/evaluate_pdf_reading.py")
    run(sys.executable, "scripts/evaluate_exact_calculation.py")
    run(sys.executable, "scripts/evaluate_feedback_order.py")
    run(sys.executable, "scripts/evaluate_conversation_text.py")
    run(sys.executable, "scripts/evaluate_conversation_priority.py")
    run(sys.executable, "scripts/evaluate_conversation_search.py")
    run(sys.executable, "scripts/evaluate_conversation_search.py", "--suite", "evals/conversation_window.json")
    run(sys.executable, "scripts/evaluate_conversation_search.py", "--suite", "evals/expression_calculation.json")
    run(sys.executable, "scripts/evaluate_conversation_search.py", "--suite", "evals/expression_diagnostics.json")
    run(sys.executable, "scripts/evaluate_conversation_search.py", "--suite", "evals/number_rounding.json")
    run(sys.executable, "scripts/evaluate_conversation_search.py", "--suite", "evals/month_schedule.json")
    run(sys.executable, "scripts/evaluate_understanding.py", "--suite", "evals/communication_scope.json")
    run(sys.executable, "scripts/evaluate_understanding.py", "--suite", "evals/quote_continuity.json")
    run(sys.executable, "scripts/evaluate_understanding.py", "--suite", "evals/recent_budget.json")
    run(sys.executable, "scripts/evaluate_understanding.py", "--suite", "evals/direction_changes.json")
    run(sys.executable, "scripts/evaluate_csv_membership.py")
    run(sys.executable, "scripts/evaluate_csv_membership.py", "--suite", "evals/csv_median.json")
    run(sys.executable, "scripts/evaluate_csv_membership.py", "--suite", "evals/csv_grouping.json")
    run(sys.executable, "scripts/evaluate_images.py")
    run(sys.executable, "scripts/evaluate_goal_status.py")
    run(sys.executable, "scripts/evaluate_goal_discussion.py")
    run(sys.executable, "scripts/evaluate_reasoning_pair.py", "--profile", "baseline", "--out", "data/evaluations/offline-reasoning-baseline")
    run(sys.executable, "scripts/evaluate_reasoning_pair.py", "--profile", "challenge", "--seed", "20261104", "--out", "data/evaluations/offline-reasoning-challenge")
    run(sys.executable, "scripts/evaluate_tasks.py")
    run(sys.executable, "scripts/evaluate_workflows.py")
    run(sys.executable, "scripts/evaluate_memory_commands.py")
    run(sys.executable, "scripts/evaluate_answer_runs.py")
    if args.expected_head:
        if run('git', 'rev-parse', 'HEAD', capture=True) != args.expected_head:
            raise RuntimeError('Upload stopped: base commit changed during validation.')
        if not args.expected_file:
            raise RuntimeError('Upload stopped: expected files are required.')
        changed = set(filter(None, run('git', 'diff', 'HEAD', '--name-only', '-z', capture=True).split('\0')))
        untracked = set(filter(None, run('git', 'ls-files', '--others', '--exclude-standard', '-z', capture=True).split('\0')))
        if not changed or changed | untracked != set(args.expected_file):
            raise RuntimeError('Upload stopped: changes differ from the verified self-improvement files.')
        expected = dict(item.rsplit('=', 1) for item in (args.expected_sha256 or []))
        if set(expected) != set(args.expected_file) or any(
            hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != sha for name, sha in expected.items()
        ):
            raise RuntimeError('Upload stopped: source content differs from the tested self-improvement.')
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
