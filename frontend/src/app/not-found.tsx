import {
  Body,
  InlineLink,
  Page,
  SectionLabel,
  TwoToneHeading,
} from "@/components/primitives";

export const metadata = { title: "not found" };

/**
 * Next's default 404 is a white page in a system font. A mistyped URL should still
 * look like the rest of the site and say where to go instead.
 */
export default function NotFound() {
  return (
    <Page>
      <SectionLabel>404</SectionLabel>
      <TwoToneHeading
        className="mt-4"
        lead="No such page,"
        trail="which is all we can tell you."
      />
      <Body className="mt-8">
        This URL does not match anything here. Nothing was looked up and nothing
        was refused — a missing page and a repository you cannot see are
        deliberately indistinguishable in this app, so a 404 is never evidence
        either way.
      </Body>
      <Body className="mt-8">
        The <InlineLink href="/">report</InlineLink> needs an account. The{" "}
        <InlineLink href="/public/flaky">public board</InlineLink> does not.
      </Body>
    </Page>
  );
}
