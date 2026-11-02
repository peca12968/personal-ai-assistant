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
- Read `docs/ROADMAP.md` before continuing the long-term goal. No automatic model-weight training is implemented.
- Self-improvement is one user-started goal per run, restricted to the exact selected files in `assistant/evolution.py`. Keep the controller, publishing, storage, tests and evaluation fixtures outside model edit scope. Candidate checks execute Python with the user's account; a copy is not an OS sandbox.
- Preserve self-improvement evidence under ignored `data/evolution`, require a clean base and unchanged source, and never report a publication as complete without matching the private remote head. Do not start perpetual self-edit loops.
