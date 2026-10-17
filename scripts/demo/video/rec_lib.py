"""Screen-recording helpers: CDP screencast of whichever page is 'on stage', a drawn cursor, smooth moves/typing,
scene markers. Frames + markers go to OUT/frames/*.jpg and OUT/timeline.json."""
import asyncio
import base64
import json
import os
import time

ZOOM = 1.6
ZOOM_JS = "(()=>{const z=()=>{document.documentElement.style.zoom='%s'}; if(document.documentElement) z(); document.addEventListener('DOMContentLoaded',z);})();" % ZOOM

CURSOR_JS = r"""
(() => {
  if (window.__usCursor) return;
  window.__usCursor = true;
  const add = () => {
    if (!document.body) { requestAnimationFrame(add); return; }
    const c = document.createElement('div');
    c.id = '__us_cursor';
    c.style.cssText = 'position:fixed;left:0;top:0;width:22px;height:22px;z-index:2147483647;pointer-events:none;' +
      'transform:translate(-3px,-2px);transition:transform 60ms linear;';
    c.innerHTML = '<svg width="22" height="22" viewBox="0 0 24 24"><path d="M3 2 L3 19 L8 14.5 L11.5 22 L14.2 20.8 L10.8 13.5 L17.5 13.5 Z" fill="white" stroke="black" stroke-width="1.4" stroke-linejoin="round"/></svg>';
    document.body.appendChild(c);
    const ring = document.createElement('div');
    ring.style.cssText = 'position:fixed;width:34px;height:34px;border-radius:50%;border:3px solid rgba(56,189,248,.95);' +
      'z-index:2147483646;pointer-events:none;opacity:0;transform:translate(-50%,-50%) scale(.4);transition:opacity .35s, transform .35s;';
    document.body.appendChild(ring);
    const zf = () => parseFloat(document.documentElement.style.zoom || '1') || 1;
    window.__usMove = (x, y) => { c.style.left = (x / zf()) + 'px'; c.style.top = (y / zf()) + 'px'; };
    document.addEventListener('mousemove', e => window.__usMove(e.clientX, e.clientY), true);
    document.addEventListener('mousedown', e => {
      ring.style.left = (e.clientX / zf()) + 'px'; ring.style.top = (e.clientY / zf()) + 'px';
      ring.style.transition = 'none'; ring.style.opacity = '1'; ring.style.transform = 'translate(-50%,-50%) scale(.4)';
      requestAnimationFrame(() => { ring.style.transition = 'opacity .45s, transform .45s';
        ring.style.opacity = '0'; ring.style.transform = 'translate(-50%,-50%) scale(1.6)'; });
    }, true);
    if (window.__usLast) window.__usMove(window.__usLast[0], window.__usLast[1]);
  };
  add();
})();
"""


class Recorder:
    def __init__(self, out_dir, scale=2):
        self.out = out_dir
        os.makedirs(os.path.join(out_dir, "frames"), exist_ok=True)
        self.frames = []          # (t, file)
        self.markers = []         # (t, name)
        self.n = 0
        self.cdp = None
        self.page = None
        self.mouse = {}           # page -> (x, y)
        self.scale = scale
        self.t0 = time.time()

    async def _on_frame(self, params):
        self.n += 1
        fn = f"f{self.n:06d}.jpg"
        with open(os.path.join(self.out, "frames", fn), "wb") as fh:
            fh.write(base64.b64decode(params["data"]))
        self.frames.append((time.time() - self.t0, fn))
        try:
            await self.cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
        except Exception:
            pass

    async def stage(self, page):
        """Put `page` on stage: the screencast now captures it."""
        if self.cdp is not None:
            try:
                await self.cdp.send("Page.stopScreencast")
                await self.cdp.detach()
            except Exception:
                pass
        await page.bring_to_front()
        self.page = page
        self.cdp = await page.context.new_cdp_session(page)
        self.cdp.on("Page.screencastFrame", lambda p: asyncio.ensure_future(self._on_frame(p)))
        vp = page.viewport_size
        await self.cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 90, "maxWidth": vp["width"],
                                                     "maxHeight": vp["height"], "everyNthFrame": 1})
        # nudge a repaint so a first frame arrives
        x, y = self.mouse.get(id(page), (vp["width"] * 0.6, vp["height"] * 0.55))
        await page.mouse.move(x, y)

    def mark(self, name):
        self.markers.append((time.time() - self.t0, name))
        print(f"[rec] {time.time() - self.t0:7.2f}s  scene {name}", flush=True)

    async def stop(self):
        if self.cdp is not None:
            try:
                await self.cdp.send("Page.stopScreencast")
            except Exception:
                pass
        with open(os.path.join(self.out, "timeline.json"), "w") as fh:
            json.dump({"frames": self.frames, "markers": self.markers, "end": time.time() - self.t0}, fh)

    # ---- human-like actions -------------------------------------------------------------------------------
    async def move_to(self, loc, dx=0.5, dy=0.5, steps=22):
        page = self.page
        await loc.scroll_into_view_if_needed()
        await page.wait_for_timeout(120)
        box = await loc.bounding_box()
        if not box:
            return None
        tx, ty = box["x"] + box["width"] * dx, box["y"] + box["height"] * dy
        sx, sy = self.mouse.get(id(page), (tx - 200, ty + 120))
        for i in range(1, steps + 1):
            k = i / steps
            e = k * k * (3 - 2 * k)                      # ease in-out
            await page.mouse.move(sx + (tx - sx) * e, sy + (ty - sy) * e)
            await page.wait_for_timeout(12)
        self.mouse[id(page)] = (tx, ty)
        return tx, ty

    async def click(self, loc, pause=260, after=400, dx=0.5, dy=0.5):
        pos = await self.move_to(loc, dx, dy)
        await self.page.wait_for_timeout(pause)
        await loc.click()                                # Playwright re-targets if the layout moved meanwhile
        await self.page.wait_for_timeout(after)

    async def type(self, loc, text, delay=28, clear=True):
        await self.click(loc, after=150)
        if clear:
            await loc.fill("")
        await loc.press_sequentially(text, delay=delay)

    async def hover(self, loc, hold=600):
        await self.move_to(loc)
        await self.page.wait_for_timeout(hold)

    async def wheel(self, total, step=90, delay=28, x=None, y=None):
        """Smooth scroll by `total` px over the element under (x, y) (default: the main content area)."""
        page = self.page
        vp = page.viewport_size
        px = x if x is not None else vp["width"] * 0.55
        py = y if y is not None else vp["height"] * 0.6
        await page.mouse.move(px, py)
        self.mouse[id(page)] = (px, py)
        total, step = total * ZOOM, step * ZOOM          # amounts are given in layout (CSS) px
        n = max(1, int(abs(total) / step))
        for _ in range(n):
            await page.mouse.wheel(0, step if total > 0 else -step)
            await page.wait_for_timeout(delay)

    async def wait_text(self, text, timeout=180, root="body", gone=None):
        end = time.time() + timeout
        while time.time() < end:
            t = await self.page.inner_text(root)
            if text in t and (gone is None or gone not in t):
                return True
            await self.page.wait_for_timeout(400)
        raise TimeoutError(f"'{text}' did not appear in {timeout}s")
