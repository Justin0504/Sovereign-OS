// node render.mjs <file.html> <out.png> <width> [dark]
setTimeout(() => { console.error("render timeout"); process.exit(2); }, 45000);
const [,, file, out, width, dark] = process.argv;
const fs = await import("node:fs");
const t = await (await fetch("http://127.0.0.1:9333/json/new?about:blank", { method: "PUT" })).json();
const ws = new WebSocket(t.webSocketDebuggerUrl); let id = 0; const P = new Map();
ws.onmessage = (m) => { const d = JSON.parse(m.data); if (d.id && P.has(d.id)) { P.get(d.id)(d); P.delete(d.id); } };
await new Promise((r) => (ws.onopen = r));
const send = (method, params = {}) => new Promise((r) => { const i = ++id; P.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
await send("Emulation.setDeviceMetricsOverride", { width: +width, height: 1200, deviceScaleFactor: 2, mobile: false });
await send("Page.enable"); await send("Runtime.enable");
await send("Page.navigate", { url: "file://" + file + (dark ? "#dark" : "") });
await new Promise((r) => setTimeout(r, 1500));
const ev = await send("Runtime.evaluate", { expression: `(async()=>{ if(location.hash==='#dark')document.documentElement.classList.add('dark'); if(window.draw)window.draw(); await document.fonts.ready; await new Promise(r=>setTimeout(r,300)); const b=document.getElementById('fig').getBoundingClientRect(); const fonts=[...document.fonts].filter(f=>f.status==='loaded').map(f=>f.family); return JSON.stringify({x:b.x,y:b.y,w:b.width,h:b.height,fonts:[...new Set(fonts)], ow:document.documentElement.scrollWidth}); })()`, awaitPromise: true, returnByValue: true });
if (ev.result.exceptionDetails) { console.error(JSON.stringify(ev.result.exceptionDetails).slice(0, 500)); process.exit(1); }
const b = JSON.parse(ev.result.result.value);
const s = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: true, clip: { x: b.x, y: b.y, width: b.w, height: b.h, scale: 1 } });
fs.writeFileSync(out, Buffer.from(s.result.data, "base64"));
console.log(out.split("/").pop(), Math.round(b.w) + "x" + Math.round(b.h), "fonts:", b.fonts.join(","));
await send("Target.closeTarget", { targetId: t.id }); ws.close(); process.exit(0);
