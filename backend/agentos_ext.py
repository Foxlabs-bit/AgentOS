"""AgentOS extensions: chats, second brain, per-mode memory/layout/skills, agent with tools."""
import asyncio, html as htmllib, json, os, re, secrets, shutil, time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx, yaml
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

MODES = ("school", "dev")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
ID_RE = re.compile(r"^[a-z0-9]{6,24}$")
TEXT_EXT = {".md", ".txt", ".json", ".csv", ".py", ".js", ".ts", ".html", ".css", ".yaml", ".yml", ".toml"}
BLOCK = {".env", ".ssh", ".aws", ".gnupg", ".netrc", "id_rsa", "id_ed25519", "Keychains", "browser_profile"}
WL = re.compile(r"\[\[([^\]\n]+)\]\]")
FMR = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.S)

class ModeBody(BaseModel):
    mode: str
class RenameBody(BaseModel):
    title: str
class NoteBody(BaseModel):
    mode: str
    path: str
    content: str
class MemBody(BaseModel):
    content: str
class DashBody(BaseModel):
    widgets: list[dict]
class SkillBody2(BaseModel):
    mode: str
    name: str
    description: str = ""
    body: str = ""
class AgentBody(BaseModel):
    mode: str
    chat_id: str
    text: str

TOOLS = [
    {"type": "function", "function": {"name": "web_search", "description": "Search the web. Use for current events, facts you are unsure about, documentation. Returns titles, URLs and snippets.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "search query"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "fetch_url", "description": "Download a web page and return its readable text.",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {"name": "list_files", "description": "List files in the user's notes folder (or a subfolder).",
        "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "relative folder, empty for root"}}}}},
    {"type": "function", "function": {"name": "read_file", "description": "Read a text/markdown file from the notes folder.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "write_file", "description": "Create or overwrite a note (or append to it). Path is relative to the notes folder, e.g. Biology/Cells.md.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}, "append": {"type": "boolean", "description": "true to append instead of overwrite"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "search_notes", "description": "Search the text of all notes for a keyword or phrase.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
]

def setup(app, G):
    ROOT, DATA = G["ROOT"], G["DATA"]
    BRAIN, CHATS, BACKUPS, TRASH = ROOT / "brain", DATA / "chats", DATA / "file_backups", DATA / "trash"
    for d in (BRAIN, CHATS, BACKUPS, TRASH):
        d.mkdir(parents=True, exist_ok=True)
    rt = APIRouter()
    sse = lambda d: "data: " + json.dumps(d) + "\n\n"

    def M(m):
        if m not in MODES:
            raise HTTPException(400, "mode must be school or dev")
        return m

    # ---------------- second brain ----------------
    WELCOME = {
        "school": "# Welcome to your Second Brain\n\nThis folder holds your study notes as plain markdown files. The AI agent can search, read and write them.\n\n- Link notes with double brackets, for example [[Study Plan]]. Click a link to open it, or create it if it does not exist yet.\n- Ask the agent things like: Summarise my notes on photosynthesis, or: Save these notes as Biology/Cells.md\n",
        "dev": "# Welcome to your Second Brain\n\nThis folder holds your dev notes as plain markdown files. The AI agent can search, read and write them.\n\n- Link notes with double brackets, for example [[Snippets]]. Click a link to open it, or create it if it does not exist yet.\n- Ask the agent things like: Save this command cheatsheet as Git/Cheatsheet.md\n",
    }
    for m in MODES:
        d = BRAIN / m
        d.mkdir(exist_ok=True)
        if not any(d.rglob("*.md")):
            (d / "Welcome.md").write_text(WELCOME[m], encoding="utf-8")

    def broot(mode):
        return (BRAIN / M(mode)).resolve()

    def bpath(mode, rel):
        base = broot(mode)
        p = (base / (rel or "").lstrip("/")).resolve()
        if p != base and base not in p.parents:
            raise HTTPException(403, "Outside the notes folder")
        return p

    def walk(mode):
        base, out = broot(mode), []
        for p in sorted(base.rglob("*")):
            try:
                rel = p.relative_to(base)
                if p.is_file() and p.suffix.lower() in TEXT_EXT and not any(x.startswith(".") for x in rel.parts):
                    st = p.stat()
                    out.append({"path": rel.as_posix(), "size": st.st_size, "mtime": st.st_mtime})
            except Exception:
                continue
            if len(out) >= 2000:
                break
        return out

    def search_notes(mode, q, limit=40):
        q = (q or "").strip().lower()
        out = []
        if not q:
            return out
        base = broot(mode)
        for f in walk(mode):
            try:
                txt = (base / f["path"]).read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            if q in f["path"].lower():
                out.append({"path": f["path"], "line": 0, "text": "(file name)"})
            n = 0
            for i, line in enumerate(txt.splitlines(), 1):
                if q in line.lower():
                    out.append({"path": f["path"], "line": i, "text": line.strip()[:160]})
                    n += 1
                    if n >= 3:
                        break
            if len(out) >= limit:
                break
        return out

    @rt.get("/api/brain/tree")
    def brain_tree(mode: str):
        return {"files": walk(mode)}

    @rt.get("/api/brain/file")
    def brain_read(mode: str, path: str):
        p = bpath(mode, path)
        if not p.is_file():
            raise HTTPException(404, "No such note")
        return {"path": path, "content": p.read_text(encoding="utf-8", errors="replace"), "mtime": p.stat().st_mtime}

    @rt.put("/api/brain/file")
    def brain_write(b: NoteBody):
        path = b.path.strip().lstrip("/")
        if not path:
            raise HTTPException(400, "Empty path")
        if not Path(path).suffix:
            path += ".md"
        if Path(path).suffix.lower() not in TEXT_EXT:
            raise HTTPException(400, "Unsupported file type")
        if any(x.startswith(".") for x in Path(path).parts):
            raise HTTPException(400, "Hidden paths are not allowed")
        p = bpath(b.mode, path)
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            shutil.copy2(p, BACKUPS / (str(int(time.time())) + "_" + p.name))
        p.write_text(b.content, encoding="utf-8")
        return {"path": p.relative_to(broot(b.mode)).as_posix()}

    @rt.delete("/api/brain/file")
    def brain_delete(mode: str, path: str):
        p = bpath(mode, path)
        if not p.is_file():
            raise HTTPException(404, "No such note")
        shutil.move(str(p), str(TRASH / (str(int(time.time())) + "_" + p.name)))
        return {"deleted": path}

    @rt.get("/api/brain/search")
    def brain_search(mode: str, q: str = ""):
        return {"results": search_notes(mode, q)}

    @rt.get("/api/brain/links")
    def brain_links(mode: str):
        base, res = broot(mode), {}
        for f in walk(mode):
            try:
                txt = (base / f["path"]).read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            res[f["path"]] = sorted({m.split("|")[0].split("#")[0].strip() for m in WL.findall(txt) if m.strip()})
        return {"links": res}

    # ---------------- chats ----------------
    def cdir(mode):
        d = CHATS / M(mode)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def cfile(mode, cid):
        if not ID_RE.match(cid or ""):
            raise HTTPException(400, "Bad chat id")
        return cdir(mode) / (cid + ".json")

    def load_chat(mode, cid):
        p = cfile(mode, cid)
        if not p.exists():
            raise HTTPException(404, "No such chat")
        return json.loads(p.read_text(encoding="utf-8"))

    def save_chat(c):
        cfile(c["mode"], c["id"]).write_text(json.dumps(c, indent=1), encoding="utf-8")

    def new_chat(mode):
        c = {"id": secrets.token_hex(5), "mode": mode, "title": "New chat", "created": time.time(), "updated": time.time(), "messages": []}
        save_chat(c)
        return c

    @rt.get("/api/chats")
    def chats_list(mode: str):
        out = []
        for p in cdir(mode).glob("*.json"):
            try:
                c = json.loads(p.read_text(encoding="utf-8"))
                out.append({"id": c["id"], "title": c.get("title", "Chat"), "updated": c.get("updated", 0), "count": len(c.get("messages", []))})
            except Exception:
                continue
        out.sort(key=lambda x: -x["updated"])
        return {"chats": out}

    @rt.post("/api/chats")
    def chats_new(b: ModeBody):
        for p in cdir(b.mode).glob("*.json"):
            try:
                c = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if c.get("title") == "New chat" and not c.get("messages"):
                return c
        return new_chat(M(b.mode))

    @rt.get("/api/chats/{mode}/{cid}")
    def chats_get(mode: str, cid: str):
        return load_chat(mode, cid)

    @rt.post("/api/chats/{mode}/{cid}/rename")
    def chats_rename(mode: str, cid: str, b: RenameBody):
        c = load_chat(mode, cid)
        c["title"] = (b.title.strip() or "Chat")[:80]
        save_chat(c)
        return {"title": c["title"]}

    @rt.delete("/api/chats/{mode}/{cid}")
    def chats_delete(mode: str, cid: str):
        p = cfile(mode, cid)
        if p.exists():
            p.unlink()
        return {"deleted": cid}

    # ---------------- per-mode memory / layout / skills ----------------
    def mem_path(mode):
        return G["MEMORY_FILE"] if M(mode) == "school" else (ROOT / "memory" / "long_term_dev.md")

    @rt.get("/api/mem/{mode}")
    def mem_get(mode: str):
        p = mem_path(mode)
        if not p.exists():
            p.write_text("# Long-Term Memory (" + mode + ")\n\n", encoding="utf-8")
        return {"content": p.read_text(encoding="utf-8")}

    @rt.post("/api/mem/{mode}")
    def mem_set(mode: str, b: MemBody):
        mem_path(mode).write_text(b.content, encoding="utf-8")
        return {"saved": True}

    @rt.get("/api/layout/{mode}")
    def lay_get(mode: str):
        cands = [DATA / ("dashboard_" + M(mode) + ".json")]
        if mode == "school":
            cands.append(DATA / "dashboard.json")
        for q in cands:
            if q.exists():
                try:
                    return json.loads(q.read_text(encoding="utf-8"))
                except Exception:
                    pass
        return {"widgets": None}

    @rt.post("/api/layout/{mode}")
    def lay_set(mode: str, b: DashBody):
        (DATA / ("dashboard_" + M(mode) + ".json")).write_text(json.dumps({"widgets": b.widgets}, indent=2), encoding="utf-8")
        return {"saved": True}

    def skills_for(mode):
        out = []
        for p in sorted(G["SKILLS"].glob("*.md")):
            try:
                m = FMR.match(p.read_text(encoding="utf-8"))
                if not m:
                    continue
                meta = yaml.safe_load(m.group(1)) or {}
                if not isinstance(meta, dict) or not meta.get("name"):
                    continue
                sm = str(meta.get("mode", "both")).lower()
                if sm in ("both", "all", mode):
                    out.append({"file": p.name, "name": str(meta["name"]), "description": str(meta.get("description", "")), "mode": sm, "body": m.group(2).strip()})
            except Exception:
                continue
        return out

    @rt.get("/api/mskills")
    def mskills_get(mode: str):
        return {"skills": skills_for(M(mode))}

    @rt.post("/api/mskills")
    def mskills_set(b: SkillBody2):
        s = re.sub(r"[^a-z0-9]+", "-", b.name.lower()).strip("-")
        if not s:
            raise HTTPException(400, "Invalid skill name")
        fm = yaml.safe_dump({"name": b.name.strip(), "description": b.description.strip(), "mode": M(b.mode)}, allow_unicode=True, sort_keys=False)
        (G["SKILLS"] / (s + ".md")).write_text("---\n" + fm + "---\n" + b.body.strip() + "\n", encoding="utf-8")
        return {"saved": "skills/" + s + ".md"}

    # ---------------- agent tools ----------------
    def read_roots(mode=None):
        r = [Path(os.path.expanduser(x.strip())).resolve() for x in os.getenv("AGENT_READ_ROOTS", "").split(",") if x.strip()]
        f = G.get("fs_roots")
        if f and mode:
            r += f(mode)
        return r

    def resolve_ai(mode, path, write=False):
        path = (path or "").strip()
        base = broot(mode)
        if path.startswith(("/", "~")):
            p = Path(os.path.expanduser(path)).resolve()
            roots = [base] + ([] if write else read_roots(mode))
        else:
            p = (base / path).resolve()
            roots = [base]
        if not any(p == r or r in p.parents for r in roots):
            raise ValueError("Path not allowed. Use a path inside the notes folder" + ("" if write else " (or a folder listed in AGENT_READ_ROOTS)") + ".")
        if any(part in BLOCK for part in p.parts):
            raise ValueError("Protected path.")
        if G.get("fs_blocked") and G["fs_blocked"](p):
            raise ValueError("Protected path.")
        return p

    def t_list(mode, path=""):
        p = resolve_ai(mode, path or ".")
        if not p.is_dir():
            return "Not a directory."
        items = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
        items = [i for i in items if not i.name.startswith(".")][:200]
        return "\n".join(("[dir] " + i.name) if i.is_dir() else (i.name + " (" + str(i.stat().st_size) + " bytes)") for i in items) or "(empty)"

    def t_read(mode, path, start=0):
        p = resolve_ai(mode, path)
        if not p.is_file():
            return "File not found."
        data, note = G["fs_extract"](p)
        if not data:
            return note or "Empty file."
        try:
            start = max(0, int(start))
        except Exception:
            start = 0
        chunk = data[start:start + 12000]
        more = len(data) - start - len(chunk)
        return chunk + ("\n...[truncated; " + str(more) + " more characters. Call read_file again with start=" + str(start + 12000) + " to continue]" if more > 0 else "")

    def t_write(mode, path, content, append=False):
        path = (path or "").strip()
        if not path:
            return "Missing path."
        if not Path(path).suffix:
            path += ".md"
        if Path(path).suffix.lower() not in TEXT_EXT:
            return "Unsupported file type."
        if len(content or "") > 200000:
            return "Content too large."
        p = resolve_ai(mode, path, write=True)
        if any(x.startswith(".") for x in p.relative_to(broot(mode)).parts):
            return "Hidden paths are not allowed."
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            shutil.copy2(p, BACKUPS / (str(int(time.time())) + "_" + p.name))
        if append and p.exists():
            with open(p, "a", encoding="utf-8") as f:
                f.write(("" if (p.read_text(encoding="utf-8").endswith("\n")) else "\n") + content)
        else:
            p.write_text(content, encoding="utf-8")
        return "Saved " + str(len(content)) + " characters to " + p.relative_to(broot(mode)).as_posix()

    def t_notes(mode, query):
        res = search_notes(mode, query, 25)
        return "\n".join(x["path"] + ((":" + str(x["line"])) if x["line"] else "") + "  " + x["text"] for x in res) or "No matches."

    def clean(s):
        return htmllib.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()

    def t_search(q):
        q = (q or "").strip()
        if not q:
            return "Empty query."
        res = []
        try:
            from ddgs import DDGS
            for x in DDGS().text(q, max_results=6):
                res.append((x.get("title", ""), x.get("href", ""), x.get("body", "")))
        except Exception:
            res = []
        if not res:
            try:
                r = httpx.post("https://html.duckduckgo.com/html/", data={"q": q}, headers={"User-Agent": UA}, timeout=15, follow_redirects=True)
                for blk in re.split(r'class="result__a"', r.text)[1:7]:
                    h = re.search(r'href="([^"]+)"', blk)
                    t = re.search(r">(.*?)</a>", blk, re.S)
                    sn = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', blk, re.S)
                    if not h:
                        continue
                    href = h.group(1)
                    if "uddg=" in href:
                        href = parse_qs(urlparse(href if href.startswith("http") else "https:" + href).query).get("uddg", [href])[0]
                    res.append((clean(t.group(1)) if t else "", href, clean(sn.group(1)) if sn else ""))
            except Exception:
                res = []
        if not res:
            try:
                r = httpx.get("https://en.wikipedia.org/w/api.php", params={"action": "query", "list": "search", "srsearch": q, "format": "json", "srlimit": 5}, headers={"User-Agent": UA}, timeout=15)
                for x in r.json().get("query", {}).get("search", []):
                    res.append((x["title"], "https://en.wikipedia.org/wiki/" + x["title"].replace(" ", "_"), clean(x.get("snippet", ""))))
            except Exception:
                pass
        if not res:
            return "No results (search is unavailable or blocked right now)."
        return "\n\n".join(str(i + 1) + ". " + t + "\n   " + u + "\n   " + s for i, (t, u, s) in enumerate(res))

    def t_fetch(url):
        u = urlparse((url or "").strip())
        host = (u.hostname or "").lower()
        if u.scheme not in ("http", "https") or not host:
            return "Invalid URL."
        if host == "localhost" or host.endswith(".local") or host in ("::1", "0.0.0.0") or re.match(r"^(127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|169\.254\.)", host):
            return "Blocked: local or private address."
        resp = httpx.get(u.geturl(), headers={"User-Agent": UA}, timeout=20, follow_redirects=True)
        ctype = resp.headers.get("content-type", "")
        if not any(k in ctype for k in ("html", "text", "json")):
            return "Unsupported content type: " + ctype
        txt = resp.text
        txt = re.sub(r"(?is)<(script|style|noscript|svg|nav|footer|header)[^>]*>.*?</\1>", " ", txt)
        txt = re.sub(r"(?s)<[^>]+>", " ", txt)
        txt = re.sub(r"\s+", " ", htmllib.unescape(txt)).strip()
        return txt[:6000] + (" ...[truncated]" if len(txt) > 6000 else "")

    async def run_tool(name, args, mode):
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {}
        args = args or {}
        try:
            if name == "web_search":
                return await asyncio.to_thread(t_search, str(args.get("query", "")))
            if name == "fetch_url":
                return await asyncio.to_thread(t_fetch, str(args.get("url", "")))
            if name == "list_files":
                return await asyncio.to_thread(t_list, mode, str(args.get("path", "")))
            if name == "read_file":
                return await asyncio.to_thread(t_read, mode, str(args.get("path", "")), args.get("start", 0))
            if name == "write_file":
                ap = str(args.get("append", "")).lower() in ("true", "1", "yes")
                return await asyncio.to_thread(t_write, mode, str(args.get("path", "")), str(args.get("content", "")), ap)
            if name == "search_notes":
                return await asyncio.to_thread(t_notes, mode, str(args.get("query", "")))
        except Exception as e:
            return "Tool error: " + str(e)
        if G.get("fs_tool"):
            res = await asyncio.to_thread(G["fs_tool"], mode, name, args)
            if res is not None:
                return res
        return "Unknown tool: " + str(name)

    # ---------------- agent ----------------
    def homework_ctx():
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        rows = []
        for t in G["read_tasks"]():
            due = G["resolve_due"](t.get("due", "")) or t.get("due_iso")
            if not due:
                continue
            try:
                d = (datetime.fromisoformat(due) - today).days
            except Exception:
                continue
            if d < -2 or d > 28:
                continue
            if re.match(r"(?i)\s*(complete|submitted|graded|turned in|excused)", t.get("status") or ""):
                continue
            when = "today" if d == 0 else ("in %d days" % d if d > 0 else "%d days overdue" % -d)
            rows.append((d, "- [" + str(t.get("course", "")) + "] " + str(t.get("title", "")) + " - due " + str(t.get("due", "")) + " (" + when + ")" + ((" - " + t["status"]) if t.get("status") else "")))
        rows.sort(key=lambda x: x[0])
        return "\n".join(x[1] for x in rows[:35]) or "(no pending homework synced)"

    def build_system(mode, last_user):
        now = datetime.now().strftime("%A %d %B %Y, %H:%M")
        parts = [
            "You are AgentOS, a personal assistant running locally on the user's Mac (" + mode.upper() + " mode). Today is " + now + ". Be concise, accurate and practical.",
            "TOOLS: web_search and fetch_url for the internet; list_files, read_file, write_file and search_notes for the user's second brain (markdown notes in the " + mode + " notes folder, relative paths like Biology/Cells.md). Search the web for anything current or that you are unsure about, and cite the URLs you used. Read a note before editing it. Only say you saved or read a file if the tool reported success. You cannot run terminal commands yourself. FILE CENTER: list_locations shows the folders you may read (such as OneDrive, Documents, Desktop); use find_files to search them by name and read_file with the full path to read text, PDF, Word, PowerPoint and Excel files. Those locations are read-only; writes go only to the notes folder.",
        ]
        if mode == "school":
            parts.append("Focus: homework, deadlines, study planning and note-taking.\nPending homework synced from Veracross:\n" + homework_ctx())
        else:
            parts.append("Focus: software engineering, git workflows, Homebrew and terminal work. When you propose a shell command, put it in a fenced bash code block so the user can run it with one click.")
        mp = mem_path(mode)
        mem = mp.read_text(encoding="utf-8")[-5000:] if mp.exists() else ""
        if mem.strip():
            parts.append("Long-term memory:\n" + mem)
        names = [f["path"] for f in walk(mode)][:40]
        parts.append("Notes in the second brain: " + (", ".join(names) or "(none yet)"))
        sk = skills_for(mode)
        if sk:
            parts.append("Available skills:\n" + "\n".join("- " + s["name"] + ": " + s["description"] for s in sk))
            for s in sk:
                if s["name"].lower() in last_user.lower():
                    parts.append("Skill instructions for '" + s["name"] + "':\n" + s["body"])
        return "\n\n".join(parts)

    @rt.post("/api/agent")
    async def agent(b: AgentBody):
        mode = M(b.mode)
        chat = load_chat(mode, b.chat_id)
        model, _ = await G["active_model"]()
        if not model:
            raise HTTPException(503, "Ollama is not reachable at " + G["OLLAMA"] + " or has no models. Open the Ollama app and pull a model.")
        text = b.text.strip()
        if not text:
            raise HTTPException(400, "Empty message")
        chat["messages"].append({"role": "user", "content": text})
        if chat.get("title") in (None, "", "New chat"):
            chat["title"] = text.replace("\n", " ")[:48]
        chat["updated"] = time.time()
        save_chat(chat)
        system = build_system(mode, text)
        hist = [{"role": m["role"], "content": m["content"]} for m in chat["messages"] if m["role"] in ("user", "assistant")][-30:]
        ollama = G["OLLAMA"]
        nctx = int(os.getenv("OLLAMA_NUM_CTX", "8192"))

        async def gen():
            msgs = [{"role": "system", "content": system}] + hist
            use_tools, final, used = True, "", []
            try:
                async with httpx.AsyncClient(timeout=None) as c:
                    for step in range(7):
                        payload = {"model": model, "messages": msgs, "stream": True, "options": {"num_ctx": nctx}}
                        if use_tools:
                            payload["tools"] = TOOLS
                        calls, txt = [], ""
                        async with c.stream("POST", ollama + "/api/chat", json=payload) as resp:
                            if resp.status_code != 200:
                                body = (await resp.aread()).decode(errors="replace")
                                if use_tools and resp.status_code == 400 and "tool" in body.lower():
                                    use_tools = False
                                    yield sse({"notice": "This model does not support tools, so web search and file access are off. Use llama3.1 or qwen2.5 for those."})
                                    continue
                                yield sse({"error": body[:400]})
                                break
                            async for line in resp.aiter_lines():
                                if not line:
                                    continue
                                j = json.loads(line)
                                m = j.get("message") or {}
                                if m.get("content"):
                                    txt += m["content"]
                                    yield sse({"token": m["content"]})
                                for tc in m.get("tool_calls") or []:
                                    calls.append(tc)
                                if j.get("done"):
                                    break
                        final += txt
                        if not calls:
                            break
                        if txt.strip():
                            final += "\n\n"
                            yield sse({"token": "\n\n"})
                        msgs.append({"role": "assistant", "content": txt, "tool_calls": calls})
                        for tc in calls:
                            fn = tc.get("function") or {}
                            name, args = fn.get("name", ""), fn.get("arguments") or {}
                            shown = args
                            if isinstance(shown, str):
                                try:
                                    shown = json.loads(shown)
                                except Exception:
                                    shown = {}
                            used.append({"name": name, "args": shown})
                            yield sse({"tool": {"name": name, "args": shown}})
                            result = await run_tool(name, args, mode)
                            msgs.append({"role": "tool", "tool_name": name, "content": str(result)[:12000]})
                        if step == 6:
                            yield sse({"notice": "Stopped after too many tool steps."})
            except Exception as e:
                yield sse({"error": str(e)})
            finally:
                chat["messages"].append({"role": "assistant", "content": final.strip(), "tools": used})
                chat["updated"] = time.time()
                try:
                    save_chat(chat)
                except Exception:
                    pass
            yield sse({"done": True})

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    app.include_router(rt)
