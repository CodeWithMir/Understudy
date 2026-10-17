"use client";

/* 8.3: the Studio bell - what happened while the user was elsewhere: the admin confirmed / rejected an escalation or
 * asked a question, a tool the session waited for became active (or was rejected). Live through the user's WebSocket
 * (event "notification"), with the page's polling fallback; stored on the server, so nothing is lost on reload. */
import React, { useCallback, useEffect, useRef, useState } from "react";
import { Bell, CheckCheck, CheckCircle2, HelpCircle, Info, XCircle } from "lucide-react";
import { studioApi, type StudioNotification } from "../../lib/studio";
import { cx } from "./ui";

const ICON = {
  success: <CheckCircle2 className="h-3.5 w-3.5 text-emerald-400" />, error: <XCircle className="h-3.5 w-3.5 text-rose-400" />,
  warning: <HelpCircle className="h-3.5 w-3.5 text-amber-400" />, info: <Info className="h-3.5 w-3.5 text-sky-400" />,
};

export function NotificationBell({ tick, onOpenSession }: { tick: number; onOpenSession: (sid: string) => void }) {
  const [items, setItems] = useState<StudioNotification[]>([]);
  const [unread, setUnread] = useState(0);
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const load = useCallback(() => {
    studioApi.notifications(30).then((r) => { setItems(r.notifications); setUnread(r.unread); }).catch(() => null);
  }, []);
  useEffect(() => { const t = setTimeout(load, 0); const id = setInterval(load, 30000); return () => { clearTimeout(t); clearInterval(id); }; }, [load, tick]);
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => { if (box.current && !box.current.contains(e.target as Node)) setOpen(false); };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("mousedown", onDoc); document.removeEventListener("keydown", onKey); };
  }, [open]);
  const markRead = (ids?: string[]) => {
    studioApi.readNotifications(ids).then((r) => { setItems(r.notifications.slice(0, 30)); setUnread(r.unread); }).catch(() => null);
  };
  return (
    <div className="relative" ref={box}>
      <button type="button" onClick={() => setOpen(!open)} data-testid="user-bell" aria-expanded={open}
        aria-label={`Notifications: ${unread} unread`} title="Notifications"
        className="relative inline-flex items-center rounded-lg border border-slate-700 bg-slate-800 p-1.5 text-slate-300 hover:text-white">
        <Bell className="h-4 w-4" />
        {unread > 0 && <span className="absolute -right-1.5 -top-1.5 min-w-[18px] rounded-full bg-amber-500 px-1 text-center text-[10px] font-bold leading-[18px] text-slate-950">{unread}</span>}
      </button>
      {open && (
        <div className="absolute right-0 z-50 mt-2 w-[22rem] max-w-[90vw] overflow-hidden rounded-xl border border-slate-700 bg-slate-900 shadow-2xl" role="dialog" aria-label="Notifications">
          <div className="flex items-center gap-2 border-b border-slate-800 px-3 py-2 text-xs">
            <b className="text-slate-100">Notifications</b>
            {unread > 0 && <button type="button" onClick={() => markRead()} className="ml-auto inline-flex items-center gap-1 text-sky-300 hover:text-sky-200">
              <CheckCheck className="h-3.5 w-3.5" />Mark all read</button>}
          </div>
          <ul className="max-h-96 overflow-y-auto" data-testid="notifications">
            {items.length === 0 && <li className="px-3 py-6 text-center text-xs text-slate-500">Nothing yet. Your admin&apos;s answers show up here.</li>}
            {items.map((n) => (
              <li key={n.id}>
                <button type="button" onClick={() => { if (!n.read) markRead([n.id]); if (n.link?.session_id) { onOpenSession(n.link.session_id); setOpen(false); } }}
                  className={cx("flex w-full gap-2 border-b border-slate-800/60 px-3 py-2 text-left text-xs hover:bg-slate-800/60", !n.read && "bg-sky-500/5")}>
                  <span className="mt-0.5 shrink-0">{ICON[n.level] || ICON.info}</span>
                  <span className="min-w-0 flex-1">
                    <span className={cx("block", n.read ? "text-slate-300" : "font-semibold text-white")}>{n.title}</span>
                    {n.body && <span className="mt-0.5 block whitespace-pre-wrap text-[11px] text-slate-400">{n.body}</span>}
                    <span className="mt-0.5 block text-[10px] text-slate-500">{n.at.slice(0, 16).replace("T", " ")} UTC</span>
                  </span>
                  {!n.read && <span className="mt-1 h-2 w-2 shrink-0 rounded-full bg-sky-400" aria-label="unread" />}
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
