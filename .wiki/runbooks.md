# Runbooks

Copy-pasteable procedures. Add to this file whenever you work out a repeatable
workflow — don't leave it in a chat log.

---

## 1. Probe the remote server (read-only)

**Workstation → host.** The alias `takeout-server` is in `~/.ssh/config`.
Always use `-o BatchMode=yes` so a missing key fails fast instead of hanging on a prompt.

```bash
ssh -o BatchMode=yes -o ConnectTimeout=10 takeout-server 'hostname; uptime; free -g | head -2'
```

**⚠️ Quoting hazard (learned the hard way):** inline multi-line `ssh '...'` payloads
get mangled by Git Bash (nested quotes + heredocs break, and the host may lack
`python3` on `$PATH`). **Write the probe to a local file, `scp` it, run it:**

```bash
# 1. write .recon/probe.sh locally (see .recon/probe2.sh for a working example)
# 2. copy and run
scp -q -o BatchMode=yes .recon/probe2.sh takeout-server:/tmp/tk_probe2.sh
ssh  -o BatchMode=yes takeout-server 'bash /tmp/tk_probe2.sh'
```

Useful one-liners (no nested quotes):

```bash
# disk truth — /config is CONTAINER-ONLY; check the host backing volume instead
ssh -o BatchMode=yes takeout-server 'df -h / /opt/local_cache_crypt /opt/archives'

# container mounts (authoritative)
ssh -o BatchMode=yes takeout-server \
  'docker inspect takeout-webgui --format "{{range .Mounts}}{{.Type}} {{.Source}} -> {{.Destination}} (rw={{.RW}}){{println}}{{end}}"'

# memory hogs
ssh -o BatchMode=yes takeout-server 'ps -eo pid,rss,etime,comm --sort=-rss | head -12'
ssh -o BatchMode=yes takeout-server 'docker stats --no-stream --format "{{.Name}} mem={{.MemUsage}} cpu={{.CPUPerc}}"'
```

**Pitfall:** `df /config` on the host prints nothing and exits silently — that is
not evidence the disk is small. `/config` exists only inside the container.

---

## 2. Inspect Chromium's tab state over CDP

Chromium listens on `127.0.0.1:9222` **inside** the container, so go through `docker exec`.

```bash
# raw target list (large — ~328 KB with hundreds of tabs)
ssh -o BatchMode=yes takeout-server 'docker exec takeout-webgui sh -c "curl -s 127.0.0.1:9222/json/list"'

# summarise by type + URL, and count the pathological sign-in tabs
ssh -o BatchMode=yes takeout-server 'docker exec takeout-webgui sh -c "curl -s 127.0.0.1:9222/json/list > /tmp/tl.json; python3 - <<PY
import json,collections
t=json.load(open(\"/tmp/tl.json\"))
print(\"total targets:\", len(t))
for k,v in collections.Counter((x.get(\"type\"), x.get(\"url\",\"\").split(\"?\")[0][:70]) for x in t).most_common(12):
    print(\"  \", v, k)
print(\"google-account:\", sum(1 for x in t if \"accounts.google.com\" in x.get(\"url\",\"\")))
PY"'
```

*(The heredoc-above works when the whole thing is passed as one `ssh` argument; if it
mangles, move it into a `scp`'d script per Runbook 1.)*

**What "healthy" looks like:** ~1 `takeout.google.com/manage` page, ~1 webgui page,
1 extension service worker, and **0** `accounts.google.com/v3/signin/identifier` pages.

---

## 3. Mine a previous Claude Code session (forensics)

**When you need:** the thinking/plan/findings of a dead or past Claude Code session,
including sessions killed by a rate limit mid-workflow.

**Where sessions live** — one directory per project, name derived from the cwd with
separators replaced by `-`:

```
~/.claude/projects/<mangled-cwd>/<session-uuid>.jsonl          # main transcript
~/.claude/projects/<mangled-cwd>/<session-uuid>/subagents/workflows/<wf-id>/
        journal.jsonl                                          # per-agent result ledger
        agent-<hash>.jsonl + agent-<hash>.meta.json            # one transcript per agent
~/.claude/projects/<mangled-cwd>/<session-uuid>/workflows/scripts/<name>-<wf-id>.js  # the workflow source
~/.claude/tasks/<session-uuid>/                                # task list
~/.claude/jobs/<short-id>/                                     # job scratch
```

**Parse it without blowing your context** (transcripts are megabytes):

```bash
node -e '
const fs=require("fs");
const recs=fs.readFileSync(process.argv[1],"utf8").split("\n").filter(Boolean)
  .map(l=>{try{return JSON.parse(l)}catch(e){return null}}).filter(Boolean);
const types={}; recs.forEach(r=>types[r.type]=(types[r.type]||0)+1);
console.log(types);
' "<path-to>.jsonl"
```

Useful record `type`s: `user`, `assistant`, `attachment`, `ai-title`, `agent-name`,
`worktree-state`, `file-history-snapshot`, `queue-operation`.

**Extraction recipes:**
- *Human messages only* — take `type==="user"` records and **skip** any whose
  `message.content` array contains a `tool_result`.
- *The plan/narration* — `type==="assistant"`, join `message.content[].text` blocks.
- *What it actually did* — `type==="assistant"` → `message.content[]` where `type==="tool_use"`, read `.name` + `.input`.

**Two gotchas that cost real time:**
1. **Context-mode indexing:** the sandbox tool (`ctx_execute`) with an `intent`
   parameter returns *indexed section titles instead of stdout*. Either drop
   `intent`, or write the output to a file with `fs.writeFileSync` and read that.
2. **Recovering a dead run:** read `journal.jsonl` first. `{"type":"result",...}` lines
   hold each completed agent's full return value; `{"type":"failed",...}` lines name
   the agent ids that died. The saved workflow script (below) lets you **re-run** the
   same angles cheaply, because unchanged `(prompt, opts)` pairs replay from cache.

**Salvage alert:** agents killed by `rate_limit` are usually **not empty** — they did
the work and failed only at the final write. Their `agent-*.jsonl` `tool_result`
payloads (and any scratch files they left in the worktree) contain findings and
fetched sources worth recovering before re-running anything.

---

## 4. Operate the project's Python env (local)

```bash
# v2 test baseline (fast, network-free)
python -m pytest tests/v2 -q

# dependency parity check — a module can be installed locally but MISSING in the
# server container's /opt/manager-venv. Always diff the two before trusting a code
# path that "passes tests".
python -c "import websocket, httpx; print(websocket.__version__, httpx.__version__)"
```

**Pitfall:** local test success has historically masked a missing server dependency
(`websocket-client`). Tests passing ≠ the path can run in production.

---

## 5. Server cleanup: reclaim memory from stacked sign-in tabs

**Full gated procedure: `.recon/07-live-state-and-cleanup-plan.md`** (main checkout).
Summary of the ordering rule — **this order is the whole trick**:

1. **Phase 0 — verify** no real download is in flight (`takeout-download.usercontent.google.com`
   targets). Abort if any exist.
2. **Phase 1 — stop the spawner** (the loop that opens a tab per tick). Prove the tab
   count has gone flat before continuing.
3. **Phase 2 — close only** `page` targets whose URL contains `accounts.google.com`,
   preserving the manage tab, the webgui tab, and the extension worker. Batch ~20 at a
   time.
4. **Phase 3 — verify** the reclaim: sign-in pages = 0, total targets ≤ ~6,
   `available` memory > 2 GB, chromium process count materially down.
5. **Phase 4 — optional Chromium restart:** separate approval. Discards a 43-day-old
   browser session; the on-disk profile *should* preserve auth but that is **unverified**.

**Never** kill Chromium, delete the profile, or cancel downloads as part of this.
Closing a sign-in tab is non-destructive; restarting the browser is not.

---

## 6. Measure the download-attempt cost (the decisive experiment)

**Full protocol: `.recon/09-decisive-experiment.md` (main checkout).** This settles the project's
oldest unmeasured assumption: *does a `Range` probe / aborted GET / resumed download consume one of
Google's ~5 downloads per archive?* It also decides the open architecture conflict
(browser-native vs `httpx`-downloads), because `resume` is Option B's entire justification.

**The harness already exists** — `takeout2/experiment.py`. It diffs Google's **own** manage-page
counter (`Number of times already downloaded: N`, already scraped into `dl_counts`) before/after one
controlled action. `run_study()` refuses to run without `confirm_throwaway=True`, uses a fresh part
index per sample, and gates every action on the ledger budget. It writes
`docs/v2/ATTEMPT-COST-FINDINGS.md` — the file whose absence has been flagged repeatedly.

**Two facts that make this cheaper than it looks:**

1. ⚠️ **The cookie path has TWO stacked defects — this earlier claim was WRONG and is corrected here.**
   `contracts.py:356` defaults `CDP_URL` to `http://127.0.0.1:9222`, which websocket clients reject
   (`ValueError: scheme http is invalid` / `InvalidURI: scheme isn't ws or wss` — reproduced with two
   independent clients). **But fixing only the scheme fails too:** a bare `ws://127.0.0.1:9222` is
   rejected by Chrome with `InvalidStatus: server rejected WebSocket connection: HTTP 404`, because
   Chrome exposes **no** WebSocket endpoint at the bare authority. The endpoint must carry a path —
   `ws://127.0.0.1:9222/devtools/browser/<uuid>` from `GET /json/version`, or a per-target
   `webSocketDebuggerUrl` from `GET /json/list`. **That UUID changes on every browser restart**, so
   `TK2_CDP_URL` cannot durably fix this — **a code change is required** (discover at runtime).
   Verified working end-to-end once the advertised endpoint is used: **49 cookies, 28 `google.com`,
   a 3,488-byte Cookie header**, including `__Secure-1PSIDTS`.
2. The only real gap is the **transport driver** (`dl_scrape` / `cookie_source` /
   `transport_factory` are injected and unwired).

**Order matters.** Clean the 314 sign-in tabs FIRST (Runbook 5) — the box is at 0 available memory,
and a failed page scrape would otherwise be ambiguous between "Google changed the page" and "out of
RAM".

**Validate the instrument before spending anything** (free, and non-negotiable):

```bash
# read the counter twice with NO action in between — the delta MUST be 0
# (inside takeout-webgui; discover the endpoint first — the bare host:port 404s)
ENDPOINT=$(curl -s 127.0.0.1:9222/json/version \
  | python3 -c "import json,sys;print(json.load(sys.stdin)['webSocketDebuggerUrl'])")
```

If that delta is non-zero, stop: every later number is noise. This project's history is a catalogue
of acting on unvalidated instruments.

**Interpretation of the key cell:** `resume` = **FREE** favours Option B (server-side `httpx` with
`Range` resume preserves budget); `resume` = **COSTS 1** destroys Option B's stated advantage and
favours Option A (browser-native). The `full` row must come out at exactly **1** — if it does not, the
oracle is wrong and no other row can be trusted.

---

## 8. See the screen and click on it (the VNC desktop)

Chromium in `takeout-webgui` runs against **`DISPLAY=:1`** (Xvfb, live viewport **2142x1372**), served over
KasmVNC. So the whole desktop is observable and drivable — not just the page DOM. Verified 2026-09-19.

**This is the fallback for anything CDP cannot reach** — native Chrome dialogs, the download shelf, KDE
notifications — and it gives **pixels as ground truth** when the DOM reports misleadingly. It immediately
showed something a DOM read had missed: **two tabs** open (`Google Takeout` + `Download history`).

### See the browser viewport (cheapest, needs only CDP)

```bash
# Page.captureScreenshot -> base64 PNG; no ImageMagick required
ssh takeout-server 'docker exec takeout-webgui python3 /tmp/tk_sc.py'   # see .recon/probe_see_click.py
# then read it with a vision model, or just docker cp it out:
ssh takeout-server 'docker cp takeout-webgui:/config/_screen.png /tmp/_screen.png'
scp takeout-server:/tmp/_screen.png ./
```

### See the WHOLE desktop

`xwd` is installed; **ImageMagick, netpbm and ffmpeg are not** — hence `tools/xwd_to_png.py`.

```bash
ssh takeout-server 'docker exec takeout-webgui sh -c "DISPLAY=:1 xwd -root -silent -out /tmp/d.xwd"'
scp tools/xwd_to_png.py takeout-server:/tmp/ && \
ssh takeout-server 'docker cp /tmp/xwd_to_png.py takeout-webgui:/tmp/ >/dev/null && \
  docker exec takeout-webgui python3 /tmp/xwd_to_png.py /tmp/d.xwd /tmp/d.png'
```

Expected: `parsed XWD: {bpp: 24, width: 2144, height: 1372, byte_order: 0, ncolors: 256}` then a ~200 KB
PNG. **Look at the output before trusting it** — a wrong pixel stride produces a diagonally-sheared image
that still saves without error.

```bash
# Pillow ships an XwdImagePlugin but it REJECTS Xvfb's dump:
#   PIL.UnidentifiedImageError: cannot identify image file
# Use tools/xwd_to_png.py instead.
```

### Click and type

```bash
ssh takeout-server 'docker exec takeout-webgui sh -c "DISPLAY=:1 xdotool getdisplaygeometry"'
ssh takeout-server 'docker exec takeout-webgui sh -c "DISPLAY=:1 xdotool getactivewindow getwindowname"'
#   -> "Google Takeout - Chromium"

# targeted input: focus the browser window, then move/click/type
#   DISPLAY=:1 xdotool search --name Chromium windowactivate
#   DISPLAY=:1 xdotool mousemove X Y click 1
#   DISPLAY=:1 xdotool type --delay 60 'text here'
#   DISPLAY=:1 xdotool key Return
```

**Pitfall (learned the hard way):** `docker exec` does **not** forward stdin without `-i`, so
`docker exec ctr python3 - <<'PY'` silently does nothing. Write the script to a file,
`docker cp` it in, and run it by path — the pattern used everywhere above.

**Design consequence for ReAuth:** `xdotool` can type the password step, so the human burden drops from
*"open the webgui and type a password"* to *"approve a prompt"* — with SMS/prompt 2FA the second factor
still needs the owner's phone. Worth weighing before implementing the re-auth path.

---

## 9. Unblock a `needs_reauth` run (the "Password challenge")

**Symptom.** `autopilot run --mint-only` exits with
`error: ReAuth required (hit https://accounts.google.com/ServiceLogin?passive=1209600&continue=…)`
and the job status is `needs_reauth`. Nothing is wrong with the code: Google is refusing to mint
without a fresh interactive proof.

**Why it cannot be automated away entirely.** `passive=1209600` is a *passive* check with no UI.
Google raises the **interactive** challenge only for *user-initiated* navigation. A CDP
`Input.dispatchMouseEvent` / `dispatchKeyEvent` is trusted, so it counts as user-initiated;
`location.href = …` from page JS does not. That is the whole trick.

### Put the password where the handler can read it (once)

```bash
# on the workstation: the file already exists on the server at <repo>/config/gPass.txt
ssh takeout-server 'ls -la /opt/local_cache_crypt/_projects/takeout-downloader/config/gPass.txt'
```

`config/` is gitignored (`.gitignore:126`) so the secret cannot be committed. The host directory
is owned by the container's uid 1000, so **chmod it inside the container**, not on the host:

```bash
ssh takeout-server 'docker exec takeout-webgui chmod 600 /config/gPass.txt'
```

### Get the handler into the container

The host `config/` is not writable by `scp` (uid mismatch), so stage and `docker cp`:

```bash
R=/opt/local_cache_crypt/_projects/takeout-downloader
scp tools/satisfy_login.py takeout-server:/tmp/satisfy_login.py
ssh takeout-server "docker cp /tmp/satisfy_login.py takeout-webgui:/config/satisfy_login.py"
```

### Always dry-run first — it types nothing

```bash
ssh takeout-server 'docker exec takeout-webgui python3 -B /config/satisfy_login.py --dry-run'
```

Expect `hasPassword: true`, `hasButton: true`, and a non-zero box for each. If
`hasPassword` is false there is nothing to satisfy and the script exits 0.

### Submit

```bash
ssh takeout-server 'docker exec takeout-webgui python3 -B /config/satisfy_login.py'
```

Exit codes: `0` satisfied or nothing to do · `2` form not found / button missing · `3`
`gPass.txt` missing or empty · `4` **password rejected** (also: no error text but the step did
not clear). There is **no retry** on purpose — a rejected password on a repeated challenge can
escalate to a lockout.

### Confirm, then resume

```bash
# the archive page must now carry rapt
ssh takeout-server 'docker exec takeout-webgui python3 -c "
import json, re, urllib.request
for x in json.load(urllib.request.urlopen(\"http://127.0.0.1:9222/json/list\")):
    if x.get(\"type\") == \"page\":
        print(re.sub(r\"(rapt=)[^&]+\", r\"\1<R>\", x.get(\"url\", \"\"))[:110])
"'

# then re-run the SAME mint command — already-earned URLs are skipped at zero cost
bash .recon/_resume_mint62.sh
```

**Gotchas.**

- **2FA wins.** If Google demands a second factor, no file can satisfy it. A human is required.
- **The password is only part of it.** `rapt` gates **minting**, not transferring. A run whose
  every part is already minted does not need it — `page_from_ledger()` rebuilds the page from the
  ledger so the transfer can proceed offline.
- **Never echo the file.** The handler prints the source path and character count only.
- **Watch the stray downloads.** With `autoCancelDownloads` OFF (the standing state) every mint
  leaves a real browser download running — failure mode 1.22. Sweep `config/Downloads/*.crdownload`
  before starting a transfer.

---

## 10. Prove which code is actually running (do this after every deploy)

A deploy command succeeding is not evidence that the deployed code is executing. On 2026-09-22
three extension fixes had been committed, "deployed", and were still not running: Chromium
loaded `--load-extension=/work/helpers` (the main checkout) while the fixes lived in
`/work/.v3/helpers`. Everything looked healthy, because the *storage* was correct and storage
alone suppresses the symptom.

**Never verify a deployment by timestamp or by `git log`. Verify by behaviour.**

### Python (v3)

```bash
# the deployed commit
ssh takeout-server 'cd /opt/local_cache_crypt/_projects/takeout-downloader/.v3 && git log --oneline -1'

# import the thing you just changed, IN the container, and assert on it
ssh takeout-server 'docker exec -w /work/.v3 takeout-webgui python3 -B -c "
from autopilot.ledger import JOB_STATUSES
print(\"minting in JOB_STATUSES:\", \"minting\" in JOB_STATUSES)
"'
```

An `ImportError` here means the deploy did not land, however clean `git log` looked.

### The extension

```bash
# 1. which directory does Chromium actually load?
ssh takeout-server 'docker exec takeout-webgui sh -c "ps -eo args | grep [c]hromium | head -1" \
  | tr " " "\n" | grep load-extension'

# 2. probe the RUNNING code, not the file
scp .recon/_which_ext_code.py takeout-server:/tmp/
ssh takeout-server 'docker cp /tmp/_which_ext_code.py takeout-webgui:/config/'
ssh takeout-server 'docker exec takeout-webgui python3 -B /config/_which_ext_code.py'
```

The probe reads `chrome.alarms.getAll()` and `chrome.storage.local`. With
`autoRecapture: false`:

| observation | meaning |
|---|---|
| `takeout-recapture-poll` present | **the OLD `background.js` is executing** — it creates the alarm unconditionally |
| alarms empty | the new code is executing (it clears/never creates it) |

This works because an alarm's presence is a direct consequence of which code ran, whereas mtime
and `git log` describe the file, not the process.

### Reload the extension after changing it

```bash
ssh takeout-server 'docker exec -w /work/.v3 takeout-webgui python3 -B -c "
import asyncio, json, urllib.request, sys
sys.path.insert(0, \"/work/.v3\")
from autopilot.cdp import CdpSession
from autopilot.ws_transport import WebSocketTransport
async def main():
    ts = json.load(urllib.request.urlopen(\"http://127.0.0.1:9222/json/list\"))
    for t in [x for x in ts if (x.get(\"url\") or \"\").startswith(\"chrome-extension://\")
              and x.get(\"webSocketDebuggerUrl\")]:
        tr = await WebSocketTransport.connect(t[\"webSocketDebuggerUrl\"])
        async with CdpSession(tr) as s:
            await s.value(\"chrome.runtime.reload(); \\\"reloading\\\"\")
asyncio.run(main())
"'
```

Then re-run the probe and confirm the verdict actually changed.

**Gotchas.**

- **`chrome.alarms` entries survive a browser restart** (measured 2026-09-19). A stale alarm
  keeps waking the service worker forever and can look like live behaviour — which is how the
  first reload check wrongly concluded the old code was still running.
- **`--load-extension=/work/helpers` is baked into the image** by `webgui/init_custom.sh` and
  written to `/usr/local/bin/takeout-chromium` at container boot. Patching the source needs a
  rebuild; patching the live script needs only a restart. Do both.
- **A passing test is not a deploy.** `tests/v3/test_extension_defaults.py` asserts the source in
  the worktree — it will pass green while the browser runs an August build of the same file.
