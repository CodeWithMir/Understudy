import type { Metadata } from "next";
import "./globals.css";
import { THEME_BOOT_SCRIPT } from "./theme-toggle";

export const metadata: Metadata = {
  title: "Understudy",
  description: "Turn a screen recording, a target URL and plain-English rules into tested, governed automation.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" data-theme="dark" className="h-full bg-slate-950 text-slate-100" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_BOOT_SCRIPT }} />
      </head>
      <body className="min-h-full flex flex-col antialiased">
        <a href="#main" className="skip-link">Skip to main content</a>
        {children}
      </body>
    </html>
  );
}
