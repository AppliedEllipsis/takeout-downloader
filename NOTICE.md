# NOTICE — Attribution, License Change & Previous Project

## Previous project

This repository is a fork of:

> **`Kalainilavann/takeout_downloader_script`**
> <https://github.com/Kalainilavann/takeout_downloader_script>

The original project was first published by **Clive Watts**
(`clive@clivewatts.net`) and later republished and expanded under the
`Kalainilavann` GitHub account. This fork preserves that authorship in the
commit history and adds new features, security hardening, a TUI,
aria2c integration, and a browser extension on top. (Earlier revisions
of this fork also shipped a Flask/SocketIO web UI, a Tampermonkey
userscript, and a bookmarklet; these were removed in v6.0.0 in favor of
an extension-only capture flow — see the change log below.)

### How to access the previous repository

If you want the previous project as it was before this fork's changes:

```bash
# Option A: clone the original upstream directly
git clone https://github.com/Kalainilavann/takeout_downloader_script.git

# Option B: add it as a remote to this checkout and inspect its history
git remote add upstream https://github.com/Kalainilavann/takeout_downloader_script.git
git fetch upstream
git log upstream/main        # full upstream history

# Option C: see a specific pre-fork commit
git log --all --oneline | less
```

The last commit of this fork before the public-history rewrite can still
be reached via the original remote, but see the section below: the
upstream's `outscorn/` directory has been intentionally removed from
this fork's published history.

---

## License change

### Previous license (upstream)

The upstream repository
(`Kalainilavann/takeout_downloader_script`) **did not ship a `LICENSE`
file** at any point in its published history. Under the Berne Convention
and default copyright law, that means **all rights are reserved by the
author(s)** — the code was not, strictly speaking, open source, even
though the upstream README implied it was freely usable. Forking and
redistributing it as if it were permissively licensed would have been
on shaky ground.

### Current license (this fork)

As of the first commit on this fork's public branch, this project is
licensed under the **[GLWTS (Good Luck With That Shit) Public License]**.

[GLWTS (Good Luck With That Shit) Public License]: ./LICENSE

See [`LICENSE`](./LICENSE) for the full text.

### Change log

| Date       | Event                                                                                          |
|------------|------------------------------------------------------------------------------------------------|
| 2025-12-05 | Upstream initial commit by Clive Watts (no license declared).                                  |
| 2025–2026  | Development continues under Kalainilavann's account; still no license file.                    |
| 2026-06-13 | Fork prepared for publication. `outscorn/`, `_tmp/` test artifacts, and `_final_test.txt` are removed from working tree and rewritten out of git history via `git-filter-repo` (see *Safety cleanup* below). `LICENSE` added under GLWTS Public License. Docker-compose NAS-mount path normalized to `/path/to/downloads`. Remote changed to `AppliedEllipsis/takeout-downloader`. |
| 2026-06-14 | **v6.0.0** — Web UI removed (`google_takeout_web.py`, `Dockerfile`, `docker-compose.yml`, Flask/SocketIO deps). Tampermonkey userscript and bookmarklet dropped. Architecture is now **TUI + browser extension only**: the extension captures a download request and copies it as JSON, the user pastes it into the TUI. No server, no auto-send, no network egress from the extension. TUI rings the terminal bell and flashes its title bar when the session cookie expires. New `takeout_payload.py` schema module bridges both sides. |

---

## Safety cleanup

The following content was present in the upstream repository (and in the
early, unpublished commits of this fork) and has been **removed from this
fork's published history** before going public. It is not present in
either the working tree or any commit reachable from `main`.

### `outscorn/` — REMOVED

The upstream repo shipped three pre-built `.zip` files under `outscorn/`:

- `downloader-script-takeout-v1.1.zip`
- `downloader_takeout_script_v3.8.zip`
- `takeout-downloader-script-galvanocauterization.zip`

Inspection of the most recent of these (`downloader_takeout_script_v3.8.zip`)
revealed:

- An archive containing `gcc.exe` (~651 KB — far smaller than a real
  GCC toolchain, which is ~50 MB+), `Launch.cmd`, and a `ptd.txt`
  consisting of ~300 KB of obfuscated Lua bytecode/source.
- The Lua payload uses obfuscated `string.char` byte arrays, runtime
  bytecode execution via `load`, environment introspection
  (`getfenv`/`_ENV`), and unpacking of further payloads.
- `Launch.cmd` is the entry point that runs the obfuscated Lua.

This pattern matches **malware dropper / loader behavior** (download a
plausibly-named executable, run it via a launcher, and let the obfuscated
script do the rest). Combined with the upstream README's heavy-handed
"download this zip from a raw GitHub URL" phrasing — which is a known
technique used to bypass user caution about running executables — these
artifacts look like a trojan-delivery mechanism and have no legitimate
place in a public source-code repository.

**Action taken:** purged with `git-filter-repo --invert-paths --path outscorn/`.
The zips are not present in any commit on this fork's public branch.

### `_tmp/` and `_final_test.txt` — REMOVED

Local test artifacts (JSON responses, a sandbox test file, and a single
test-results text file) that were accidentally tracked in upstream commit
`3a74cce`. They have no value in the source tree.

**Action taken:** purged with
`git-filter-repo --invert-paths --path _tmp/ --path _final_test.txt`.

### `/smb/takeout` mount path — NORMALIZED

The previous `docker-compose.yml` mounted `/smb/takeout` from the
author's NAS as the host-side downloads directory. That path leaks
information about the author's personal storage layout and is not
appropriate for a generic template.

**Action taken:** rewritten to `/path/to/downloads` throughout history
with `git-filter-repo --replace-text`.

### Secrets / credentials audit

Before publication, the entire git history was scanned for:

- Cloud-provider API keys (AWS `AKIA…`, GCP `AIza…`, Azure, etc.)
- Source-control tokens (GitHub `ghp_…`, `github_pat_…`)
- LLM provider keys (`sk-…`, OpenAI, Anthropic, etc.)
- Private keys (`-----BEGIN … PRIVATE KEY-----`)
- Long Google session cookies (`SID=…`, `HSID=…`)
- Personal email addresses in code
- Hostnames / IPs outside `localhost` / `127.0.0.1`

**Result: no real secrets found.** The only "secrets" present were
generic env-var-driven config knobs (`AUTH_USER`, `AUTH_PASS`,
`SECRET_KEY`, `ARIA2C_RPC_SECRET`) and a `changeme` default password in
`docker-compose.yml` and the helper extension's default options — all of
which are obvious placeholders, not real credentials.

---

## Re-publishing upstream

If at any point the original upstream adds a proper open-source license
and wishes to pull these changes back in, the GLWTS Public License
imposed by this fork imposes no restrictions on reuse of these
modifications — under its own terms, anyone (including the upstream
maintainers) can copy, modify, and redistribute these changes at their
own risk. See `LICENSE`.
