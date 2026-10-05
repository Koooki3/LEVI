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
import { THEME_BOOT_SCRIPT } from "@/components/shell/theme-boot";
export const metadata: Metadata = {
  title: "LEVI · Robot Data Atelier",
  description:
    "A bilingual LeRobot workbench for dataset exploration, validation, annotation and conversion.",
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
