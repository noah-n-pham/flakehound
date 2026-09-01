import type { Metadata } from "next";
import { Instrument_Serif, Inter, JetBrains_Mono } from "next/font/google";

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
  title: "flakehound",
  description: "flaky ci jobs, found from history you already have",
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
      <body>
        <Nav />
        {children}
      </body>
    </html>
  );
}
