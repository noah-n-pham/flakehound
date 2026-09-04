// SPEC section 10, scenario 1: validly-signed `workflow_job` deliveries at a fixed
// arrival rate, aimed straight at the container.
//
// Never point this at api.flakehound.com. Driving hundreds of requests per second
// through Cloudflare measures their edge and the tunnel, and load from one IP gets
// challenged. The tunnel gets a low-rate smoke test instead, reported separately.
//
//   RATE=200 DURATION=30s BASE_URL=http://app:8000 k6 run webhook_throughput.js
//
// One run is one plateau. The ladder that finds the ceiling lives in ladder.sh,
// because a separate process per rate keeps each rate's percentiles its own.
import http from 'k6/http';
import crypto from 'k6/crypto';
import { check } from 'k6';
import { Counter } from 'k6/metrics';

const BASE_URL = __ENV.BASE_URL || 'http://app:8000';
const SECRET = __ENV.WEBHOOK_SECRET;
const RATE = Number(__ENV.RATE || 100);
const DURATION = __ENV.DURATION || '30s';

// A VU is busy for one request, so the concurrency an arrival rate needs is
// rate x latency: 400/s at 10ms needs 4, and at 1s needs 400. Sizing the budget
// as a multiple of the rate therefore sizes it for the latency you assumed,
// which is how the first run of this made k6 the bottleneck and reported it as
// the service's ceiling. A flat, generous budget instead, the same at every
// plateau, so `dropped_iterations` means the service stopped keeping up.
const PRE_ALLOCATED_VUS = Number(__ENV.VUS || 100);
const MAX_VUS = Number(__ENV.MAX_VUS || 600);

// Ids no real installation can collide with, so load rows stay identifiable and
// deletable after the run.
const INSTALLATION_ID = 999000001;
const REPO_ID = 999000002;
const RUN_ID_BASE = 9000000000;
const JOB_ID_BASE = 9500000000;

const duplicates = new Counter('deliveries_duplicate');
const accepted = new Counter('deliveries_accepted');

export const options = {
  scenarios: {
    webhooks: {
      // Open model: k6 offers RATE per second whatever the service does with it.
      // A closed model would let rising latency quietly lower the offered rate,
      // which is exactly the number being measured.
      executor: 'constant-arrival-rate',
      rate: RATE,
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: PRE_ALLOCATED_VUS,
      maxVUs: MAX_VUS,
    },
  },
  // These define "broke" objectively rather than by eye, and do not abort: a
  // failing plateau is a result, and its shape is worth seeing in full.
  thresholds: {
    http_req_failed: [{ threshold: 'rate<0.01', abortOnFail: false }],
    checks: [{ threshold: 'rate>0.99', abortOnFail: false }],
    http_req_duration: [{ threshold: 'p(99)<1000', abortOnFail: false }],
    // Not assertions. A threshold is the only way to make k6 materialise a
    // tagged submetric, and the reported numbers must exclude setup's one
    // health check. At a short plateau it is enough to move a percentile.
    'http_reqs{name:webhook}': ['count>=0'],
    'http_req_duration{name:webhook}': ['max>=0'],
    'http_req_failed{name:webhook}': ['rate>=0'],
  },
  discardResponseBodies: false,
  summaryTrendStats: ['avg', 'med', 'p(95)', 'p(99)', 'max'],
};

export function setup() {
  if (!SECRET) {
    throw new Error('WEBHOOK_SECRET is required; the handler verifies every signature');
  }
  const res = http.get(`${BASE_URL}/healthz`);
  if (res.status !== 200) {
    throw new Error(`${BASE_URL}/healthz answered ${res.status}, not 200`);
  }
  // Stamped into every delivery id so a rerun cannot collide with the last run's
  // rows, which the deliveries primary key would answer as duplicates.
  //
  // `base` does the same job for the *fact* ids, and it is not decoration. k6
  // restarts __VU and __ITER in every process, so a ladder of plateaus without it
  // sends distinct deliveries carrying repeated job and run ids. The upserts then
  // update rows instead of inserting them, and the worker's drain rate comes out
  // measuring the cheaper of the two paths. It read 30,239 deliveries against 1,999
  // new job rows before this existed.
  const now = Date.now();
  return { tag: `lt${now}`, base: Math.floor(now / 1000) % 100000 };
}

// Shaped like the real thing, including a step list, because the body is what
// gets HMAC'd and JSON-parsed. A 300-byte stub would measure a payload nobody sends.
function payload(runId, jobId, sha) {
  const steps = [];
  for (let i = 1; i <= 10; i += 1) {
    steps.push({
      name: `step ${i}`,
      status: 'completed',
      conclusion: 'success',
      number: i,
      started_at: '2026-09-03T09:00:00Z',
      completed_at: '2026-09-03T09:00:30Z',
    });
  }
  return JSON.stringify({
    action: 'completed',
    workflow_job: {
      id: jobId,
      run_id: runId,
      run_attempt: 1,
      workflow_name: 'ci',
      head_branch: 'main',
      head_sha: sha,
      name: 'test (ubuntu-latest, 3.12)',
      status: 'completed',
      conclusion: 'success',
      started_at: '2026-09-03T09:00:00Z',
      completed_at: '2026-09-03T09:04:12Z',
      runner_name: 'GitHub Actions 2',
      runner_group_name: 'GitHub Actions',
      labels: ['ubuntu-latest'],
      steps,
    },
    repository: {
      id: REPO_ID,
      name: 'flakehound-loadtest',
      full_name: 'loadtest/flakehound-loadtest',
      private: true,
      default_branch: 'main',
      owner: { id: 999000003, login: 'loadtest', type: 'Organization' },
    },
    installation: { id: INSTALLATION_ID },
    sender: { id: 999000003, login: 'loadtest' },
  });
}

export default function (data) {
  // Unique per iteration, per VU, per run. A repeated delivery id would be
  // answered by the primary key, so a flood of one id measures dedup, not ingest.
  // VU <= maxVUs and ITER < 100000 keeps seq below the 1e8 the run offset is
  // spaced by, and the whole id well inside a float64's exact integer range.
  const seq = __VU * 100000 + __ITER;
  const runId = RUN_ID_BASE + data.base * 100000000 + seq;
  const jobId = JOB_ID_BASE + data.base * 100000000 + seq;
  const sha = `${data.tag}${seq}`.padEnd(40, '0').slice(0, 40);
  const body = payload(runId, jobId, sha);

  const res = http.post(`${BASE_URL}/webhooks/github`, body, {
    headers: {
      'Content-Type': 'application/json',
      'X-GitHub-Event': 'workflow_job',
      'X-GitHub-Delivery': `${data.tag}-${seq}`,
      'X-Hub-Signature-256': `sha256=${crypto.hmac('sha256', SECRET, body, 'hex')}`,
      'User-Agent': 'GitHub-Hookshot/loadtest',
    },
    tags: { name: 'webhook' },
  });

  const queued = check(res, {
    'status is 202': (r) => r.status === 202,
    'delivery was new': (r) => r.body && r.body.indexOf('"queued"') !== -1,
  });
  if (queued) {
    accepted.add(1);
  } else if (res.body && res.body.indexOf('duplicate') !== -1) {
    duplicates.add(1);
  }
}

// One line per run, so the ladder is readable as a table instead of six screens
// of default summary.
export function handleSummary(data) {
  const m = data.metrics;
  const n = (metric, field) => (m[metric] && m[metric].values[field]) || 0;
  const round = (x, places = 1) => Number(x.toFixed(places));

  const reqs = 'http_reqs{name:webhook}';
  const dur = 'http_req_duration{name:webhook}';

  const result = {
    offered_rate: RATE,
    duration: DURATION,
    requests: n(reqs, 'count'),
    achieved_rate: round(n(reqs, 'rate')),
    // Iterations k6 could not start on time: the generator ran out of VUs, so a
    // shortfall here is k6's ceiling and not the service's.
    dropped_iterations: n('dropped_iterations', 'count'),
    accepted: n('deliveries_accepted', 'count'),
    duplicates: n('deliveries_duplicate', 'count'),
    http_failed_rate: round(n('http_req_failed{name:webhook}', 'rate'), 4),
    p50_ms: round(n(dur, 'med'), 2),
    p95_ms: round(n(dur, 'p(95)'), 2),
    p99_ms: round(n(dur, 'p(99)'), 2),
    max_ms: round(n(dur, 'max'), 2),
    body_bytes: Math.round(n('data_sent', 'count') / Math.max(1, n(reqs, 'count'))),
    thresholds_failed: Object.entries(m)
      .flatMap(([name, metric]) =>
        Object.entries(metric.thresholds || {})
          .filter(([, t]) => !t.ok)
          .map(([source]) => `${name}:${source}`)
      )
      .join(','),
  };
  return { stdout: `RESULT ${JSON.stringify(result)}\n` };
}
