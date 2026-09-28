# What happens when a job fails — the retry policy

**Written 2026-09-28, shipped in GatewayKH 0.2.62.** The owner asked for this policy to exist
**and to be written down**, in his words:

> *"i want a 'job' that's failed to retry on a different machine, be it in the same cluster or a
> different site. just make sure no duplicates but also make sure the job is retried 3 times. if
> all 3 fails. that it's declared failed properly to story studio."*

The policy is enforced in `gateway/worker.py` (`_next_machine`, `_tried_everything_detail`) and is
stated to the caller on every job that has been retried, through `gateway/api.py`
(`_publish_attempt_evidence`). The number is live-editable; the wording below is generated from
the same function the code uses (`concurrency.retry_policy_wording`), so this page and the
Gateway's own Jobs view cannot drift apart.

## The table

| Attempt | Machine chosen | When it moves on |
|---|---|---|
| 1 | the machine the job was routed to | any **known** failure: busy, refused, unreachable, engine error (`ffmpeg not found`, `[METAL] Command buffer execution failed`), or a timeout whose upstream outcome is known to have failed |
| 2 | a **different machine** — another Mac in the same cluster first, then any other site offering the same model | same |
| 3 | another different machine, **never one already tried** | same |
| — | after the third failure | the job is declared **failed to Story Studio** with a terminal reason naming every machine that was tried and why each one failed |

**Three attempts is the default, not a law of nature.** It is `retry.max_attempts` in
`provider-concurrency.local.json`, read live beside the pool size, so a fleet of one Mac and a
fleet of fifty can want different answers without a release:

```json
{"global": 60, "retry": {"max_attempts": 3}}
```

## What counts as an attempt — and what does not

**Only a dispatch to a machine counts.** A job that is waiting because every site offering the
model is busy has not been tried on anything; it is *deferred*, and the queue's own wait budget
bounds that (`queue.max_deferrals`, `queue.defer_cap_seconds`). The job row's `attempt_count`
counts every dispatch including those deferrals, which is why it is **not** the number this
policy is measured against — the attempt log is
(`store.attempts_of`, the `attempt_log` column). A caller reading `attempts_used` and
`attempts_allowed` on the job envelope is reading the number that decides.

**A job with nowhere else to go waits, it does not fail.** If no other site offers the model —
or the only site offering it is busy with its own work — the job defers at the machine it is
already on rather than spending one of its three attempts on a machine that was never going to
run it. That is deliberate: one house losing power must not fail the work that the other houses
could still do, and a single-Mac site that is busy must wait for its own Mac rather than collect
a failure.

## No duplicates, absolutely

**A retry happens only when the previous attempt is known to have failed.**

If the upstream **accepted** the work and then went silent, the outcome is *unknown*: the job may
be rendering and billing right now. An unknown outcome becomes a **reconciliation hold** and is
**never** re-run automatically, no matter how many attempts remain. This is the discipline that
already existed (`upstream_accepted is not False` guarded the old re-route) and the retry policy
sits **on top of it** — it does not weaken it. The condition is one line in `worker.execute_job`:
`if not unknown and pinned is None:` guards the re-route, and everything else falls through to the
hold-or-fail paths unchanged.

Three further guarantees, each with a test in `tests/test_job_retry_policy.py`:

- **A machine already tried is never chosen again.** `fleet.route(..., exclude_locations=…)` is
  given the job's own recorded list plus the machine that just failed. The exclusion is a
  **column**, not a local variable: the old set lived inside one dispatch call, so a job that
  deferred and came back — or was redelivered after a restart — could be sent to the machine
  that had just failed it.
- **An attempt is recorded before the next machine is chosen.** A crash between "we tried it" and
  "we remembered" would make the same machine eligible again.
- **Idempotency keys still hold.** A caller replaying a request cannot create a second job; the
  retry policy operates on the one job row and never inserts a new one.

## Voices are pinned, so a voice failure is terminal

A voice clone names its owner and its reference clip lives on one controller. There is no other
machine that could serve it, so a pinned job is excluded from the re-route entirely
(`pinned = fleet.pinned_location(params)`): a "retry elsewhere" would be a slower failure. A voice
that fails is terminal after its own attempts.

## What an operator sees

Every job that has been retried publishes, on its envelope:

```json
{
  "attempts": [
    {"number": 1, "machine": "studio-hub-9701220bf30f", "failure_code": "MACHINE_BUSY",
     "failure_reason": "no free machine at this site", "at": 1790000000.0},
    {"number": 2, "machine": "studio-hub-a311a0a9f4f7", "failure_code": "PROVIDER_FAILED",
     "failure_reason": "ffmpeg not found on PATH", "at": 1790000030.0}
  ],
  "attempts_used": 2,
  "attempts_allowed": 3,
  "attempt_policy": "A failed job is retried on a **different machine** — …"
}
```

A job on its first machine carries **no** `attempts` key rather than an empty list, so "never
retried" and "we did not look" stay distinguishable. The Gateway's Jobs view renders
*attempt n of 3* and the last reason from exactly these fields; the terminal failure's
`failure_reason` is the sentence that names every machine.

## The honest limits

- **The Gateway addresses sites; Studio Hub addresses machines.** A retry therefore moves the
  job to a different *site* — which is a different Mac whenever a site has one reachable Mac, and
  a different *cluster* when it has several. Inside a cluster, which Mac runs the work is the
  controller's decision, and the controller already avoids a machine that has just failed
  (`broker.py`). Nothing here can name an individual Mac inside a site, and pretending otherwise
  would mean a new field on the Hub's submit contract.
- **An unknown outcome can still leave work unresolved.** It is a hold, not a failure, and it
  needs the operator (or the reconciliation sweep) to settle it. That is the price of never
  rendering twice, and it is the right way round.
- **A job that fails for a reason that is about the *request*, not the machine, still spends its
  attempts.** A geometry refusal will fail identically on all three machines. This is bounded and
  harmless — it never duplicates work — but it is three round trips where one would do; the
  attempt log makes it obvious, because all three entries carry the same code and sentence.
