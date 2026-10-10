import asyncio, hashlib, json, os, re, secrets, subprocess, sys, time
from datetime import datetime
from pathlib import Path

import httpx, psutil, yaml
from dateutil import parser as dateparser
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
SKILLS, MEMORY_DIR, DATA = ROOT / "skills", ROOT / "memory", ROOT / "data"
for d in (SKILLS, MEMORY_DIR, DATA):
    d.mkdir(exist_ok=True)
MEMORY_FILE, TASKS_FILE, STATE_FILE = MEMORY_DIR / "long_term.md", DATA / "tasks.json", DATA / "state.json"
OLLAMA = os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")
psutil.cpu_percent(None)
app = FastAPI(title="AgentOS")
sse = lambda d: f"data: {json.dumps(d)}\n\n"

# ---------------- state ----------------
def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"mode": "school", "last_sync": None, "last_sync_error": None}

def save_state(s):
    STATE_FILE.write_text(json.dumps(s, indent=2))

def read_tasks():
    try:
        return json.loads(TASKS_FILE.read_text())
    except Exception:
        return []

# ---------------- ollama ----------------
async def ollama_models():
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            r = await c.get(f"{OLLAMA}/api/tags")
            r.raise_for_status()
            return [m["name"] for m in r.json().get("models", [])]
    except Exception:
        return None

async def active_model():
    names = await ollama_models()
    sel = load_state().get("model")
    pref = os.getenv("OLLAMA_MODEL", "").strip()
    if names:
        for c in (sel, pref):
            if c and c in names:
                return c, names
        return names[0], names
    return (sel or pref or None), names

# ---------------- status / mode ----------------
def hw_extra():
    out = {"cpu_count": psutil.cpu_count()}
    try:
        p = "/System/Volumes/Data" if os.path.exists("/System/Volumes/Data") else "/"
        du = psutil.disk_usage(p)
        out["disk"] = {"percent": du.percent, "used_gb": round(du.used / 1e9, 1), "total_gb": round(du.total / 1e9, 1)}
    except Exception:
        out["disk"] = None
    try:
        b = psutil.sensors_battery()
        out["battery"] = {"percent": round(b.percent), "plugged": bool(b.power_plugged)} if b else None
    except Exception:
        out["battery"] = None
    try:
        out["load"] = [round(x, 2) for x in os.getloadavg()]
    except Exception:
        out["load"] = None
    out["uptime_s"] = int(time.time() - psutil.boot_time())
    return out

@app.get("/api/status")
async def status():
    model, names = await active_model()
    vm = psutil.virtual_memory()
    st = load_state()
    return {
        "health": "ok",
        "ollama": {"online": names is not None, "model": model, "available": names or []},
        "cpu_percent": psutil.cpu_percent(None),
        **hw_extra(),
        "ram": {"percent": vm.percent, "used_gb": round(vm.used / 1e9, 2), "total_gb": round(vm.total / 1e9, 2)},
        "mode": st.get("mode", "school"),
        "last_sync": st.get("last_sync"),
        "task_count": len(read_tasks()),
    }

class ModeBody(BaseModel):
    mode: str

@app.post("/api/mode")
def set_mode(b: ModeBody):
    if b.mode not in ("school", "dev"):
        raise HTTPException(400, "mode must be 'school' or 'dev'")
    st = load_state(); st["mode"] = b.mode; save_state(st)
    return {"mode": b.mode}

# ---------------- model selection + dashboard layout ----------------
class ModelBody(BaseModel):
    model: str

@app.post("/api/model")
async def set_model(b: ModelBody):
    names = await ollama_models()
    if names is None:
        raise HTTPException(503, "Ollama is not reachable")
    if b.model not in names:
        raise HTTPException(404, "Model not installed: " + b.model)
    st = load_state(); st["model"] = b.model; save_state(st)
    return {"model": b.model}

DASH_FILE = DATA / "dashboard.json"

class DashBody(BaseModel):
    widgets: list[dict]

@app.get("/api/dashboard")
def get_dashboard():
    try:
        return json.loads(DASH_FILE.read_text())
    except Exception:
        return {"widgets": None}

@app.post("/api/dashboard")
def save_dashboard(b: DashBody):
    DASH_FILE.write_text(json.dumps({"widgets": b.widgets}, indent=2))
    return {"saved": True}

# ---------------- Veracross scraper ----------------
HEAD_MAP = [
    ("due", ("due",)),
    ("course", ("course", "class", "subject")),
    ("description", ("description", "detail", "note", "instruction")),
    ("status", ("status", "complete", "submitted")),
    ("title", ("assignment", "title", "name", "task")),
]
DATE_RE = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b|\b\d{1,2}[/.-]\d{1,2}([/.-]\d{2,4})?\b", re.I)

JS_EXTRACT = r"""
() => {
  const out = [];
  document.querySelectorAll('table').forEach(t => {
    const hr = [...t.querySelectorAll('tr')].find(r => r.querySelector('th'));
    const heads = hr ? [...hr.children].map(c => c.innerText.trim().toLowerCase()) : [];
    t.querySelectorAll('tr').forEach(tr => {
      if (!tr.querySelector('td')) return;
      const cells = [...tr.children].map(c => c.innerText.trim());
      out.push({kind: 'row', heads, cells, link: (tr.querySelector('a') || {}).href || ''});
    });
  });
  if (!out.length) {
    const sel = '[class*="assignment" i], [data-assignment-id], [class*="homework" i]';
    [...document.querySelectorAll(sel)].filter(e => !e.querySelector(sel)).forEach(e => {
      const lines = e.innerText.split('\n').map(s => s.trim()).filter(Boolean);
      if (lines.length) out.push({kind: 'card', lines, link: (e.querySelector('a') || {}).href || ''});
    });
  }
  return out;
}
"""

def parse_due(s):
    if not s:
        return None
    try:
        return dateparser.parse(re.sub(r"(?i)\bdue\b:?", "", s), fuzzy=True,
                                default=datetime(datetime.now().year, 1, 1)).isoformat()
    except Exception:
        return None

def normalize(raw):
    seen, tasks = set(), []
    for r in raw:
        t = {"title": "", "course": "", "due": "", "description": "", "status": "", "url": r.get("link", "")}
        if r["kind"] == "row":
            heads, cells = r["heads"], r["cells"]
            for i, c in enumerate(cells):
                h = heads[i] if i < len(heads) else ""
                if not h or not c:
                    continue
                for field, keys in HEAD_MAP:
                    if any(k in h for k in keys) and not t[field]:
                        t[field] = c
                        break
            if not t["due"]:
                t["due"] = next((c for c in cells if c and DATE_RE.search(c)), "")
            if not t["title"]:
                t["title"] = next((c for c in cells if c and c != t["due"] and c != t["course"]), "")
        else:
            lines, desc = r["lines"], []
            t["title"] = lines[0]
            for l in lines[1:]:
                if not t["due"] and (DATE_RE.search(l) or "due" in l.lower()):
                    t["due"] = l
                elif not t["course"]:
                    t["course"] = l
                else:
                    desc.append(l)
            t["description"] = " ".join(desc)
        if not t["title"]:
            continue
        t["id"] = hashlib.sha1(f'{t["title"]}|{t["course"]}|{t["due"]}'.encode()).hexdigest()[:12]
        t["due_iso"] = parse_due(t["due"])
        if t["id"] in seen:
            continue
        seen.add(t["id"]); tasks.append(t)
    return tasks

async def scrape_veracross():
    from playwright.async_api import async_playwright
    home = os.getenv("VERACROSS_LOGIN_URL", "").strip() or os.getenv("VERACROSS_ASSIGNMENTS_URL", "").strip()
    user, pw = os.getenv("VERACROSS_USERNAME", ""), os.getenv("VERACROSS_PASSWORD", "")
    if not home or "YOURSCHOOL" in home:
        raise RuntimeError("Set VERACROSS_LOGIN_URL in AgentOS/.env and restart.")
    force_visible = os.getenv("VERACROSS_HEADLESS", "").lower() == "false"
    submit = 'button[type="submit"], input[type="submit"], #login-button, #next-button'
    link_sel = 'a[href*="/classes/"][href*="/assignments"]'
    profile = str(DATA / "browser_profile")
    debug = []

    async def open_ctx(p, headless):
        ctx = await p.chromium.launch_persistent_context(profile, headless=headless)
        async def blk(route):
            if route.request.resource_type in ("image", "font", "media"):
                await route.abort()
            else:
                await route.continue_()
        await ctx.route("**/*", blk)
        return ctx

    async def reach_home(ctx, seconds):
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(home, wait_until="domcontentloaded")
        tried_form = False
        end = time.time() + seconds
        while time.time() < end:
            try:
                if await page.locator(link_sel).count() > 0:
                    return page
                url = page.url
                if "microsoftonline.com" in url or "login.live.com" in url:
                    if await page.get_by_text("Stay signed in", exact=False).count() > 0:
                        cb = page.locator("#KmsiCheckboxField")
                        if await cb.count() > 0:
                            try:
                                await cb.check()
                            except Exception:
                                pass
                        await page.locator("#idSIButton9").click()
                elif url.startswith("https://portals.veracross.eu") and home not in url:
                    await page.goto(home, wait_until="domcontentloaded")
                elif (not tried_form) and user and pw and "veracross" in url and await page.locator('input[type="password"], input[name="username"], input#username').count() > 0:
                    tried_form = True
                    uf = page.locator('input[name="username"], input#username, input[type="email"], input[type="text"]').first
                    pwf = page.locator('input[type="password"]').first
                    if await uf.is_visible():
                        await uf.fill(user)
                    if not await pwf.is_visible():
                        await page.locator(submit).first.click()
                        await pwf.wait_for(state="visible", timeout=10000)
                    await pwf.fill(pw)
                    await page.locator(submit).first.click()
            except Exception:
                pass
            await page.wait_for_timeout(300)
        return None

    async with async_playwright() as p:
        ctx, page = None, None
        if not force_visible:
            ctx = await open_ctx(p, True)
            page = await reach_home(ctx, 12)
            if not page:
                await ctx.close()
                ctx = None
        if not page:
            ctx = await open_ctx(p, False)
            page = await reach_home(ctx, 300)
            if not page:
                try:
                    (DATA / "last_scrape.html").write_text(await ctx.pages[0].content())
                except Exception:
                    pass
                await ctx.close()
                raise RuntimeError("Never reached the portal home page. Sign in in the opened window within 5 minutes, then press Sync again.")
        tasks, seen = [], set()
        try:
            classes = await page.evaluate(JS_LINKS)
            debug.append("classes found: " + json.dumps(classes))
            sem = asyncio.Semaphore(5)

            async def one(idx, c):
                async with sem:
                    pg = await ctx.new_page()
                    reqs = []
                    pg.on("response", lambda r: reqs.append(r.request.resource_type + " " + r.url[:160]) if r.request.resource_type in ("xhr", "fetch") else None)
                    rows, info, cp = [], "", ""
                    try:
                        await pg.goto(c["url"], wait_until="domcontentloaded")
                        end = time.time() + 15
                        while time.time() < end and not rows:
                            for fr in pg.frames:
                                try:
                                    if await fr.evaluate(WAIT_JS):
                                        rows = await fr.evaluate(JS_ROWS)
                                        if rows:
                                            break
                                except Exception:
                                    pass
                            if not rows:
                                await pg.wait_for_timeout(250)
                        try:
                            cp = await pg.evaluate(JS_COURSE)
                        except Exception:
                            pass
                        info = "## " + str(c["course"]) + " | " + c["url"] + " | rows=" + str(len(rows)) + " | frames=" + json.dumps([f.url for f in pg.frames]) + "\nxhr: " + json.dumps(reqs[:15])
                        if not rows:
                            if not (DATA / "last_scrape.html").exists() or idx == 0:
                                (DATA / "last_scrape.html").write_text(await pg.content())
                            info += "\n" + (await pg.evaluate("document.body.innerText"))[:1200]
                    except Exception as e:
                        info = "## " + str(c["course"]) + " | ERROR " + str(e)
                    finally:
                        await pg.close()
                    return dict(c, course=cp or c["course"]), rows, info

            results = await asyncio.gather(*(one(i, c) for i, c in enumerate(classes)))
            for c, rows, info in results:
                debug.append(info)
                for t in parse_rows(rows, c["course"], c["url"]):
                    if t["id"] not in seen:
                        seen.add(t["id"]); tasks.append(t)
        finally:
            (DATA / "last_scrape.txt").write_text("\n".join(debug))
            await ctx.close()
    return tasks

JS_LINKS = r"""
() => {
  const sel = 'a[href*="/classes/"][href*="/assignments"]';
  const seen = new Map();
  document.querySelectorAll(sel).forEach(a => {
    if (seen.has(a.href)) return;
    let node = a, hops = 0;
    while (node.parentElement && hops < 6 && node.parentElement.querySelectorAll(sel).length === 1) { node = node.parentElement; hops++; }
    const lines = (node.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
    const course = lines.find(l => !/view all assignments/i.test(l)) || '';
    seen.set(a.href, course);
  });
  return [...seen.entries()].map(([url, course]) => ({url, course}));
}
"""

JS_ROWS = r"""
() => {
  const toks = [];
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = w.nextNode())) {
    const p = n.parentElement;
    if (!p || /^(SCRIPT|STYLE|NOSCRIPT)$/.test(p.tagName)) continue;
    const s = n.textContent.replace(/\s+/g, ' ').trim();
    if (s) toks.push({s: s, link: (p.closest('a') || {}).href || ''});
  }
  const dateRe = /^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? \d{1,2}(\s+(Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*)?$/i;
  const hdrRe = /^[A-Za-z]+ \d{1,2}\s*[-\u2013]|\(\d+\s+assignments?\)/i;
  const statusRe = /^(pending|complete|completed|submitted|graded|missing|late|overdue|excused|incomplete|not submitted|turned in|ungraded)$/i;
  const rows = [];
  for (let i = 0; i < toks.length; i++) {
    if (!dateRe.test(toks[i].s)) continue;
    if (i + 1 < toks.length && /^[-\u2013]/.test(toks[i + 1].s)) continue;
    const seg = [toks[i].s];
    let link = '';
    for (let j = i + 1; j < toks.length && seg.length < 8; j++) {
      const t = toks[j].s;
      if (dateRe.test(t) || hdrRe.test(t)) break;
      seg.push(t);
      if (!link) link = toks[j].link;
      if (statusRe.test(t)) break;
    }
    if (seg.length >= 3) rows.push({tokens: seg, link: link});
  }
  return rows;
}
"""

WAIT_JS = r"""() => /(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? \d{1,2}/.test(document.body.innerText)"""

JS_COURSE = r"""
() => {
  const lines = (document.body.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
  for (const l of lines) {
    const m = l.match(/^[A-Z][A-Za-z0-9]*-[A-Za-z0-9.\-]+\s*:\s*(.+)$/);
    if (m) return m[1].trim();
  }
  const h = document.querySelector('h1, h2, .class-name, [class*="course" i]');
  return h ? h.innerText.trim().split('\n')[0] : '';
}
"""

WEEKDAY = re.compile(r"(?i)^(mon|tue|wed|thu|fri|sat|sun)[a-z]*$")
STATUS = re.compile(r"(?i)^(pending|complete|completed|submitted|graded|missing|late|overdue|excused|incomplete|not submitted|not turned in|turned in|ungraded|[\d.]+(\s*/\s*[\d.]+)?%?)$")
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

def resolve_due(due):
    m = re.match(r"(?i)\s*([a-z]{3})[a-z]*\.?\s+(\d{1,2})(?:\s+([a-z]{3}))?", due or "")
    if not m or m.group(1).lower() not in MONTHS:
        return None
    mon, day, wd = MONTHS[m.group(1).lower()], int(m.group(2)), m.group(3)
    today, best = datetime.now(), None
    for y in (today.year - 1, today.year, today.year + 1):
        try:
            d = datetime(y, mon, day)
        except ValueError:
            continue
        if wd and d.strftime("%a").lower() != wd.lower():
            continue
        if best is None or abs((d - today).days) < abs((best - today).days):
            best = d
    return best.isoformat() if best else None

def clean_course(c, cid):
    c = (c or "").strip()
    if not c or re.search(r"(?i)new updates?|^\d+$|^-+$|view all", c):
        return ("Class " + cid) if cid else "Class"
    return c

def parse_rows(rows, course, url):
    cid = (re.search(r"classes/(\d+)", url) or [None, ""])[1]
    course = clean_course(course, cid)
    out = []
    for r in rows:
        tk = [x for x in r["tokens"] if x]
        if not tk or any("{{" in x for x in tk):
            continue
        due, i = tk[0], 1
        if i < len(tk) and WEEKDAY.match(tk[i]):
            due += " " + tk[i]; i += 1
        rest = tk[i:]
        status = ""
        idx = next((k for k in range(len(rest) - 1, 0, -1) if STATUS.match(rest[k])), None)
        if idx is not None:
            status = rest[idx]; rest = rest[:idx]
        if len(rest) >= 2:
            atype, title = rest[0], " ".join(rest[1:])
        else:
            atype, title = "", " ".join(rest)
        if not title:
            continue
        t = {"title": title, "course": course, "due": due, "description": atype, "status": status, "url": r.get("link") or url, "class_id": cid}
        t["id"] = hashlib.sha1(f"{title}|{cid}|{due}".encode()).hexdigest()[:12]
        t["due_iso"] = resolve_due(due)
        out.append(t)
    return out

SYNC_LOCK = asyncio.Lock()

@app.get("/api/tasks")
def get_tasks():
    st = load_state()
    tasks, seen = [], set()
    for t in read_tasks():
        if "{{" in (t.get("title", "") + t.get("due", "") + t.get("status", "")):
            continue
        k = (t.get("title"), t.get("due"))
        if k in seen:
            continue
        seen.add(k)
        t = dict(t)
        t["due_iso"] = resolve_due(t.get("due", "")) or t.get("due_iso")
        tasks.append(t)
    return {"tasks": tasks, "last_sync": st.get("last_sync"), "last_sync_error": st.get("last_sync_error")}

@app.post("/api/tasks/sync")
async def sync_tasks():
    if SYNC_LOCK.locked():
        raise HTTPException(409, "A sync is already running")
    async with SYNC_LOCK:
        st = load_state()
        try:
            tasks = await scrape_veracross()
            if not tasks:
                raise RuntimeError("Logged in but no assignments could be parsed. Page text was saved to data/last_scrape.txt.")
            TASKS_FILE.write_text(json.dumps(tasks, indent=2))
            st.update(last_sync=datetime.now().isoformat(), last_sync_error=None); save_state(st)
            return {"count": len(tasks), "tasks": tasks}
        except Exception as e:
            st["last_sync_error"] = str(e); save_state(st)
            raise HTTPException(502, str(e))

# ---------------- skills ----------------
FM = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.S)
slug = lambda s: re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")

def parse_skill(p: Path):
    try:
        m = FM.match(p.read_text(encoding="utf-8"))
        if not m:
            return None
        meta = yaml.safe_load(m.group(1)) or {}
        if not isinstance(meta, dict) or not meta.get("name"):
            return None
        return {"file": p.name, "name": str(meta["name"]), "description": str(meta.get("description", "")), "body": m.group(2).strip()}
    except Exception:
        return None

class SkillBody(BaseModel):
    name: str
    description: str = ""
    body: str = ""

@app.get("/api/skills")
def get_skills():
    return {"skills": [s for s in (parse_skill(p) for p in sorted(SKILLS.glob("*.md"))) if s]}

@app.post("/api/skills")
def save_skill(b: SkillBody):
    s = slug(b.name)
    if not s:
        raise HTTPException(400, "Invalid skill name")
    fm = yaml.safe_dump({"name": b.name.strip(), "description": b.description.strip()}, allow_unicode=True, sort_keys=False)
    (SKILLS / f"{s}.md").write_text(f"---\n{fm}---\n{b.body.strip()}\n", encoding="utf-8")
    return {"saved": f"skills/{s}.md"}

@app.delete("/api/skills/{name}")
def delete_skill(name: str):
    p = SKILLS / f"{slug(name)}.md"
    if not p.exists():
        raise HTTPException(404, "No such skill")
    p.unlink()
    return {"deleted": p.name}

# ---------------- files & memory ----------------
def safe(rel: str) -> Path:
    p = (ROOT / rel.lstrip("/")).resolve()
    if p != ROOT and ROOT not in p.parents:
        raise HTTPException(403, "Path is outside the workspace")
    if p.name == ".env" or ".venv" in p.parts:
        raise HTTPException(403, "Protected path")
    return p

class FileBody(BaseModel):
    path: str
    content: str

@app.get("/api/files/list")
def files_list(path: str = Query("")):
    p = safe(path)
    if not p.is_dir():
        raise HTTPException(404, "Not a directory")
    items = [{"name": c.name, "is_dir": c.is_dir(), "size": c.stat().st_size if c.is_file() else None}
             for c in sorted(p.iterdir()) if c.name not in (".venv", "__pycache__", ".env")]
    return {"path": str(p.relative_to(ROOT)), "items": items}

@app.get("/api/files/read")
def files_read(path: str):
    p = safe(path)
    if not p.is_file():
        raise HTTPException(404, "Not a file")
    return {"path": path, "content": p.read_text(encoding="utf-8", errors="replace")}

@app.post("/api/files/write")
def files_write(b: FileBody):
    p = safe(b.path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(b.content, encoding="utf-8")
    return {"written": b.path}

@app.get("/api/memory")
def get_memory():
    return {"content": MEMORY_FILE.read_text(encoding="utf-8")}

class MemBody(BaseModel):
    content: str

@app.post("/api/memory")
def set_memory(b: MemBody):
    MEMORY_FILE.write_text(b.content, encoding="utf-8")
    return {"saved": True}

# ---------------- terminal + safety interception ----------------
RISK_PATTERNS = [
    (r"\brm\b[^\n;|&]*\s-(?:[a-zA-Z]*[rRf]|-recursive|-force)", "recursive/forced delete (rm)"),
    (r"\bmkfs", "filesystem formatting (mkfs)"),
    (r"\b(sudo|doas)\b|\bsu\s+-", "privilege escalation"),
    (r"\bbrew\s+(uninstall|remove|rm|untap|cleanup|autoremove)\b", "Homebrew removal"),
    (r"\b(pip3?|npm)\s+uninstall\b", "package uninstall"),
    (r"\bdd\b[^\n]*\bof=", "raw disk write (dd)"),
    (r":\s*\(\)\s*\{", "fork bomb pattern"),
    (r"\b(chmod|chown)\b[^\n]*\s-\w*R", "recursive permission/ownership change"),
    (r">>?\s*/(etc|dev|System|Library|usr|bin|sbin)\b", "write to system path"),
    (r">>?\s*(~|\$HOME)/\.(zshrc|bashrc|zprofile|ssh)", "shell/ssh config override"),
    (r"\bdiskutil\s+(erase|partition|reformat|apfs\s+delete)", "disk erase/partition"),
    (r"\b(launchctl|csrutil|scutil|networksetup|systemsetup|nvram|pmset)\b", "system configuration change"),
    (r"\bdefaults\s+(write|delete)\b", "system defaults override"),
    (r"\b(shutdown|reboot|halt|poweroff)\b", "power state change"),
    (r"\bgit\s+(push\b[^\n]*(--force|\s-f\b)|reset\s+--hard|clean\s+-\w*f)", "destructive git operation"),
    (r"\b(curl|wget)\b[^\n]*\|\s*(sudo\s+)?(ba|z)?sh\b", "piping download to shell"),
    (r"\b(killall|pkill)\b", "process kill"),
]
PY_RISK = [(r"\bshutil\.rmtree\b|\bos\.(remove|rmdir|unlink|system)\b|\bsubprocess\b", "Python filesystem/process call")]
COMPILED = [(re.compile(p, re.I), w) for p, w in RISK_PATTERNS]
PY_COMPILED = [(re.compile(p), w) for p, w in PY_RISK]
PENDING = {}

def assess(cmd, lang):
    pats = COMPILED + (PY_COMPILED if lang == "python" else [])
    return sorted({w for rx, w in pats if rx.search(cmd)})

def run_cmd(command, language, timeout=120):
    env = os.environ.copy()
    env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + env.get("PATH", "")
    try:
        if language == "python":
            r = subprocess.run([sys.executable, "-c", command], cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout)
        else:
            exe = "/bin/bash" if Path("/bin/bash").exists() else None
            r = subprocess.run(command, shell=True, executable=exe, cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout)
        return {"status": "executed", "exit_code": r.returncode, "stdout": r.stdout[-20000:], "stderr": r.stderr[-20000:], "timed_out": False}
    except subprocess.TimeoutExpired as e:
        return {"status": "executed", "exit_code": None, "stdout": (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""), "stderr": f"Timed out after {timeout}s", "timed_out": True}

class ExecBody(BaseModel):
    command: str
    language: str = "shell"

class AuthBody(BaseModel):
    token: str

@app.post("/api/terminal/execute")
async def terminal_execute(b: ExecBody):
    lang = "python" if b.language.lower().startswith("py") else "shell"
    reasons = assess(b.command, lang)
    if reasons:
        now = time.time()
        for k in [k for k, v in PENDING.items() if now - v["ts"] > 300]:
            PENDING.pop(k, None)
        token = secrets.token_urlsafe(16)
        PENDING[token] = {"command": b.command, "language": lang, "ts": now}
        return JSONResponse(status_code=403, content={"status": "requires_confirmation", "command": b.command, "language": lang, "reasons": reasons, "token": token})
    return await asyncio.to_thread(run_cmd, b.command, lang)

@app.post("/api/terminal/authorize")
async def terminal_authorize(b: AuthBody):
    item = PENDING.pop(b.token, None)
    if not item or time.time() - item["ts"] > 300:
        raise HTTPException(404, "Unknown or expired authorization token")
    return await asyncio.to_thread(run_cmd, item["command"], item["language"])

# ---------------- chat (SSE, streams from Ollama) ----------------
class ChatBody(BaseModel):
    messages: list[dict]

def build_system(messages):
    mode = load_state().get("mode", "school")
    last = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "").lower()
    skills = [s for s in (parse_skill(p) for p in sorted(SKILLS.glob("*.md"))) if s]
    parts = ["You are AgentOS, a personal agent running locally on the user's machine. Be concise and accurate. Never claim you executed anything; the user runs commands themselves."]
    if mode == "school":
        parts.append("MODE: SCHOOL. Prioritize homework, deadlines, academic planning and note structuring.")
        pend = [t for t in read_tasks() if not re.search(r"(?i)complete|submitted|graded", t.get("status") or "")]
        pend.sort(key=lambda t: t.get("due_iso") or "9")
        lst = "\n".join(f"- [{t.get('course','')}] {t['title']} (due {t.get('due','?')})" for t in pend[:20])
        parts.append("Homework synced from Veracross:\n" + (lst or "(none synced yet)"))
    else:
        parts.append("MODE: DEV. Prioritize software engineering, local git workflows, Homebrew inspection (brew list) and terminal work. When you propose a command, put it in a fenced code block labelled bash (or python) so the user can run it from the UI.")
    mem = MEMORY_FILE.read_text(encoding="utf-8")[-6000:]
    parts.append("Long-term memory:\n" + mem)
    if skills:
        parts.append("Available skills (name: description):\n" + "\n".join(f"- {s['name']}: {s['description']}" for s in skills))
        for s in skills:
            if s["name"].lower() in last:
                parts.append(f"Skill instructions for '{s['name']}':\n{s['body']}")
    return "\n\n".join(parts)

@app.post("/api/chat")
async def chat(b: ChatBody):
    model, _ = await active_model()
    if not model:
        raise HTTPException(503, f"Ollama is not reachable at {OLLAMA} or has no models. Start it and run: ollama pull llama3.1")
    msgs = [{"role": "system", "content": build_system(b.messages)}] + [m for m in b.messages if m.get("role") in ("user", "assistant")]

    async def gen():
        try:
            async with httpx.AsyncClient(timeout=None) as c:
                async with c.stream("POST", f"{OLLAMA}/api/chat", json={"model": model, "messages": msgs, "stream": True}) as r:
                    if r.status_code != 200:
                        yield sse({"error": (await r.aread()).decode(errors="replace")[:500]})
                        return
                    async for line in r.aiter_lines():
                        if not line:
                            continue
                        j = json.loads(line)
                        tok = (j.get("message") or {}).get("content")
                        if tok:
                            yield sse({"token": tok})
                        if j.get("done"):
                            break
        except Exception as e:
            yield sse({"error": str(e)})
        yield sse({"done": True})

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.middleware("http")
async def no_cache(request, call_next):
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-store"
    return resp

app.mount("/", StaticFiles(directory=ROOT / "frontend", html=True), name="frontend")
