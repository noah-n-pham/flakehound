"use client";

import {
  Body,
  Button,
  MetaStrip,
  Page,
  SectionLabel,
  TwoToneHeading,
} from "@/components/primitives";

/**
 * The only client component in the app, because Next requires an error boundary to
 * be one.
 *
 * It exists because a real failure mode has already happened twice in production: a
 * page here reads the API on every request, so a backend hiccup surfaces as a 500 on
 * the frontend, and both times it recovered by itself. Next's default screen for
 * that is a stack trace in development and a blank error in production; a retry
 * button is the correct affordance for something transient.
 *
 * `error.digest` is a hash Next assigns on the server, not a message. Nothing about
 * the failure itself reaches the browser, which is deliberate. The messages our
 * reads throw name repo ids and endpoints.
 */
export default function ErrorBoundary({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <Page>
      <SectionLabel>error</SectionLabel>
      <TwoToneHeading
        className="mt-4"
        lead="That read failed,"
        trail="and it is usually worth trying again."
      />
      <Body className="mt-8">
        This page asks the API for live rows every time it is opened, so a
        restarting container or a slow query shows up here rather than as stale
        numbers. Nothing is cached and nothing was written.
      </Body>
      <div className="mt-8">
        <Button onClick={reset}>try again</Button>
      </div>
      {error.digest ? (
        <div className="mt-24">
          <MetaStrip items={["error digest", error.digest]} />
        </div>
      ) : null}
    </Page>
  );
}
