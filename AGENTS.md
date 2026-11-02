# Personal AI Assistant

Primary project: `F:\Projects\personal-ai-assistant`.
Private repository: `peca12968/personal-ai-assistant`.

- Communicate with the user in Traditional Chinese.
- Preserve local data. Never commit conversations, personal memory, exports, auth files, tokens, or logs.
- Keep changes focused on understanding requests and solving problems accurately.
- After a bug or misunderstanding, add a meaningful regression case when feasible.
- Validate with `python -m unittest discover -s tests -v`, `python scripts/evaluate.py`, `python scripts/evaluate_context.py`, `python scripts/evaluate_documents.py`, and `python scripts/evaluate_search.py`.
- Record material progress and evidence in `docs/PROGRESS.md`.
- Publish completed updates using `python scripts/publish.py --message "description"`; verify the private destination and remote head. User has authorized updates to this private repository.
- Do not equate offline retrieval tests with model reasoning improvement. Live evaluation requires real responses and rubric review.
- Validate understanding scenarios with `python scripts/evaluate_understanding.py`. Live runs use explicit report paths and fingerprints; wait for collection to finish before writing reviews with `scripts/review_understanding.py`. Keep agent reviews distinct from user or independent acceptance.
- Default engine is advice-only. Any future tool execution must have explicit scope and observable results.
- CSV work tools require an explicit selected input and saved plan before execution. Validate real artifacts with `python scripts/evaluate_tasks.py`; report planned/running/failed/cancelled states honestly. Chat does not automatically execute task plans.
- CSV workflow plans are typed data, never generated executable code. Language planning sends only the request and selected CSV headers; execution requires the saved plan and an explicit execute action. Validate `python scripts/evaluate_workflows.py`, preserving distinction between all filtered totals and top-N totals. Do not silently omit unsupported requested operations.
- Workflow v3 adds arithmetic means with a frozen four-place ROUND_HALF_EVEN policy. Preserve sum/count evidence, undefined empty means, exact-ratio ordering, and separate overall/returned row counts and means. Legacy v2 plans and reports remain sum-only. Never substitute the unweighted mean of group means for the mean of retained data rows.
- Read `docs/ROADMAP.md` before continuing the long-term goal. No automatic model-weight training is implemented.
- Earlier explicit user corrections have a separate bounded raw-text context (latest six, 1000 characters each), selected across the same session beyond the ordinary 200-user scan. Preserve message IDs, clipping/overflow flags, local-command exclusions and retry exclusions. Prefix matching is not semantic confirmation, memory mutation or fresh authorization; current requests still take precedence.
- Explicit memory chat commands execute locally in one transaction with receipts. Never parse model replies, quoted data or document contents as operations. Preserve local-memory message exclusions in both recent and earlier model context, and persistent non-reused memory IDs. Validate `python scripts/evaluate_memory_commands.py`; live replies still require separate rubric review.
- Model answer attempts persist status and source snapshots. Retry only the latest unfinished question without later messages, use current references, and never append a duplicate user request. Keep answer/context/success writes atomic. Recover a running record only after acquiring its SQLite exclusive lease; file presence, age or an observation error is not proof of termination. Local owner loss does not prove the cloud request stopped. Validate `python scripts/evaluate_answer_runs.py`.
- Self-improvement is one user-started goal per run, restricted to the exact selected files in `assistant/evolution.py`. Keep the controller, publishing, storage, tests and evaluation fixtures outside model edit scope. Candidate checks execute Python with the user's account; a copy is not an OS sandbox.
- Preserve self-improvement evidence under ignored `data/evolution`, require a clean base and unchanged source, and never report a publication as complete without matching the private remote head. Do not start perpetual self-edit loops.
- When acceptance cases are provided, measure the original and candidate in fresh processes against the same immutable inputs and expectations. All candidate cases must pass with at least one baseline failure resolved. Keep host grading, observers and cases outside model edit scope; report regression-only runs as such. Known-case gains are not independent model quality scores.
