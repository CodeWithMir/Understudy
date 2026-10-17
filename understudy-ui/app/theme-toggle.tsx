"use client";

/**
 * Dark / Light theme switch (v7.2) - shown in the header of every page (Studio, Mission Control, both login screens).
 * The choice is stored per browser (localStorage "understudy_theme"); dark is the default. The palette mirroring lives
 * in globals.css (:root[data-theme="light"]); an inline script in layout.tsx applies the stored theme before the first
 * paint, so there is no flash.
 */
import React, { useEffect, useState } from "react";
import { Moon, Sun } from "lucide-react";

export type Theme = "dark" | "light";
export const THEME_KEY = "understudy_theme";

function current(): Theme {
  if (typeof document === "undefined") return "dark";
  return document.documentElement.dataset.theme === "light" ? "light" : "dark";
}

export function applyTheme(t: Theme) {
  document.documentElement.dataset.theme = t;
  try { window.localStorage.setItem(THEME_KEY, t); } catch { /* storage unavailable */ }
  window.dispatchEvent(new CustomEvent("understudy:theme", { detail: t }));
}

export function ThemeToggle({ className = "" }: { className?: string }) {
  const [theme, setTheme] = useState<Theme>("dark");
  useEffect(() => {
    const sync = () => setTheme(current());
    const t = setTimeout(sync, 0);
    window.addEventListener("understudy:theme", sync);
    return () => { clearTimeout(t); window.removeEventListener("understudy:theme", sync); };
  }, []);
  const next: Theme = theme === "dark" ? "light" : "dark";
  return (
    <button type="button" data-testid="theme-toggle" onClick={() => { applyTheme(next); setTheme(next); }}
      aria-label={`Switch to ${next} theme`} title={`Switch to ${next} theme`} aria-pressed={theme === "light"}
      className={"inline-flex items-center gap-1.5 rounded-lg border border-slate-700 bg-slate-800 px-2.5 py-1.5 text-xs font-medium text-slate-200 hover:bg-slate-700 " + className}>
      {theme === "dark" ? <Sun className="h-3.5 w-3.5 text-amber-300" aria-hidden /> : <Moon className="h-3.5 w-3.5 text-sky-300" aria-hidden />}
      {theme === "dark" ? "Light" : "Dark"}
    </button>
  );
}

/** Runs before React: applies the stored theme so the first paint already has the right colours. */
export const THEME_BOOT_SCRIPT =
  `try{var t=localStorage.getItem("${THEME_KEY}");document.documentElement.dataset.theme=t==="light"?"light":"dark"}catch(e){document.documentElement.dataset.theme="dark"}`;
