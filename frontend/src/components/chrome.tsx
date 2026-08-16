// Console chrome: light-default theme with a manual dark toggle (stamps <html data-mode="dark">,
// NOT prefers-color-scheme — locked decision) + the "show/hide stubbed" toggle (body.hide-stubs).
// State lives here so every screen's topbar buttons share it.
import { createContext, useContext, useEffect, useState, type ReactNode } from "react";

type Chrome = {
  dark: boolean;
  toggleTheme: () => void;
  stubsHidden: boolean;
  toggleStubs: () => void;
};

const Ctx = createContext<Chrome | null>(null);

export function ChromeProvider({ children }: { children: ReactNode }) {
  const [dark, setDark] = useState(false);
  const [stubsHidden, setStubsHidden] = useState(false);

  useEffect(() => {
    const root = document.documentElement;
    if (dark) root.setAttribute("data-mode", "dark");
    else root.removeAttribute("data-mode");
  }, [dark]);

  useEffect(() => {
    document.body.classList.toggle("hide-stubs", stubsHidden);
  }, [stubsHidden]);

  return (
    <Ctx.Provider
      value={{
        dark,
        toggleTheme: () => setDark((v) => !v),
        stubsHidden,
        toggleStubs: () => setStubsHidden((v) => !v),
      }}
    >
      {children}
    </Ctx.Provider>
  );
}

export function useChrome(): Chrome {
  const c = useContext(Ctx);
  if (!c) throw new Error("useChrome must be used within ChromeProvider");
  return c;
}

// The two topbar buttons every screen shares (right side of the topbar).
export function ChromeToggles() {
  const { stubsHidden, toggleStubs, toggleTheme } = useChrome();
  return (
    <>
      <button className="kbtn" onClick={toggleStubs}>
        {stubsHidden ? "show stubbed" : "hide stubbed"}
      </button>
      <button className="kbtn" onClick={toggleTheme}>
        ◐ theme
      </button>
    </>
  );
}
