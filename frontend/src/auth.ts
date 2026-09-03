/**
 * GitHub login. JWT sessions, **no database adapter** — the session is a
 * signed cookie, so login adds no tables to RDS and the BFF stays stateless.
 *
 * Identity is forced to be GitHub's: this is a GitHub App, so GitHub is the only
 * thing that can answer "which repos may this person see". The session therefore
 * carries the user's access token, which is what the next slice exchanges for the
 * installation list that becomes the authorized repo set.
 *
 * Nothing here reads `AUTH_GITHUB_ID`/`AUTH_GITHUB_SECRET`, Auth.js's implicit
 * names. The credentials are the *App's* client credentials and are named after it
 * in one place, so `.env` says what they belong to.
 */

import NextAuth from "next-auth";
import GitHub from "next-auth/providers/github";

function required(name: string): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(`${name} is not set. Login cannot be configured without it.`);
  }
  return value;
}

export const { handlers, signIn, signOut, auth } = NextAuth({
  providers: [
    GitHub({
      clientId: required("GITHUB_CLIENT_ID"),
      clientSecret: required("GITHUB_CLIENT_SECRET"),
      // The provider's default `userinfo` falls through to `/user/emails`
      // whenever the profile carries no public email — and this App has no
      // permission to read emails, so that is a 403 in the middle of a login
      // that only ever wanted a username. Authorization comes from the
      // installations API, not from an email address, so `/user` is enough.
      //
      // The default `scope=read:user user:email` is left alone deliberately: a
      // GitHub App's user-to-server token takes its permissions from the App's
      // own configuration and GitHub ignores `scope` outright, so overriding it
      // changes the query string and nothing else.
      userinfo: "https://api.github.com/user",
      profile: (profile) => ({
        id: String(profile.id),
        name: profile.name ?? profile.login,
        login: profile.login,
        image: profile.avatar_url,
      }),
    }),
  ],
  session: { strategy: "jwt" },
  callbacks: {
    /**
     * The token is the only place this information lives, so what is not put here
     * cannot be recovered later without sending the user back to GitHub.
     */
    jwt({ token, account, profile }) {
      if (account?.access_token) {
        token.accessToken = account.access_token;
      }
      if (profile?.login) {
        token.login = profile.login as string;
      }
      return token;
    },
    /**
     * `accessToken` is deliberately **not** copied onto the session. `auth()` in a
     * server component and `useSession()` in the browser return the same object,
     * so anything here is one careless import away from being shipped to the
     * client. Server code that needs the token reads the JWT instead.
     */
    session({ session, token }) {
      if (session.user) {
        session.user.login = token.login;
      }
      return session;
    },
  },
});
