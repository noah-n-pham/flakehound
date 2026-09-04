import type { Metadata } from "next";
import { Instrument_Serif, Inter, JetBrains_Mono } from "next/font/google";

import { Footer } from "@/components/footer";
import { Nav } from "@/components/nav";
import "./globals.css";

const instrumentSerif = Instrument_Serif({
  subsets: ["latin"],
  weight: "400",
  variable: "--font-instrument-serif",
});

const inter = Inter({
  subsets: ["latin"],
  variable: "--font-inter",
});

const jetbrainsMono = JetBrains_Mono({
  subsets: ["latin"],
  variable: "--font-jetbrains-mono",
});

export const metadata: Metadata = {
  // The template means a page declares "public board" and never restates the brand,
  // so no page can drift to a different separator or word order.
  title: {
    default: "flakehound",
    template: "%s · flakehound",
  },
  description: "flaky ci detection, from the github actions history you already have",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  // The font variables go on <html>, not <body>: Tailwind declares --font-display
  // and friends on :root, and a custom property referencing a variable that is
  // undefined on that same element is invalid at computed-value time, which
  // silently collapses the whole token to nothing.
  return (
    <html
      lang="en"
      className={`${instrumentSerif.variable} ${inter.variable} ${jetbrainsMono.variable}`}
    >
      {/* The shell is a column: nav, then the page, then the footer pinned below it
          rather than to the viewport. Every page here is taller than the screen. */}
      <body className="flex min-h-screen flex-col">
        <Nav />
        <div className="flex-1">{children}</div>
        <Footer />
      </body>
    </html>
  );
}
