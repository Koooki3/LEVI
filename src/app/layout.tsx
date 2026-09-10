// Modified for LEVI (2026); see NOTICE and docs/UPSTREAM.md.
import type { Metadata } from "next";
import "./globals.css";
import "./levi.css";
import { AuthProvider } from "@/context/auth-context";
import { LocaleProvider } from "@/components/levi-locale";
import LeviHeader from "@/components/levi-header";
export const metadata: Metadata = {
  title: "LEVI · 机器人数据工坊",
  description:
    "LeRobot Exploration, Validation & Integration — bilingual robotics dataset workbench",
};
export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="zh-CN">
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
