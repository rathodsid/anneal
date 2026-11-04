import type React from "react";
import { ChromeToggles } from "./chrome";

/** The page frame every screen shares: topbar (breadcrumbs · actions · chrome toggles), then the body. */
export function Shell({
  crumbs,
  actions,
  className,
  children,
}: {
  crumbs: React.ReactNode;
  actions?: React.ReactNode;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <div className={className ? `main ${className}` : "main"}>
      <header className="topbar">
        <nav className="crumbs">{crumbs}</nav>
        <div className="top-actions">
          {actions}
          <ChromeToggles />
        </div>
      </header>
      {children}
    </div>
  );
}
