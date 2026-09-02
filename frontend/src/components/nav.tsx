import Link from "next/link";

import { SessionControls } from "@/components/session-controls";

/** 56px tall, lowercase brand left, lowercase links right, 1px bottom border. */
export function Nav() {
  return (
    <nav className="h-14 border-b border-border">
      <div className="mx-auto flex h-full max-w-[960px] items-center justify-between px-6">
        <Link href="/" className="text-[15px] text-text">
          flakehound
        </Link>
        <div className="flex items-center gap-6 text-[13px] text-text-muted">
          <Link href="/">report</Link>
          <Link href="/styleguide">styleguide</Link>
          <SessionControls />
        </div>
      </div>
    </nav>
  );
}
