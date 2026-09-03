/**
 * Resolving which repos the signed-in person may see.
 *
 * This is the only place the *user's* GitHub token is used. It is read out of the
 * session JWT rather than off the session object, because the session object is
 * what `auth()` hands to any caller, and this token should never travel that far.
 * Server-side only.
 *
 * Two endpoints are needed, not one:
 *
 *     GET /user/installations                    → installations this user can reach
 *     GET /user/installations/{id}/repositories  → the repos in one, *for this user*
 *
 * The second is not the same as "every repo in the installation". On an organisation
 * install, GitHub returns only the repos this particular user can see, which is the
 * whole point — the installation is the org's, the authorization is the person's.
 */

import { cookies } from "next/headers";
import { decode } from "next-auth/jwt";

const API = "https://api.github.com";
const CACHE_TTL_MS = 5 * 60 * 1000;
const PER_PAGE = 100;

/**
 * In-process, keyed by GitHub user id, five minutes.
 *
 * In process rather than in Redis because there is one server process and a cache
 * server would earn nothing here; in process rather than in the JWT because
 * a server component cannot write cookies, so a JWT-held cache could never be
 * refreshed without bouncing the user through GitHub again.
 */
const cache = new Map<string, { repoIds: number[]; fetchedAt: number }>();

/** Auth.js prefixes the cookie with `__Secure-` only when the site is served over TLS. */
const COOKIE_NAMES = ["__Secure-authjs.session-token", "authjs.session-token"] as const;

function required(name: string): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(`${name} is not set. The authorized repo set cannot be resolved.`);
  }
  return value;
}

/**
 * The decoded session JWT, including the fields the session object deliberately omits.
 * The salt Auth.js signs with is the cookie name, so the two have to be tried together.
 */
async function sessionJwt() {
  const jar = await cookies();
  const secret = required("AUTH_SECRET");

  for (const name of COOKIE_NAMES) {
    const cookie = jar.get(name);
    if (cookie) {
      return await decode({ token: cookie.value, secret, salt: name });
    }
  }
  return null;
}

async function githubGet<T>(path: string, accessToken: string): Promise<T> {
  const response = await fetch(`${API}${path}`, {
    headers: {
      Authorization: `Bearer ${accessToken}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
    },
    cache: "no-store",
  });

  if (!response.ok) {
    // Deliberately does not include the response body: it is GitHub's error text about
    // a user's own installations and has no business in this server's logs.
    throw new Error(`GitHub GET ${path} returned ${response.status}`);
  }
  return (await response.json()) as T;
}

async function installationIds(accessToken: string): Promise<number[]> {
  const ids: number[] = [];
  for (let page = 1; ; page += 1) {
    const body = await githubGet<{ installations: { id: number }[] }>(
      `/user/installations?per_page=${PER_PAGE}&page=${page}`,
      accessToken,
    );
    ids.push(...body.installations.map((installation) => installation.id));
    if (body.installations.length < PER_PAGE) return ids;
  }
}

async function repoIdsIn(installationId: number, accessToken: string): Promise<number[]> {
  const ids: number[] = [];
  for (let page = 1; ; page += 1) {
    const body = await githubGet<{ repositories: { id: number }[] }>(
      `/user/installations/${installationId}/repositories?per_page=${PER_PAGE}&page=${page}`,
      accessToken,
    );
    ids.push(...body.repositories.map((repository) => repository.id));
    if (body.repositories.length < PER_PAGE) return ids;
  }
}

/**
 * Every repo id the signed-in user may read, or `null` if nobody is signed in.
 *
 * `null` and `[]` mean different things and both are real: nobody signed in, versus
 * someone signed in who has not installed the App anywhere. The caller must not
 * collapse them, and neither may ever be read as "no filter".
 */
export async function authorizedRepoIds(): Promise<number[] | null> {
  const token = await sessionJwt();
  const accessToken = token?.accessToken;
  if (!token || !accessToken) return null;

  const key = String(token.sub ?? token.login ?? "");
  const hit = cache.get(key);
  if (hit && Date.now() - hit.fetchedAt < CACHE_TTL_MS) {
    return hit.repoIds;
  }

  const installations = await installationIds(accessToken);
  const perInstallation = await Promise.all(
    installations.map((id) => repoIdsIn(id, accessToken)),
  );
  // Deduplicated: one repo can be reachable through more than one installation, and a
  // duplicated id would be sent twice in the header for no reason.
  const repoIds = [...new Set(perInstallation.flat())];

  cache.set(key, { repoIds, fetchedAt: Date.now() });
  return repoIds;
}

/** Test seam and a way to force a re-resolve after an install changes. */
export function clearAuthorizedRepoCache(): void {
  cache.clear();
}
