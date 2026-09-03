/**
 * Auth.js's own endpoints: the GitHub redirect, the callback, the session, the
 * CSRF token, sign-out. The callback path here must match the one registered on the
 * GitHub App — `/api/auth/callback/github`.
 */

import { handlers } from "@/auth";

export const { GET, POST } = handlers;
