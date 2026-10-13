"""First-run setup before any database, scanner, or media writer is started."""
from __future__ import annotations

import json
import os
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from retrotv.processing import PRESETS
from retrotv.media_folders import ensure_library_scaffold


def existing_profile(root: Path) -> bool:
    data = root / "user-data"
    if (data / "state/setup-pending.json").exists() and not (data / "config/portable.windows.settings.json").exists():
        return False
    return any((data / name).is_file() for name in (
        "config/portable.windows.toml", "config/portable.windows.settings.json",
        "database/retrotv.sqlite3", "state/setup-completed.json"))


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class SetupService:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.completed = False
        self.cancelled = False
        self.lock = threading.Lock()

    def validate(self, value):
        if not isinstance(value, dict):
            raise ValueError("Setup values must be an object")
        media = Path(os.path.expandvars(os.path.expanduser(str(value.get("media", ""))))).resolve()
        if not str(value.get("media", "")).strip() or not media.is_dir():
            raise ValueError("Choose an existing, accessible media folder")
        try:
            next(iter(media.iterdir()), None)
        except OSError as exc:
            raise ValueError("The media folder cannot be read") from exc
        timezone = str(value.get("timezone", "UTC"))
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Choose a valid timezone, such as America/Los_Angeles") from exc
        for name in ("video", "audio", "tails", "fullscreen", "weather", "overnight"):
            if not isinstance(value.get(name, False), bool):
                raise ValueError(f"{name} must be true or false")
        display = value.get("display", "modern")
        aspect = value.get("aspect", "preserve")
        policy = value.get("policy", "replace_with_backup")
        if display not in {"crt", "modern"} or aspect not in {"preserve", "retro_4_3"} or policy not in {"proxy", "replace_with_backup"}:
            raise ValueError("Choose valid display, aspect, and file-handling options")
        preset = value.get("resolution", "1080p")
        if preset not in PRESETS:
            raise ValueError("Choose a valid resolution preset; Custom is available later in Settings")
        source_edits = bool(value.get("audio") or value.get("tails") or (value.get("video") and policy == "replace_with_backup"))
        if source_edits and not os.access(media, os.W_OK):
            raise ValueError("Source-changing features require a writable library. Disable them or use video proxy copies.")
        backup_raw = str(value.get("backup", "")).strip()
        backup = Path(os.path.expandvars(os.path.expanduser(backup_raw))).resolve() if backup_raw else self.root / "user-data" / "originals-backup"
        if backup == media or backup.is_relative_to(media):
            raise ValueError("Choose an originals backup folder outside the indexed media library")
        if source_edits:
            ancestor = backup
            while not ancestor.exists() and ancestor != ancestor.parent:
                ancestor = ancestor.parent
            if not ancestor.is_dir() or not os.access(ancestor, os.W_OK):
                raise ValueError("The originals backup location is not writable")
        return {**value, "media": str(media), "backup": str(backup), "timezone": timezone,
                "resolution": preset, "display": display, "aspect": aspect, "policy": policy}

    def save(self, value):
        values = self.validate(value)
        from retrotv.runtime_terms import accepted, acknowledge
        if not accepted(self.root) and values.get("runtime_terms") is not True:
            raise ValueError("Accept the bundled Microsoft runtime terms to continue")
        with self.lock:
            if self.completed:
                raise ValueError("Setup has already completed")
            settings = self.root / "user-data/config/portable.windows.settings.json"
            if settings.exists():
                raise ValueError("A profile already exists; restart CRTV Sim to use it")
            width, height = PRESETS[values["resolution"]]
            overrides = {
                "application": {"timezone": values["timezone"]},
                "scheduling": {"timezone": values["timezone"]},
                "storage_roots": {"media": values["media"], "originals_backup": values["backup"]},
                "normalization": {"enabled": values.get("video", False), "aspect_policy": values["aspect"],
                                  "canvas_width": width, "canvas_height": height, "allow_upscale": False,
                                  "output_policy": values["policy"]},
                "audio_normalization": {"enabled": values.get("audio", False)},
                "commercial_tails": {"enabled": values.get("tails", False)},
                "player": {"fullscreen": values.get("fullscreen", False), "osd_font_size": 60 if values["display"] == "crt" else 40},
                "guide": {"text_size_percent": 120 if values["display"] == "crt" else 100},
                "application_layer": {"start_tv_on_launch": False, "startup_channel": "__guide__"},
                "maintenance_window": {"enabled": values.get("overnight", False), "start": "03:00", "end": "05:00"},
                "synthetic_channels": {"weather": {"enabled": values.get("weather", False)}},
                "web": {"host": "127.0.0.1"},
            }
            # Setup creates only empty drag-and-drop directories. It does not
            # scan, classify, or modify any files in the selected library.
            ensure_library_scaffold(Path(values["media"]))
            # Saving settings is the commit point. A restart after interruption
            # uses them, and no media processing starts during setup.
            atomic_json(settings, overrides)
            atomic_json(self.root / "user-data/state/setup-completed.json", {"schema_version": 1})
            acknowledge(self.root)
            self.completed = True
        return {"ok": True, "message": "Setup saved. CRTV Sim will now open the Dashboard."}


SETUP_HTML = r'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Welcome to CRTV Sim</title><style>
body{background:#111821;color:#edf4ff;font:16px system-ui;margin:0}main{max-width:820px;margin:30px auto;padding:24px}
fieldset{border:1px solid #465565;border-radius:8px;margin:20px 0;padding:20px}legend{font-weight:bold}label{display:block;margin:12px 0}
input:not([type=checkbox]),select{box-sizing:border-box;width:100%;padding:10px;background:#0b131c;color:inherit;border:1px solid #57687a;border-radius:4px}
button{padding:12px;margin:6px;background:#235e50;color:white;border:1px solid #50786c;border-radius:5px;cursor:pointer}small,p{line-height:1.5;color:#bcd0dd}#error{color:#ffad9a;white-space:pre-wrap}pre{white-space:pre-wrap}
</style><main><h1>Welcome to CRTV Sim</h1><p>Choose your media library and how CRTV Sim should handle it. Nothing is scanned or processed until you finish setup.</p>
<form id="setup"><fieldset><legend>Library and local time</legend>
<label>Media folder <input id="media" required></label><button type="button" id="browse">Browse folders</button><div id="folders" hidden></div>
<p>The ZIP includes Media/shared and Media/_channel_template. Copy _channel_template, rename the copy to your channel, and drop media into its daypart folders. You can also select an existing library anywhere accessible.</p>
<label>Timezone <input id="timezone" required></label></fieldset>
<fieldset><legend>Watching preferences</legend><label>Display <select id="display"><option value="modern">Modern display</option><option value="crt">CRT / retro 4:3</option></select></label>
<label><input id="fullscreen" type="checkbox"> Start the player fullscreen</label><p>Display preferences change presentation and text readability. They do not change your source videos. Guide and station graphics currently use the retro SD presentation.</p></fieldset>
<fieldset><legend>Optional media processing</legend><label><input id="video" type="checkbox"> Enable video normalization</label>
<label>Maximum video resolution <select id="resolution"><option value="480p">Retro SD / 480p — 640 × 480</option><option value="720p">720p — 1280 × 720</option><option selected value="1080p">1080p — 1920 × 1080</option><option value="4K">4K — 3840 × 2160</option></select></label>
<p>Larger videos are downscaled to fit. Smaller videos are not upscaled. Custom dimensions and the optional upscale control are available later in Settings.</p>
<label>Aspect correction <select id="aspect" aria-describedby="aspect-help"><option value="preserve">Preserve source frame and aspect</option><option value="retro_4_3">Correct detected retro 4:3 formatting</option></select></label>
<p id="aspect-help">Preserve keeps the existing picture shape and embedded black bars. Correct automatically fixes detected retro 4:3 formatting issues when confident and sends uncertain results for review. Manual review choices override this default; correction requires video normalization to be enabled.</p>
<label>Video file handling <select id="policy"><option value="replace_with_backup">Replace working video; keep a verified original backup</option><option value="proxy">Keep source video in place; play a separate optimized copy</option></select></label>
<label><input id="audio" type="checkbox"> Enable audio loudness normalization</label><p>This makes perceived volume more consistent. It replaces working files and keeps verified pre-audio backups, even when video copies are selected. Loudness targets are editable later.</p>
<label><input id="tails" type="checkbox"> Automatically trim commercial black tails</label><p>Trimming rewrites commercial files and keeps a recovery backup. Only confident black-tail detections are trimmed; ambiguous endings, including text, are retained.</p>
<label>Originals backup folder <input id="backup"></label><p>Verified originals use extra storage. The default backup retention is 30 days. Settings lets you change the location, retention, and deletion policy. Raising the resolution limit later cannot recover detail already removed; that requires an original or retained backup.</p></fieldset>
<fieldset><legend>Optional services and maintenance</legend><label><input id="overnight" type="checkbox"> Use an overnight maintenance window (3 AM–5 AM)</label><p>Otherwise background readiness work runs when TV is stopped. Processing can use CPU and disk bandwidth.</p>
<label><input id="weather" type="checkbox"> Enable the Weather channel</label><p>If enabled, you must finish RetroWeather setup: choose your location and fetch weather + create the channel. The Dashboard will guide you; Weather cannot be tuned until its first playable video is ready. Weather uses Open-Meteo. It is optional.</p>
<p>AI graphics and announcers are optional and may incur provider charges. Add OpenAI or Breeze Blue credentials later in Settings. TV works without them. The Dashboard is local to this computer by default.</p></fieldset>
<fieldset id="runtime-license" hidden><legend>Bundled Microsoft runtime</legend><p>These terms apply only to Microsoft's Visual C++ runtime. CRTV Sim and the other bundled components retain their own licenses.</p><label><input id="runtime_terms" type="checkbox"> I agree to the <a href="/setup/runtime-terms" target="_blank">Microsoft runtime terms</a>.</label></fieldset>
<button type="submit">Review choices</button><button type="button" id="cancel">Exit without starting</button></form>
<section id="review" hidden><h2>Ready to start?</h2><pre id="summary"></pre><button id="finish">Save and start CRTV Sim</button><button id="back">Change choices</button></section><p id="error" role="status"></p></main>
<script>
const $=id=>document.getElementById(id);const token='__TOKEN__';let values;
async function api(path,value){const r=await fetch(path,{method:value?'POST':'GET',headers:{'Content-Type':'application/json','X-Setup-Token':token},body:value?JSON.stringify(value):undefined});const d=await r.json();if(!r.ok)throw Error(d.error||'Request failed');return d;}
function collect(){const d={};for(const id of ['media','timezone','resolution','aspect','policy','backup','display'])d[id]=$(id).value;for(const id of ['video','audio','tails','fullscreen','weather','overnight','runtime_terms'])d[id]=$(id).checked;return d;}
$('timezone').value=Intl.DateTimeFormat().resolvedOptions().timeZone||'UTC';
api('/api/setup/defaults').then(d=>{$('media').value=d.media;$('backup').value=d.backup;$('runtime-license').hidden=!d.runtime_terms_required;$('runtime_terms').required=d.runtime_terms_required;}).catch(e=>$('error').textContent=e.message);
async function folders(path){const d=await api('/api/setup/folders?path='+encodeURIComponent(path));const box=$('folders');box.hidden=false;box.replaceChildren();for(const item of d.folders){const b=document.createElement('button');b.type='button';b.textContent=item;b.onclick=()=>{ $('media').value=item;folders(item).catch(e=>$('error').textContent=e.message);};box.append(b);}if(d.parent){const b=document.createElement('button');b.type='button';b.textContent='Parent folder';b.onclick=()=>folders(d.parent);box.append(b);}const done=document.createElement('button');done.type='button';done.textContent='Use selected folder';done.onclick=()=>box.hidden=true;box.append(done);}
$('browse').onclick=()=>folders($('media').value).catch(e=>$('error').textContent=e.message);
$('setup').onsubmit=async e=>{e.preventDefault();try{values=await api('/api/setup/preview',collect());$('summary').textContent=`Media: ${values.media}\nTimezone: ${values.timezone}\nDisplay: ${values.display}\nVideo normalization: ${values.video?'on':'off'} (${values.resolution}, ${values.aspect}, ${values.policy})\nAudio normalization: ${values.audio?'on — modifies working files':'off'}\nCommercial trimming: ${values.tails?'on — modifies commercials':'off'}\nBackups: ${values.backup}\nWeather: ${values.weather?'on; RetroWeather setup required before tuning':'off'}\nMaintenance: ${values.overnight?'3 AM–5 AM':'when TV is stopped'}`;$('setup').hidden=true;$('review').hidden=false;$('error').textContent='';}catch(e){$('error').textContent=e.message;}};
$('back').onclick=()=>{$('setup').hidden=false;$('review').hidden=true;};
$('finish').onclick=async()=>{ $('finish').disabled=true;try{const d=await api('/api/setup/complete',values);$('error').textContent=d.message;}catch(e){$('error').textContent=e.message;$('finish').disabled=false;}};
$('cancel').onclick=()=>api('/api/setup/cancel',{}).then(()=>$('error').textContent='Setup cancelled. No media was processed.');
</script></html>'''


def serve_setup(root, *, open_browser=True):
    service = SetupService(root)
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def reply(self, value, status=200, html=False):
            data = value.encode() if html else json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8" if html else "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            from urllib.parse import urlparse, parse_qs
            url = urlparse(self.path)
            if url.path in {"/", "/setup"}:
                self.reply(SETUP_HTML.replace("__TOKEN__", token), html=True)
                return
            if url.path == "/setup/runtime-terms":
                import html
                from retrotv.runtime_terms import terms
                self.reply("<meta charset='utf-8'><pre style='white-space:pre-wrap'>" + html.escape(terms(Path(root)) or "No bundled Microsoft runtime terms.") + "</pre>", html=True)
                return
            if self.headers.get("X-Setup-Token") != token:
                self.reply({"error": "Setup token required"}, 403)
                return
            if url.path == "/api/setup/defaults":
                candidates = (Path(root).parent / "Media", Path(root) / "Media")
                media = next((p for p in candidates if p.is_dir()), candidates[0])
                from retrotv.runtime_terms import accepted
                self.reply({"media": str(media.resolve()), "backup": str(Path(root) / "user-data/originals-backup"), "runtime_terms_required": not accepted(Path(root))})
            elif url.path == "/api/setup/folders":
                path = Path(parse_qs(url.query).get("path", [""])[0])
                try:
                    self.reply({"folders": sorted(str(p) for p in path.iterdir() if p.is_dir()),
                                "parent": str(path.parent) if path != path.parent else None})
                except OSError:
                    self.reply({"error": "Folder cannot be read. Enter another path or network drive."}, 400)
            else:
                self.reply({"error": "Not found"}, 404)

        def do_POST(self):
            if self.headers.get("X-Setup-Token") != token:
                self.reply({"error": "Setup token required"}, 403)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 16384:
                    raise ValueError("Invalid setup request size")
                value = json.loads(self.rfile.read(size))
                if self.path == "/api/setup/preview":
                    self.reply(service.validate(value))
                elif self.path == "/api/setup/complete":
                    self.reply(service.save(value))
                    threading.Thread(target=server.shutdown, daemon=True).start()
                elif self.path == "/api/setup/cancel":
                    service.cancelled = True
                    self.reply({"ok": True})
                    threading.Thread(target=server.shutdown, daemon=True).start()
                else:
                    self.reply({"error": "Not found"}, 404)
            except (ValueError, OSError) as exc:
                self.reply({"error": str(exc)}, 400)

        def log_message(self, *_args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 8765), Handler) as server:
        if open_browser:
            webbrowser.open("http://127.0.0.1:8765/setup")
        server.serve_forever()
    return service.completed
