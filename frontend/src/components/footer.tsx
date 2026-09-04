import { MetaStrip, Rule } from "@/components/primitives";

/**
 * The bottom of the shell: a hairline, then the same dot-separated mono strip that
 * sits under a statistic elsewhere, here describing how the whole system works
 * rather than one number.
 *
 * Every claim on it is true and checkable, which is the point: this is the place a
 * reader looks to find out whether the numbers above came from somewhere real.
 */
export function Footer() {
  return (
    <footer className="mx-auto max-w-[960px] px-6 pb-24">
      <Rule />
      <div className="mt-8">
        <MetaStrip
          items={[
            "github actions webhooks",
            "deduplicated on delivery id",
            "postgres is the queue",
            "one container on one ec2 instance",
            "no workflow file edited, nothing uploaded from a test job",
          ]}
        />
      </div>
    </footer>
  );
}
