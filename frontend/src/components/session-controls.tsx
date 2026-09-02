import { auth, signIn, signOut } from "@/auth";
import { Button } from "@/components/primitives";

/**
 * Sign in / sign out, as forms wrapped around server actions rather than a client
 * component. There is no `SessionProvider` and no `useSession` anywhere in this
 * app: every page is already a server component that can read `auth()` directly,
 * so shipping the session to the browser would add a bundle and a hydration pass
 * to display a username the server already had.
 *
 * Copy is lowercase and factual per DESIGN.md, and the login is mono because it is
 * an identifier.
 */
export async function SessionControls() {
  const session = await auth();

  if (!session?.user) {
    return (
      <form
        action={async () => {
          "use server";
          await signIn("github", { redirectTo: "/" });
        }}
      >
        <Button type="submit" variant="secondary" size="compact">
          sign in with github
        </Button>
      </form>
    );
  }

  return (
    <div className="flex items-center gap-4">
      <span className="font-mono text-[12px] text-text-faint">
        {session.user.login ?? session.user.name}
      </span>
      <form
        action={async () => {
          "use server";
          await signOut({ redirectTo: "/" });
        }}
      >
        <Button type="submit" variant="secondary" size="compact">
          sign out
        </Button>
      </form>
    </div>
  );
}
