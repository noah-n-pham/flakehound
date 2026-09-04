# BENCHMARKS

Real numbers from real runs. Every figure here was produced by a command in
`loadtest/`, and any figure that is not yet measured says so instead of being
estimated.

**Measured so far:** scenario 1, webhook ingest throughput, and scenario 2, dashboard
read load, both against the container.
**Not yet measured:** the tunnel smoke test and the chaos pass.

---

## The box these numbers came from, which is not production

Everything below ran on one Apple-silicon laptop: k6, uvicorn, the worker and
Postgres 16 all inside Docker Desktop's VM, **8 CPUs and 8.2 GB between them**.
The load generator was co-located with the target, so it competed for those CPUs.

The production task is **256 CPU units (a quarter of one vCPU) and 512 MB**. It
is roughly a thirtieth of the compute these numbers were measured on, so **none of
them transfers to production as-is.** They characterise the software: where its
ceiling comes from, what shape it fails in, and how much room there is above the
0.2–0.5 events/s the design answers to. Measuring the task itself means running k6
from inside the VPC against the task's own port, which needs infrastructure that
does not exist yet.

**k6 never pointed at `api.flakehound.com`.** SPEC section 10 is explicit, and the
reason is that a few hundred requests per second through Cloudflare measures
Cloudflare. The tunnel gets its own low-rate smoke test, reported separately, so
no number here is ambiguous about which path it crossed.

---

## Scenario 1: webhook ingest throughput

### Method

`loadtest/ladder.sh` walks the offered rate up, one k6 process per plateau, 30
seconds each, after a discarded 5-second warm-up. k6 runs in a container on the
compose network and posts to `http://app:8000`, so there is no host port
forwarding in the path.

Each iteration posts a `workflow_job` body of **2,520 bytes** (ten steps, shaped
like the real thing, because the body is what gets HMAC'd and JSON-parsed), signed
per iteration with HMAC-SHA256 and carrying a **unique `X-GitHub-Delivery`**. That
last detail is load-bearing: the deliveries table's primary key *is* the dedup
mechanism, so a flood of one delivery id would measure one insert and N−1 conflicts.
Zero duplicates were recorded at every plateau, which is the proof it worked.

The executor is `constant-arrival-rate`: an open model, where k6 offers the rate
regardless of what the service does with it. A closed model would let rising
latency quietly lower the offered rate, which is the number being measured.

The database was not empty. It began the run holding **146,172 deliveries and
30,458 job rows**, with the queue fully drained. The worker ran throughout, as it
does in production: one container, three processes.

### Results

Latencies in milliseconds, as seen by the client. `dropped` is iterations k6 could
not start on time against a 600-VU budget; `backlog` is `event_queue` rows still
pending when the plateau ended.

| offered /s | achieved /s | dropped | p50 | p95 | p99 | max | HTTP errors | backlog |
|---|---|---|---|---|---|---|---|---|
| 50 | 50.0 | 0 | 4.4 | 150 | 845 | 1,014 | 0 | 17 |
| 100 | 100.0 | 0 | 4.3 | 132 | 411 | 908 | 0 | 549 |
| 200 | 192.5 | 225 | 3.3 | 80 | 1,512 | 1,961 | 0 | 3,505 |
| 400 | 369.6 | 579 | 67.9 | 1,767 | 2,900 | 5,453 | 0 | 12,056 |
| 800 | 266.3 | 15,564 | 1,783 | 6,014 | 9,245 | 15,607 | 0 | 18,122 |

Then, with the load stopped, the worker drained the backlog it had been left:

**18,102 queue rows in 201 seconds: 90 rows/second.**

Over the whole run, deliveries rose by 30,385 and job rows rose by **30,385**. One
delivery, one fact, no drift, and nothing lost.

### What the numbers say

**Sustained ingest is 90/s, and the limit is the worker, not the endpoint.** The
API accepts far more than the worker can drain, so "sustained" is set by the drain
rate: 90 rows/second, measured directly. At 50/s the queue ends a 30-second
plateau 17 rows deep. The worker keeps up. At 100/s it ends 549 deep, which is
the queue growing at ~18/s and the drain running at ~82/s under ingest pressure.
Everything above that is a backlog, not a throughput.

**The endpoint's own ceiling is ~370/s.** Offering 400 got 370 accepted; offering
800 got *less* (266), which is the signature of a saturated service rather than a
faster one. Somewhere between 400 and 800 offered, more load buys less work.

**p99 at the sustainable rate is 411–845 ms, and p50 is 4 ms.** The median is what
the handler costs: verify a signature, parse 2.5 KB, two inserts, commit. The p99
is three orders of magnitude above the median, and that gap is the interesting
part of this whole run.

**It breaks by waiting, never by failing.** Zero HTTP errors at every plateau,
including the one offering 800/s at a service that could take 266. Nothing was
rejected, nothing 500'd, no delivery was accepted and then lost. Requests queued
and the queue grew. For a webhook consumer that is the right failure mode:
GitHub retries a 5xx and gives up eventually, but a slow 202 still ends with the
delivery recorded.

**Headroom against the design point is ~180×.** SPEC sizes this at 0.2–0.5
events/s; a real push produces six deliveries across ~40 seconds. Sustained 90/s
is 180 times the top of that range, on a quarter of the compute budget's worth of
software running on a laptop VM.

### The tail, and what it is not

At 200/s the p95 is 80 ms and the p99 is 1,512 ms: a small fraction of requests
take twenty times longer than the 95th percentile. Two candidates were tested.

**It is not the rollup.** The obvious suspect was the worker's once-a-minute sweep,
which recomputes a 90-day window per active repo, and the load-test repo had grown
to 60,822 job rows. Timed directly against that repo: **0.25 s cold, 0.09 s warm.**
Too small and too rare to be a p99 of 1.5 s. The guess was wrong, which is why it
was measured before being written down.

**The prime suspect is connection-pool queueing.** The API's engine allows
`pool_size=5` plus `max_overflow=5` (**ten connections**) with `pool_pre_ping`
adding a round trip before each checkout. A request that finds a free connection
commits in 4 ms; one that arrives with all ten busy waits for one, and at 200/s
k6 has hundreds of requests in flight. That shape (flat median, heavy tail,
no errors) is what a small pool behind a large arrival burst looks like.

**This is a hypothesis with a cheap test, and the test has not been run.** Raise
the pool, re-run the same ladder, and see whether the ceiling moves or the tail
flattens. Until then it is written here as a suspect and not as a cause.

Scenario 2 narrowed it without settling it. See "Where the time actually goes"
below. The API process is exonerated; the pool and Postgres are not separated.

---

## Scenario 2: dashboard read load

### Method

`loadtest/read_ladder.sh` walks the offered rate up, one k6 process per plateau,
30 seconds each after a discarded warm-up, k6 in a container on the compose
network posting to `http://app:8000`. Same shape as scenario 1, same reasons.

**One iteration is one page load, not one request.** `frontend/src/app/page.tsx`
issues six calls in two waves: `/api/repos` first, because which repo to show comes
out of it; then the flake board, the job list and the minutes attribution together;
then the timeline and the duration trend, which need the job name the board ranked
worst. A seventh, `/public/flaky`, is in the mix because it is the one read a
stranger can reach. Firing seven independent requests at a flat rate would exercise
the same endpoints with none of that serialisation, and what a page owner waits on
is the sum of the waves. **So the rate below is pages per second and the request
rate is seven times it.**

Every request carries the internal bearer token and an `X-Authorized-Repo-Ids`
header, because without them `/api` answers 401 and 400 respectively, and a run of
those would report a flattering number for a service that answered nothing. `setup()`
asserts once, up front, that all seven paths return 200 and non-empty bodies.

The target is the repo scenario 1 filled: **60,822 job rows and one rollup row**,
one day of history, one job name. Everything else in the database is the dogfooded
repo's 21 rows. The worker ran throughout, including its once-a-minute rollup sweep.

### Results

`pages/s` is offered; `req/s` is achieved. Latencies in milliseconds. `page` is the
whole six-call page load as the BFF would see it; the rest are per-endpoint p99s.

| pages/s | req/s | dropped | page p50 | page p95 | page p99 | errors |
|---|---|---|---|---|---|---|
| 2 | 14.4 | 0 | 99 | 180 | 234 | 0 |
| 5 | 35.3 | 0 | 85 | 195 | 547 | 0 |
| 10 | 69.8 | 0 | 80 | 114 | 196 | 0 |
| 20 | 136.3 | 0 | 90 | 2,133 | 3,208 | 0 |
| 40 | 251.8 | 35 | 2,263 | 4,153 | 5,073 | 0 |
| 80 | 211.3 | 1,188 | 13,723 | 24,233 | 27,255 | 0 |

**The last two rows are one sample each of a plateau that does not repeat.** Six
later runs of the 40 row, below, spread from 157 to 215 req/s (±30%), so read them
as "it is saturated and degrading" and not as numbers. Everything at 20 and below
reproduced within a few percent.

Per endpoint, at the sustainable rate (10 pages/s) and at the first plateau where
the tail breaks (20 pages/s):

| endpoint | source | p50 @10 | p99 @10 | p50 @20 | p99 @20 |
|---|---|---|---|---|---|
| `/api/repos` | raw jobs | 10.6 | 50.7 | 12.0 | 1,184 |
| `/api/repos/{id}/jobs` | raw jobs | 14.1 | 69.9 | 16.0 | 917 |
| `/api/repos/{id}/jobs/{name}/history` | raw jobs | 50.5 | 99.8 | 56.5 | 822 |
| `/api/repos/{id}/flaky` | rollup | 6.9 | 53.6 | 7.7 | 677 |
| `/api/repos/{id}/minutes` | rollup | 6.6 | 56.8 | 7.8 | 830 |
| `/api/repos/{id}/jobs/{name}/duration` | rollup | 5.4 | 20.1 | 7.9 | 773 |
| `/public/flaky` | rollup | 2.6 | 14.1 | 4.6 | 504 |

### What the numbers say

**Sustained read load is 10 page loads/second (70 requests/second) at a page p99
of 196 ms.** Below that the median is flat at ~85 ms and the tail stays under a
quarter second. The service answers a whole dashboard in under 200 ms, 99 times in
100, while serving seventy requests a second.

**It breaks between 10 and 20 pages/s, and it breaks in the tail first.** At 20 the
median is still 90 ms (indistinguishable from the idle case) while the p95 jumps
from 114 ms to 2,133 ms. Half the page loads are unaffected and the other half wait
seconds. At 40 the median itself goes to 2.3 s, which is the saturation point. At 80
achieved throughput *falls*, 252 req/s down to 211, the same "more load buys less
work" signature scenario 1 found above 400/s.

**Zero HTTP errors at every plateau, again**, including the one offering 80 pages/s
at a service that could deliver 30. Reads degrade by waiting, exactly as ingest does.

**The rollup is doing its job.** The three rollup-backed endpoints cost 5–7 ms at the
median against 10–50 ms for the three that touch raw job rows. `/public/flaky`, the
only one with no auth in front of it, is the cheapest thing here at 2.6 ms.

**The low plateaus have noisy p99s and the 5 pages/s row is the tell**: 547 ms,
worse than the 10 pages/s row's 196 ms. 151 page loads is a sample where the p99 is
roughly the second-worst observation, so one rollup sweep landing inside the window
moves it. The monotonic part of the curve starts at 10.

### Where the time actually goes

**Every raw-fact read is a sequential scan of the whole repo's job rows.** `EXPLAIN
ANALYZE` on the three, against 60,822 rows:

- `/api/repos/{id}/jobs`: `Parallel Seq Scan on jobs`, 60,822 rows read to return
  50, `top-N heapsort`, 45 ms. `jobs` has indexes for the two detection signals and
  one on `updated_at`; **nothing supports `(repo_id, started_at desc)`**.
- the timeline's commit-picking subquery: `Seq Scan` then `HashAggregate` over
  **60,326 groups, which spills: `Batches: 5 … Disk Usage: 752kB`**, 104 ms. It
  groups every commit in the window to take the newest 30.
- `/api/repos`: `Nested Loop Left Join` over all 60,822 rows to produce one count,
  25 ms.

So these three are **O(the repo's job rows), not O(the page size)**, and the constant
is small enough to hide at 60k rows. SPEC sizes this at ~2M rows in 90 days, which is
thirty times the row count these numbers came from.

**The API process is not the bottleneck.** Sampled during a 20 pages/s plateau, when
DB-backed p99s were 0.8–1.7 s, `/healthz` (which touches no database and checks out
no connection) answered in **2.5 ms to 92 ms**, median ~35 ms. An event loop that
was itself saturated would have delayed it too. Over the same window `docker stats`
read the app container at **45–110% CPU** and Postgres at **149–337%**: the database
is burning three cores to the app's one.

That exonerates the API process and narrows scenario 1's open hypothesis without
settling it. The queueing is at the database boundary, but "waiting for one of ten
pooled connections" and "waiting for a Postgres that is genuinely busy" both fit,
and at 337% CPU on sequential scans the second is now at least as plausible as the
first. **The experiment that separates them is to add the missing index and re-run
this ladder unchanged**: if the ceiling moves, it was the work; if only the tail
flattens, it was the queue. That experiment was run (see below) and neither
happened, for a reason the design of the experiment had missed.

### What this means for production

Nothing here transfers as a rate. See the box at the top; this ran on 8 CPUs and the
task has a quarter of one. What transfers is the shape: **the read path's cost grows
with the repo's history on three endpoints and stays flat on four**, and the four
flat ones are the rollup's. That is the argument for the rollup restated as a
measurement rather than a design claim.

### Scenario 2a: the same ladder, with the missing index

`ix_jobs_repo_recent` on `jobs (repo_id, started_at DESC NULLS LAST, id DESC)`,
migration `b8e5309fa14c`. The ordering is spelled out because a b-tree only satisfies
a sort it matches exactly, and plain `DESC` would mean NULLS FIRST while the query
asks for NULLS LAST. Nothing else changed, and the ladder was re-run unmodified.

**The query did exactly what it should. The system did not move at all.**

| | before | after |
|---|---|---|
| the query alone, `EXPLAIN ANALYZE` | 45.4 ms, `Parallel Seq Scan`, 60,822 rows read to return 50 | **1.3 ms**, `Index Scan`, 50 rows read: 0.20 ms once its pages are cached |
| `/api/repos/{id}/jobs` p50 in a loaded page, 10 pages/s | 14.1 ms | 7.7 ms |
| `/api/repos/{id}/jobs` p99, 10 pages/s | 69.9 ms | 58.8 ms |
| page p99, 10 pages/s | 196 ms | 217 ms |
| achieved req/s, 10 pages/s | 69.8 | 70.1 |
| page p99, 20 pages/s | 3,208 ms | 3,217 ms |
| achieved req/s, 20 pages/s | 136.3 | 139.4 |

**A 35× faster query bought a 2× faster endpoint and a 0× faster page.** That gap is
the finding. In isolation the scan was 45 ms of the endpoint's cost; inside a page at
70 req/s the endpoint only fell from 14.1 ms to 7.7 ms, because most of what it was
spending was never the query. The ceiling is where it was: still sustainable at 10
page loads/s, still breaking in the tail between 10 and 20.

### Why this could not settle the pool question, which is the useful part

**The experiment was designed before the query plans were known, and it removed the
cheapest of the three scans.** `/api/repos` still nested-loops all 60,822 rows for one
count, and the timeline still hash-aggregates 60,326 groups and spills to disk. Those
cost 25 ms and 104 ms; the one that got fixed cost 45 ms. Removing a third of the work
and finding the ceiling unmoved is consistent with "still bound by the remaining work"
*and* with "never bound by the work at all", which is precisely the two things it was
supposed to tell apart.

What still points at the pool is a detail in scenario 2's own per-endpoint table.
**At saturation all seven endpoints converge on the same latency.** At 40 pages/s
before the index they read 403, 510, 523, 517, 544, 509 and 502 ms, including
`/public/flaky`, which costs 2.6 ms when idle and reads a five-row table. A fixed
delay applied equally to a 2.6 ms query and a 55 ms one is a queue in front of the
work, not the work. A merely busy Postgres would still return the cheap query
cheaply; ten pooled connections make everything wait the same.

**The decisive experiment is now the pool itself**, not another index: raise
`pool_size` in `app/db.py` from 5+5 and re-run this ladder. That is one variable, and
unlike an index it cannot be confounded by which queries happen to be in the mix.

### The saturated plateaus are not a measurement, and here is the proof

The first re-run appeared to show throughput at 40 pages/s falling from 252 to 182
req/s, a 28% regression from adding an index, which is not a thing an index does. So
the plateau was run six times, dropping and restoring the index around the middle
pair, rather than published:

| | run | req/s | page p50 | page p99 |
|---|---|---|---|---|
| index present | A1 | 156.8 | 9,769 | 20,750 |
| index present | A2 | 165.1 | 10,042 | 19,520 |
| **index dropped** | B1 | 172.7 | 7,706 | 18,248 |
| **index dropped** | B2 | 170.8 | 9,490 | 18,229 |
| index restored | A3 | 214.9 | 4,552 | 10,502 |
| index restored | A4 | 201.2 | 5,952 | 12,490 |

**Both "without" runs land inside the spread of the four "with" runs**, and the
highest two came last, which is drift over the sequence rather than any effect of the
index. Run-to-run variation at 40 pages/s is ±30% and swamps what is being measured;
the apparent regression was noise, and so was the apparent 252. Above the ceiling this
box measures its own scheduler. The reproducible region is 20 pages/s and below.

---

## Not yet measured

- **The pool experiment.** Raise `pool_size`/`max_overflow` in `app/db.py` and re-run
  the read ladder. It is the one variable that separates a queue in front of the
  database from a database that is genuinely busy, and both scenarios now end on it.
- **`/public/flaky` against a board with rows in it.** Both repos in the local
  database are private, so the public board legitimately returned `[]` and its
  numbers above are a floor: real HTTP and routing cost, no result rows. The
  endpoint reads the same rollup the other three do, so the floor is close, but it
  is a floor.
- **The tunnel smoke test**: a low rate over `api.flakehound.com`, so this file is
  explicit about which numbers crossed Cloudflare and which did not.
- **Chaos** (SPEC scenario 3): kill the worker mid-message, make Postgres
  unreachable, replay one delivery 100×, send a bad signature, send a malformed
  payload. The replay and the bad signature already have tests; this is the
  by-hand pass with observed behaviour written down.
- **The task's own numbers**, from inside the VPC against 0.25 vCPU.

---

## Reproducing

```sh
docker compose up -d --build
./loadtest/ladder.sh                          # 50..1600/s, k6 on the compose network
RATES="400" DURATION=60s ./loadtest/ladder.sh # one plateau, then the drain rate
MODE=host ./loadtest/ladder.sh                # via the published port, for comparison

./loadtest/read_ladder.sh                     # 2..80 dashboard page loads/s
RATES="10" DURATION=60s ./loadtest/read_ladder.sh
REPO_ID=1352471967 ./loadtest/read_ladder.sh  # against real GitHub data instead
```

Both harnesses read their credential out of `.env` into the environment and hand
docker the variable's *name*, so no secret reaches a command line: the webhook
ladder needs `GITHUB_WEBHOOK_SECRET` because it signs every request, and the read
ladder needs `INTERNAL_API_TOKEN` because `/api` answers 401 without it. They point
at whatever database compose is pointing at. **Never run either against production.**

Two things it took a wrong measurement to learn, both now fixed in the harness and
worth knowing before trusting a run:

- **Size the VU budget by concurrency, not by rate.** An arrival rate of R with
  latency L needs R×L concurrent VUs. A budget set as a multiple of R is really a
  budget for the latency you assumed, and when latency rose the generator ran out
  of VUs and reported its own ceiling as the service's. `dropped_iterations` is
  the tell; above 200/s here it still binds, so achieved rates in the overloaded
  rows are lower bounds.
- **Make the fact ids unique per run, not just the delivery ids.** k6 restarts
  `__VU` and `__ITER` in every process, so a ladder sent distinct deliveries
  carrying repeated job and run ids: the upserts updated rows instead of inserting
  them, and the drain rate measured the cheaper path. The giveaway was 30,239
  deliveries producing 1,999 new job rows. Deliveries rising 1:1 with jobs is the
  check that the harness is honest.
- **A read test can be fast because it answered nothing.** A 401, a 400 for the
  missing authorization header, or a 404 for an unauthorized repo all return in
  microseconds and all look like a fast service in a percentile. `read_load.js`
  therefore asserts in `setup()` that every path returns 200 *and* a non-empty
  body before a single measured request is sent.

Counting the requests afterwards is worth the thirty seconds it costs. Last turn
recorded a harness that appeared to have run four times when it was invoked once,
so this turn's runs were reconciled against the container's own access log:
**30,358 GET lines against 30,350 expected** across nine k6 processes and the curl
probes, with the residual being setup's health checks. The harness ran exactly as
often as it was asked to.
