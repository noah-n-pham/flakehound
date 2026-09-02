/**
 * Auth.js's own endpoints: the GitHub redirect, the callback, the session, the
 * CSRF token, sign-out. The callback path here is what H-013 registered on the
 * App — `/api/auth/callback/github`.
 */

import { handlers } from "@/auth";

export const { GET, POST } = handlers;
