// SPEC section 10, scenario 2: dashboard read load, aimed straight at the container.
//
// Never point this at api.flakehound.com, for the reason webhook_throughput.js gives.
//
//   RATE=10 DURATION=30s BASE_URL=http://app:8000 k6 run read_load.js
//
// One iteration is **one dashboard page load, not one request**, because that is the
// unit a user waits on. `frontend/src/app/page.tsx` issues six calls in two waves —
// `/api/repos` first, because which repo to show comes out of it, then the board, the
// job list and the minutes together, then the timeline and the duration trend, which
// need the job name the board ranked worst. Firing six independent requests at a flat
// rate would measure the same endpoints with none of that serialisation, and the
// number a page owner cares about is the sum of the waves.
//
// So `page_duration` is the headline and the per-endpoint percentiles say where it
// went. RATE is page loads per second; the request rate is six times it.
import http from 'k6/http';
import { check } from 'k6';
import { Trend } from 'k6/metrics';

const BASE_URL = __ENV.BASE_URL || 'http://app:8000';
const TOKEN = __ENV.INTERNAL_API_TOKEN;
const RATE = Number(__ENV.RATE || 10);
const DURATION = __ENV.DURATION || '30s';

// The repo the webhook ladder filled, and the only job name it sends. Overridable so
// this can be pointed at a repo with real GitHub data instead of synthetic rows.
const REPO_ID = Number(__ENV.REPO_ID || 999000002);
const JOB_NAME = __ENV.JOB_NAME || 'test (ubuntu-latest, 3.12)';
const WINDOW_DAYS = Number(__ENV.WINDOW_DAYS || 30);
const LIMIT = Number(__ENV.LIMIT || 50);
const COMMITS = Number(__ENV.COMMITS || 30);

// Every repo id the caller is allowed to see. The BFF sends this on every read and a
// missing header is a 400, so leaving it off would measure the error path.
const AUTHORIZED = __ENV.AUTHORIZED_REPO_IDS || String(REPO_ID);

// Named so the summary reads as a table. `public` is the only one that needs no token
// and it is in the mix because it is the one read a stranger can reach.
const ENDPOINTS = ['repos', 'flaky', 'jobs', 'minutes', 'history', 'duration', 'public'];

const pageDuration = new Trend('page_duration', true);

export const options = {
  scenarios: {
    reads: {
      // Open model, same reason as the webhook ladder: a closed model would let
      // rising latency lower the offered rate, which is the number being measured.
      executor: 'constant-arrival-rate',
      rate: RATE,
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: Number(__ENV.VUS || 100),
      maxVUs: Number(__ENV.MAX_VUS || 600),
    },
  },
  thresholds: Object.assign(
    {
      http_req_failed: [{ threshold: 'rate<0.01', abortOnFail: false }],
      checks: [{ threshold: 'rate>0.99', abortOnFail: false }],
      page_duration: [{ threshold: 'p(99)<2000', abortOnFail: false }],
    },
    // Not assertions. A threshold is the only way to make k6 materialise a tagged
    // submetric, and per-endpoint percentiles are the whole point of this run.
    ...ENDPOINTS.map((name) => ({
      [`http_req_duration{name:${name}}`]: ['max>=0'],
      [`http_reqs{name:${name}}`]: ['count>=0'],
      [`http_req_failed{name:${name}}`]: ['rate>=0'],
    })),
  ),
  discardResponseBodies: false,
  summaryTrendStats: ['avg', 'med', 'p(95)', 'p(99)', 'max'],
};

function authed(name) {
  return {
    headers: {
      Authorization: `Bearer ${TOKEN}`,
      'X-Authorized-Repo-Ids': AUTHORIZED,
    },
    tags: { name },
  };
}

const job = encodeURIComponent(JOB_NAME);
const paths = {
  repos: '/api/repos',
  flaky: `/api/repos/${REPO_ID}/flaky?window_days=${WINDOW_DAYS}&limit=${LIMIT}`,
  jobs: `/api/repos/${REPO_ID}/jobs?limit=${LIMIT}`,
  minutes: `/api/repos/${REPO_ID}/minutes?group_by=workflow&window_days=${WINDOW_DAYS}&limit=${LIMIT}`,
  history: `/api/repos/${REPO_ID}/jobs/${job}/history?window_days=${WINDOW_DAYS}&limit=${COMMITS}`,
  duration: `/api/repos/${REPO_ID}/jobs/${job}/duration?window_days=${WINDOW_DAYS}`,
  public: `/public/flaky?window_days=${WINDOW_DAYS}&limit=${LIMIT}`,
};

export function setup() {
  if (!TOKEN) {
    throw new Error('INTERNAL_API_TOKEN is required; /api answers 401 without it');
  }
  const health = http.get(`${BASE_URL}/healthz`);
  if (health.status !== 200) {
    throw new Error(`${BASE_URL}/healthz answered ${health.status}, not 200`);
  }
  // A 401 or a 404 is fast, and a run of them would report a flattering number for a
  // service that answered nothing. Prove every path returns real rows once, up front,
  // rather than discovering it in the percentiles.
  for (const name of ENDPOINTS) {
    const res = http.get(`${BASE_URL}${paths[name]}`, authed(name));
    if (res.status !== 200) {
      throw new Error(`${paths[name]} answered ${res.status}, not 200`);
    }
    // `public` is allowed to be empty: it filters `private = false`, and a local
    // database of private repos legitimately has nothing to show.
    if (name !== 'public' && res.body === '[]') {
      throw new Error(`${paths[name]} returned no rows; it would measure an empty scan`);
    }
  }
  return {};
}

export default function () {
  const start = Date.now();

  const repos = http.get(`${BASE_URL}${paths.repos}`, authed('repos'));

  // The three the page fetches together once it knows the repo, batched here too so
  // the connection concurrency matches what the BFF actually creates.
  const wave = http.batch([
    ['GET', `${BASE_URL}${paths.flaky}`, null, authed('flaky')],
    ['GET', `${BASE_URL}${paths.jobs}`, null, authed('jobs')],
    ['GET', `${BASE_URL}${paths.minutes}`, null, authed('minutes')],
  ]);

  // These two wait on the board: which job the page draws is whichever it ranked worst.
  const tail = http.batch([
    ['GET', `${BASE_URL}${paths.history}`, null, authed('history')],
    ['GET', `${BASE_URL}${paths.duration}`, null, authed('duration')],
  ]);

  const publicBoard = http.get(`${BASE_URL}${paths.public}`, authed('public'));

  pageDuration.add(Date.now() - start);

  const all = [repos, ...wave, ...tail, publicBoard];
  check(all, {
    'every response is 200': (rs) => rs.every((r) => r.status === 200),
    'repo list is not empty': () => repos.body !== '[]',
  });
}

// One line per run, so the ladder is readable as a table.
export function handleSummary(data) {
  const m = data.metrics;
  const n = (metric, field) => (m[metric] && m[metric].values[field]) || 0;
  const round = (x, places = 1) => Number(x.toFixed(places));

  const perEndpoint = {};
  for (const name of ENDPOINTS) {
    const dur = `http_req_duration{name:${name}}`;
    perEndpoint[name] = {
      p50: round(n(dur, 'med'), 1),
      p95: round(n(dur, 'p(95)'), 1),
      p99: round(n(dur, 'p(99)'), 1),
      max: round(n(dur, 'max'), 1),
      failed: round(n(`http_req_failed{name:${name}}`, 'rate'), 4),
    };
  }

  const result = {
    offered_pages_per_s: RATE,
    duration: DURATION,
    // From `iterations` rather than from `page_duration`: k6 filters a trend's
    // reported values down to `summaryTrendStats`, so a trend has no count here.
    pages: n('iterations', 'count'),
    achieved_pages_per_s: round(n('iterations', 'rate')),
    requests: n('http_reqs', 'count'),
    achieved_req_per_s: round(n('http_reqs', 'rate')),
    // Iterations k6 could not start on time: a shortfall here is the generator's
    // ceiling, not the service's.
    dropped_iterations: n('dropped_iterations', 'count'),
    http_failed_rate: round(n('http_req_failed', 'rate'), 4),
    page_p50_ms: round(n('page_duration', 'med')),
    page_p95_ms: round(n('page_duration', 'p(95)')),
    page_p99_ms: round(n('page_duration', 'p(99)')),
    page_max_ms: round(n('page_duration', 'max')),
    endpoints: perEndpoint,
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
