// Modified for LEVI (2026); see NOTICE and docs/UPSTREAM.md.
import type { Metadata } from "next";
import "./globals.css";
import "@/styles/tokens.css";
import "@/styles/ds.css";
import "@/styles/shell.css";
import { AuthProvider } from "@/context/auth-context";
import { LocaleProvider } from "@/components/levi-locale";
import AgentWorkbench from "@/components/agent-workbench";
import LeviHeader from "@/components/levi-header";
import { AppFrame } from "@/components/shell/app-frame";
import { SkipToContent } from "@/components/shell/skip-to-content";
import { RouteTitle } from "@/components/shell/route-title";
import { THEME_BOOT_SCRIPT } from "@/components/shell/theme-boot";
// The name is the same in both languages; the tab title gets the page's name
// in the reader's language from <RouteTitle /> (the language is chosen in the
// browser, so the server cannot know it).
export const metadata: Metadata = {
  title: "LEVI",
  description:
    "LEVI, a bilingual workbench for robot demonstration data: explore, convert, label and review LeRobot datasets. 中英双语的机器人示范数据工作台：浏览、转换、标注与审核。",
};
export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    // data-theme is set before hydration by the boot script (a stored
    // light/dark choice), so React must not complain that it differs.
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_BOOT_SCRIPT }} />
      </head>
      <body>
        <LocaleProvider>
          <AuthProvider>
            <AppFrame>
              <SkipToContent />
              <RouteTitle />
              <LeviHeader />
              {children}
              <AgentWorkbench />
            </AppFrame>
          </AuthProvider>
        </LocaleProvider>
      </body>
    </html>
  );
}
