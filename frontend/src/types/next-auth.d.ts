/**
 * Auth.js's own types carry no GitHub login, and the session shape is worth
 * checking at compile time rather than reaching for `any` at each use.
 */

import type { DefaultSession } from "next-auth";

declare module "next-auth" {
  interface Session {
    user?: DefaultSession["user"] & { login?: string };
  }

  interface Profile {
    login?: string;
  }
}

/**
 * `@auth/core/jwt` and not `next-auth/jwt`: the latter is a bare `export *` of the
 * former, and augmenting a re-export declares a new interface rather than merging
 * into the original. `JWT extends Record<string, unknown>`, so getting this wrong
 * does not fail — every field silently types as `unknown`.
 */
declare module "@auth/core/jwt" {
  interface JWT {
    login?: string;
    /** The GitHub user access token. Server-side only — never copied to the session. */
    accessToken?: string;
  }
}
