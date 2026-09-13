// Modified for LEVI (2026); see NOTICE and docs/UPSTREAM.md.
import type { Metadata } from "next";
import "./globals.css";
import "./levi.css";
import { AuthProvider } from "@/context/auth-context";
import { LocaleProvider } from "@/components/levi-locale";
import LeviHeader from "@/components/levi-header";
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
    <html lang="en">
      <body>
        <LocaleProvider>
          <AuthProvider>
            <LeviHeader />
            {children}
          </AuthProvider>
        </LocaleProvider>
      </body>
    </html>
  );
}
