# Read a Run and Hand Off the Evidence

## Minimal join contract

| Record | Evidence to retain |
|---|---|
| Run | `run_id`, case and condition, actual model/request settings, code and environment versions, trace ID when present |
| Attempt | `attempt_id`, prior attempt, trigger, subject that initiated it, result, retained or reset workspace |
| Event | Unique event ID, `run_id`, `attempt_id`, UTC timestamp, source, event type, stage status, evidence pointer |
| Model call | `call_id`, provider request/response IDs, usage or missing marker, status and time; link originating tool result when sent |
| Tool or program | Provider tool call ID when available, actual execution boundary, result/error, file hashes, timestamps |
| Feedback | What was sent versus only logged locally, timestamp, target call and source error/event |
| Submission and evaluation | Before/after artifact IDs or hashes, schema result, final score or evaluation-unavailable reason, evaluator version |

Use a separate source for the model, runner/Harness, executor, evaluator, SDK and operator where applicable. Prefer structured events to interpreting free text. For sensitive data save a local artifact pointer and hash rather than exporting the content. Log tool requests and results from the actual interface you control. To prove an internal Python library call took a branch or ran a certain number of times, add runtime observations or debuggable counters to the executor; imports, names in generated code and heuristic tool scores are not substitutes.

## Annotate a case

```text
Condition -> first observed problem -> actual delivered feedback
          -> actor and next action -> changed artifact -> final evaluation
```

Use one row per boundary with source links or local event IDs. Tag claims as `observed`, `inferred`, `not_recorded`, or `not_applicable`. Use `not_sent` only when there is evidence that no feedback was sent. Do not equate an empty log with evidence of absence.

Examples:

| Record | Defensible conclusion |
|---|---|
| One model response containing code, then Python exit 0 and a valid file | Runner executed the code and delivered a valid file. There is no in-run model repair when no feedback path exists. |
| First process failed; a new run ID succeeded | A replacement run succeeded. The available records do not prove the first error was sent or that the agent corrected its own work. |
| Tool request, matching execution error, next model input includes the error, changed code, validation passes | The logged agent path handled a known error and recovered execution or submission. Check the final task result separately; the trace alone does not establish the change caused an improvement. |
| Tool returned success but did not provide enough information to continue | A tool execution succeeded; usefulness is a separate observation requiring the returned result and next action. |

## Pilot and handoff

1. Check a normal run and one real failure or unhelpful tool result when available. If none exists, use an explicitly labeled fixture (such as a missing public CSV ID column) to test the join from failure through feedback and final validation. A fixture is not measured agent performance.
2. Hand the runner owner the exact missing link: for example, the tool error exists, but no outgoing request records that it was delivered. Ask for the originating event ID, next call ID and allowed message, not for private chain-of-thought.
3. Hand the policy owner one evidence card per bottleneck: condition and frequency, verified sequence, unknowns, impact on valid submissions and final results, extra calls/time/cost, plus a small candidate comparison. Compare on the same failure and keep model, task, authorized information, environment, evaluator and budgets fixed except for the variable being studied. Review feedback access with the evaluator owner.

Count a repeated model usage record once per actual call. Separate retry calls when observed; resolve missing usage with the runner or mark it unmeasured. Treat before/after scores from different runs or conditions as comparisons, not proof that repair caused a score change.
