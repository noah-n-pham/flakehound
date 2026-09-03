# BENCHMARKS

Real numbers from real runs. Every figure here was produced by a command in
`loadtest/`, and any figure that is not yet measured says so instead of being
estimated.

**Measured so far:** scenario 1, webhook ingest throughput, against the container.
**Not yet measured:** read load, the tunnel smoke test, and the chaos pass.

---

## The box these numbers came from, which is not production

Everything below ran on one Apple-silicon laptop: k6, uvicorn, the worker and
Postgres 16 all inside Docker Desktop's VM, **8 CPUs and 8.2 GB between them**.
The load generator was co-located with the target, so it competed for those CPUs.

The production task is **256 CPU units — a quarter of one vCPU — and 512 MB**. It
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

## Scenario 1 — webhook ingest throughput

### Method

`loadtest/ladder.sh` walks the offered rate up, one k6 process per plateau, 30
seconds each, after a discarded 5-second warm-up. k6 runs in a container on the
compose network and posts to `http://app:8000`, so there is no host port
forwarding in the path.

Each iteration posts a `workflow_job` body of **2,520 bytes** — ten steps, shaped
like the real thing, because the body is what gets HMAC'd and JSON-parsed — signed
per iteration with HMAC-SHA256 and carrying a **unique `X-GitHub-Delivery`**. That
last detail is load-bearing: the deliveries table's primary key *is* the dedup
mechanism, so a flood of one delivery id would measure one insert and N−1 conflicts.
Zero duplicates were recorded at every plateau, which is the proof it worked.

The executor is `constant-arrival-rate`: an open model, where k6 offers the rate
regardless of what the service does with it. A closed model would let rising
latency quietly lower the offered rate, which is the number being measured.

The database was not empty. It began the run holding **146,172 deliveries and
30,458 job rows**, with the queue fully drained. The worker ran throughout, as it
does in production — one container, three processes.

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

**18,102 queue rows in 201 seconds — 90 rows/second.**

Over the whole run, deliveries rose by 30,385 and job rows rose by **30,385**. One
delivery, one fact, no drift, and nothing lost.

### What the numbers say

**Sustained ingest is 90/s, and the limit is the worker, not the endpoint.** The
API accepts far more than the worker can drain, so "sustained" is set by the drain
rate: 90 rows/second, measured directly. At 50/s the queue ends a 30-second
plateau 17 rows deep — the worker keeps up. At 100/s it ends 549 deep, which is
the queue growing at ~18/s and the drain running at ~82/s under ingest pressure.
Everything above that is a backlog, not a throughput.

**The endpoint's own ceiling is ~370/s.** Offering 400 got 370 accepted; offering
800 got *less* — 266 — which is the signature of a saturated service rather than a
faster one. Somewhere between 400 and 800 offered, more load buys less work.

**p99 at the sustainable rate is 411–845 ms, and p50 is 4 ms.** The median is what
the handler costs: verify a signature, parse 2.5 KB, two inserts, commit. The p99
is three orders of magnitude above the median, and that gap is the interesting
part of this whole run.

**It breaks by waiting, never by failing.** Zero HTTP errors at every plateau,
including the one offering 800/s at a service that could take 266. Nothing was
rejected, nothing 500'd, no delivery was accepted and then lost. Requests queued
and the queue grew. For a webhook consumer that is the right failure mode —
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
`pool_size=5` plus `max_overflow=5` — **ten connections** — with `pool_pre_ping`
adding a round trip before each checkout. A request that finds a free connection
commits in 4 ms; one that arrives with all ten busy waits for one, and at 200/s
k6 has hundreds of requests in flight. That shape — flat median, heavy tail,
no errors — is what a small pool behind a large arrival burst looks like.

**This is a hypothesis with a cheap test, and the test has not been run.** Raise
the pool, re-run the same ladder, and see whether the ceiling moves or the tail
flattens. Until then it is written here as a suspect and not as a cause.

---

## Not yet measured

- **Read load** (SPEC scenario 2): dashboard endpoints at sustained RPS, direct to
  the container, p99.
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
```

The harness reads `GITHUB_WEBHOOK_SECRET` from `.env` and signs every request, so
it exercises the real verification path. It writes to whatever database compose is
pointing at — **never run it against production.**

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
