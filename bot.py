import os, asyncio, tempfile, shutil, secrets, json
from pathlib import Path
from datetime import datetime, timezone

import aiohttp, discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands

MAX = 9 * 1024 * 1024
TIMEOUT = 180
VIDEO = {".mp4",".mov",".webm",".mkv",".avi",".m4v"}
IMAGE = {".jpg",".jpeg",".png",".webp",".bmp",".gif"}
LOCK = asyncio.Semaphore(1)

GUILD_ID = 1551941310682767410
STATS = {"total": 0, "active": 0, "last": "Idle"}
LOGS = []
SESSIONS = set()
MAX_LOGS = 300


def log_event(kind, message):
    entry = {
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "kind": kind,
        "message": message,
    }
    LOGS.insert(0, entry)
    del LOGS[MAX_LOGS:]
    print(f"[{entry['time']}] [{kind}] {message}", flush=True)


bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}", flush=True)
    log_event("SYSTEM", f"Bot online as {bot.user}")
    try:
        x = await bot.tree.sync()
        g = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=g)
        gx = await bot.tree.sync(guild=g)
        print(
            f"Synced {len(x)} global and {len(gx)} server command(s)",
            flush=True
        )
        log_event("SYSTEM", f"Synced {len(x)} global and {len(gx)} server commands")
    except Exception as e:
        log_event("ERROR", f"Command sync failed: {e}")


async def status(i, text):
    try:
        await i.edit_original_response(content=text)
    except Exception:
        pass


async def download(url, path):
    t = aiohttp.ClientTimeout(total=TIMEOUT)
    async with aiohttp.ClientSession(timeout=t) as s, s.get(url) as r:
        if r.status != 200:
            raise RuntimeError(f"Download failed (HTTP {r.status})")
        with open(path, "wb") as f:
            async for chunk in r.content.iter_chunked(1024 * 1024):
                f.write(chunk)


async def ffmpeg(src, dst, width, fps, seconds, image=False):
    prep = f"scale=w='min({width},iw)':h=-2:flags=lanczos"
    if not image:
        prep = f"fps={fps}," + prep
    filt = f"{prep},split[a][b];[a]palettegen=max_colors=256:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=5[out]"
    cmd = [
        "ffmpeg","-y","-threads","1","-filter_threads","1",
        "-filter_complex_threads","1","-i",src,
        "-an","-sn","-filter_complex",filt,"-map","[out]",
        "-loop","0"
    ]
    if image:
        cmd += ["-frames:v","1"]
    else:
        cmd += ["-t",str(seconds)]
    cmd += [dst]

    p = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    try:
        _, err = await asyncio.wait_for(p.communicate(), TIMEOUT)
    except asyncio.TimeoutError:
        p.kill()
        await p.communicate()
        raise RuntimeError("Conversion timed out. Try a shorter video.")
    if p.returncode:
        raise RuntimeError(err.decode(errors="ignore")[-1200:])
    if not os.path.exists(dst):
        raise RuntimeError("FFmpeg did not create the GIF.")


async def convert(a, update):
    ext = Path(a.filename).suffix.lower()
    if ext not in VIDEO and ext not in IMAGE:
        raise RuntimeError("Please use an image or video.")

    d = tempfile.mkdtemp(prefix="gif_")
    src, dst = os.path.join(d, a.filename), os.path.join(d, "converted.gif")
    try:
        await update("📥 Downloading your file...")
        await download(a.url, src)

        settings = (
            [(1200,1,1),(1024,1,1),(896,1,1),(768,1,1),(640,1,1)]
            if ext in IMAGE
            else [(480,10,6),(400,8,6),(320,8,6)]
        )

        for n, (w, fps, sec) in enumerate(settings):
            if n:
                await update(f"⚙️ Compressing GIF — {w}px at {fps} FPS...")
            else:
                await update(f"⚙️ Converting to GIF — {w}px at {fps} FPS...")
            if os.path.exists(dst):
                os.remove(dst)
            await ffmpeg(src, dst, w, fps, sec, ext in IMAGE)
            if os.path.getsize(dst) <= MAX:
                return dst, d

        raise RuntimeError("GIF is still over Discord's 9 MB limit.")
    except Exception:
        shutil.rmtree(d, ignore_errors=True)
        raise


async def do_convert(i, a):
    await i.response.defer()
    STATS["active"] += 1
    STATS["last"] = f"Converting {a.filename}"
    log_event("CONVERSION", f"Started {a.filename} (user: {i.user}, id: {i.user.id})")

    if LOCK.locked():
        await status(i, "⏳ Another GIF is being converted. Waiting...")

    async with LOCK:
        await status(i, "📥 Starting GIF conversion...")
        try:
            path, d = await convert(a, lambda x: status(i, x))
            try:
                await status(i, "📤 Uploading your GIF...")
                await i.followup.send(
                    "✅ Done!",
                    file=discord.File(path, filename="converted.gif")
                )
                await status(i, "✅ Done!")
                STATS["total"] += 1
                STATS["last"] = f"Finished {a.filename}"
                log_event("CONVERSION", f"Finished {a.filename} (user: {i.user})")
            finally:
                shutil.rmtree(d, ignore_errors=True)
        except Exception as e:
            STATS["last"] = f"Error: {str(e)[:120]}"
            log_event("ERROR", f"Conversion failed for {a.filename}: {e}")
            await status(i, f"❌ {str(e)[:1500]}")
        finally:
            STATS["active"] -= 1


@bot.tree.command(name="gif", description="Convert an image or video to a GIF.")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.describe(file="Image or video to convert.")
async def gif(i: discord.Interaction, file: discord.Attachment):
    await do_convert(i, file)


@bot.tree.context_menu(name="Convert to GIF")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def message_gif(i: discord.Interaction, message: discord.Message):
    a = next(
        (x for x in message.attachments
         if Path(x.filename).suffix.lower() in VIDEO | IMAGE),
        None
    )
    if not a:
        await i.response.send_message("❌ No supported image/video in that message.")
        return
    await do_convert(i, a)


def ram():
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return line.split()[1] + " KB"
    except Exception:
        pass
    return "unknown"


def admin_password():
    return os.getenv("ADMIN_KEY", "").strip()


def session_ok(request):
    token = request.cookies.get("admin_session")
    return bool(token and token in SESSIONS)


def json_response(data, status=200):
    return web.Response(
        status=status,
        text=json.dumps(data),
        content_type="application/json",
    )


async def health(request):
    return web.Response(text="GIF Converter is online!")


async def admin_login(request):
    if request.method == "GET":
        return web.Response(content_type="text/html", text=LOGIN_HTML)

    try:
        data = await request.json()
    except Exception:
        data = {}

    password = str(data.get("password", ""))
    configured = admin_password()

    if not configured:
        log_event("ERROR", "ADMIN_KEY is missing from Render environment variables")
        return json_response({
            "ok": False,
            "error": "ADMIN_KEY is not configured on the server. Add it in Render Environment Variables and redeploy."
        }, 500)

    if not secrets.compare_digest(password, configured):
        log_event("AUTH", "Failed admin login attempt")
        return json_response({"ok": False, "error": "Incorrect password."}, 401)

    token = secrets.token_urlsafe(32)
    SESSIONS.add(token)
    log_event("AUTH", "Admin logged in")

    response = json_response({"ok": True})
    response.set_cookie(
        "admin_session",
        token,
        httponly=True,
        secure=True,
        samesite="Strict",
        max_age=60 * 60 * 12,
        path="/",
    )
    return response


async def admin_logout(request):
    token = request.cookies.get("admin_session")
    SESSIONS.discard(token)
    response = json_response({"ok": True})
    response.del_cookie("admin_session", path="/")
    log_event("AUTH", "Admin logged out")
    return response


async def require_admin(request):
    if not session_ok(request):
        return json_response({"ok": False, "error": "Not logged in."}, 401)
    return None


async def admin_page(request):
    if not session_ok(request):
        return web.Response(content_type="text/html", text=LOGIN_HTML)

    return web.Response(content_type="text/html", text=ADMIN_HTML)


async def api_stats(request):
    auth = await require_admin(request)
    if auth:
        return auth

    return json_response({
        "ok": True,
        "online": not bot.is_closed(),
        "bot": str(bot.user) if bot.user else "Starting...",
        "ram": ram(),
        "active": STATS["active"],
        "total": STATS["total"],
        "last": STATS["last"],
        "log_count": len(LOGS),
    })


async def api_logs(request):
    auth = await require_admin(request)
    if auth:
        return auth
    return json_response({"ok": True, "logs": LOGS[:100]})


async def api_channels(request):
    auth = await require_admin(request)
    if auth:
        return auth

    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return json_response({"ok": False, "error": "The bot is not connected to the configured server."}, 503)

    channels = []
    for channel in guild.text_channels:
        permissions = channel.permissions_for(guild.me) if guild.me else None
        if permissions and permissions.send_messages:
            channels.append({
                "id": str(channel.id),
                "name": f"#{channel.name}",
                "category": channel.category.name if channel.category else "No category",
            })

    return json_response({"ok": True, "channels": channels})


async def api_send(request):
    auth = await require_admin(request)
    if auth:
        return auth

    try:
        data = await request.json()
    except Exception:
        return json_response({"ok": False, "error": "Invalid request."}, 400)

    channel_id = str(data.get("channel_id", "")).strip()
    message = str(data.get("message", "")).strip()

    if not channel_id or not message:
        return json_response({"ok": False, "error": "Choose a channel and enter a message."}, 400)

    if len(message) > 2000:
        return json_response({"ok": False, "error": "Discord messages are limited to 2000 characters."}, 400)

    try:
        channel = await bot.fetch_channel(int(channel_id))
        if not hasattr(channel, "send"):
            raise RuntimeError("That channel cannot receive messages.")

        sent = await channel.send(message)
        location = f"{channel.guild.name} / #{getattr(channel, 'name', channel.id)}"
        log_event("MESSAGE", f"Bot sent a message to {location} (message ID: {sent.id})")
        return json_response({
            "ok": True,
            "location": location,
            "message_id": str(sent.id),
        })
    except ValueError:
        return json_response({"ok": False, "error": "Invalid channel ID."}, 400)
    except discord.Forbidden:
        log_event("ERROR", f"Bot was denied permission to send in channel {channel_id}")
        return json_response({"ok": False, "error": "Discord denied permission to send in that channel."}, 403)
    except Exception as e:
        log_event("ERROR", f"Failed to send admin message to {channel_id}: {e}")
        return json_response({"ok": False, "error": str(e)[:500]}, 500)


LOGIN_HTML = r"""
<!doctype html>
<html>
<head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>GIF Converter — Admin Login</title>
<style>
*{box-sizing:border-box}
body{margin:0;min-height:100vh;font-family:Inter,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:radial-gradient(circle at 20% 10%,#26356b 0,#11162d 35%,#080b14 75%);color:#f7f8ff;display:grid;place-items:center;padding:24px}
.box{width:min(430px,100%);background:rgba(19,24,42,.88);border:1px solid rgba(255,255,255,.1);border-radius:24px;padding:34px;box-shadow:0 24px 80px rgba(0,0,0,.45);backdrop-filter:blur(18px)}
.logo{width:58px;height:58px;border-radius:17px;display:grid;place-items:center;background:linear-gradient(135deg,#7c5cff,#29d8ff);font-size:28px;margin-bottom:22px}
h1{font-size:28px;margin:0 0 8px}.sub{color:#aeb6d0;margin:0 0 26px}
label{display:block;color:#cfd5e8;font-size:14px;margin-bottom:8px}
input{width:100%;padding:14px 15px;border-radius:12px;border:1px solid #39415e;background:#0e1322;color:white;font-size:16px;outline:none}
input:focus{border-color:#7c5cff;box-shadow:0 0 0 3px rgba(124,92,255,.16)}
button{width:100%;margin-top:16px;padding:14px;border:0;border-radius:12px;background:linear-gradient(135deg,#7c5cff,#4c9cff);color:white;font-size:16px;font-weight:700;cursor:pointer}
button:disabled{opacity:.6;cursor:wait}
.error{display:none;margin-top:14px;padding:12px;border-radius:10px;background:#3b1820;color:#ffb7c2;border:1px solid #6e2635}
.hint{font-size:12px;color:#7f89a7;margin-top:18px;line-height:1.5}
</style>
</head>
<body>
<div class="box">
<div class="logo">🎞️</div>
<h1>GIF Converter</h1>
<p class="sub">Secure admin dashboard</p>
<form id="login">
<label for="password">Admin password</label>
<input id="password" type="password" autocomplete="current-password" placeholder="Enter your admin password" required>
<button id="btn">Sign in</button>
<div id="error" class="error"></div>
</form>
<div class="hint">Your password is sent only to this bot over HTTPS. The dashboard uses a secure login session instead of putting the password in the URL.</div>
</div>
<script>
const form=document.getElementById('login'),btn=document.getElementById('btn'),err=document.getElementById('error');
form.addEventListener('submit',async e=>{
 e.preventDefault(); err.style.display='none'; btn.disabled=true; btn.textContent='Signing in…';
 try{
  const r=await fetch('/admin/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:document.getElementById('password').value})});
  const d=await r.json();
  if(!r.ok) throw new Error(d.error||'Login failed');
  location.href='/admin';
 }catch(x){err.textContent=x.message;err.style.display='block';}
 finally{btn.disabled=false;btn.textContent='Sign in';}
});
</script>
</body>
</html>
"""


ADMIN_HTML = r"""
<!doctype html>
<html>
<head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>GIF Converter — Admin</title>
<style>
*{box-sizing:border-box}
body{margin:0;font-family:Inter,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#080b14;color:#f7f8ff}
.top{position:sticky;top:0;z-index:5;background:rgba(8,11,20,.88);backdrop-filter:blur(16px);border-bottom:1px solid #20263a;padding:18px 28px;display:flex;justify-content:space-between;align-items:center}
.brand{display:flex;align-items:center;gap:12px;font-weight:800;font-size:19px}.brand span{display:grid;place-items:center;width:38px;height:38px;border-radius:11px;background:linear-gradient(135deg,#7c5cff,#29d8ff)}
.logout{background:#171d2d;color:#cbd3e8;border:1px solid #2b334a;border-radius:10px;padding:9px 14px;cursor:pointer}
main{max-width:1200px;margin:0 auto;padding:30px 22px 60px}
h1{font-size:34px;margin:0 0 8px}.lead{color:#8994b1;margin:0 0 28px}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}.card{background:#111727;border:1px solid #20283d;border-radius:17px;padding:20px}.label{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:#77839f}.value{font-size:25px;font-weight:800;margin-top:8px}.online{color:#54e39b}
.layout{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:16px}
h2{font-size:18px;margin:0 0 16px}.panel{background:#111727;border:1px solid #20283d;border-radius:17px;padding:20px}
textarea,select{width:100%;background:#0b1020;color:#f7f8ff;border:1px solid #2c3550;border-radius:11px;padding:12px;font:inherit;outline:none}
textarea{min-height:150px;resize:vertical}select{margin-bottom:12px}
.send{margin-top:12px;border:0;border-radius:11px;padding:12px 18px;background:linear-gradient(135deg,#7c5cff,#4c9cff);color:#fff;font-weight:800;cursor:pointer}
#sendStatus{min-height:20px;margin-top:10px;color:#9da8c4;font-size:14px}
.logs{height:430px;overflow:auto;background:#0b1020;border-radius:12px;border:1px solid #222b42}
.log{padding:12px 14px;border-bottom:1px solid #1c2437}.log:last-child{border:0}.time{color:#6f7c99;font-size:11px}.kind{display:inline-block;margin:4px 7px 0 0;padding:3px 7px;border-radius:6px;background:#202943;color:#aebaff;font-size:10px;font-weight:800}.msg{margin-top:5px;color:#d9deeb;font-size:13px;word-break:break-word}
.refresh{float:right;border:1px solid #2b334a;background:#171d2d;color:#dce2f0;border-radius:9px;padding:7px 10px;cursor:pointer}
@media(max-width:850px){.grid{grid-template-columns:1fr 1fr}.layout{grid-template-columns:1fr}}
@media(max-width:500px){.grid{grid-template-columns:1fr}.top{padding:15px}.top .brand b{display:none}main{padding:22px 14px}}
</style>
</head>
<body>
<header class="top"><div class="brand"><span>🎞️</span><b>GIF Converter Admin</b></div><button class="logout" onclick="logout()">Log out</button></header>
<main>
<h1>Dashboard</h1>
<p class="lead">Monitor the bot, send messages, and inspect recent activity.</p>
<section class="grid">
<div class="card"><div class="label">Status</div><div id="online" class="value online">Loading…</div></div>
<div class="card"><div class="label">RAM</div><div id="ram" class="value">—</div></div>
<div class="card"><div class="label">Active conversions</div><div id="active" class="value">—</div></div>
<div class="card"><div class="label">Completed</div><div id="total" class="value">—</div></div>
</section>
<section class="layout">
<div class="panel">
<h2>Send as the bot</h2>
<select id="channel"><option>Loading channels…</option></select>
<textarea id="message" maxlength="2000" placeholder="Type the message you want the bot to send…"></textarea>
<button class="send" onclick="sendMessage()">Send message</button>
<div id="sendStatus"></div>
</div>
<div class="panel">
<h2>Activity logs <button class="refresh" onclick="loadAll()">Refresh</button></h2>
<div id="logs" class="logs"><div class="log">Loading…</div></div>
</div>
</section>
</main>
<script>
async function api(url,opts={}){
 const r=await fetch(url,opts);
 if(r.status===401){location.href='/admin';throw new Error('Session expired');}
 const d=await r.json(); if(!r.ok) throw new Error(d.error||'Request failed'); return d;
}
async function loadStats(){
 try{const d=await api('/api/admin/stats');document.getElementById('online').textContent=d.online?'Online':'Offline';document.getElementById('online').className='value '+(d.online?'online':'');document.getElementById('ram').textContent=d.ram;document.getElementById('active').textContent=d.active;document.getElementById('total').textContent=d.total;}catch(e){}
}
async function loadChannels(){
 try{const d=await api('/api/admin/channels');const s=document.getElementById('channel');s.innerHTML='';d.channels.forEach(c=>{const o=document.createElement('option');o.value=c.id;o.textContent=c.name+' — '+c.category;s.appendChild(o)});if(!d.channels.length)s.innerHTML='<option>No channels available</option>';}catch(e){document.getElementById('channel').innerHTML='<option>'+e.message+'</option>';}
}
async function loadLogs(){
 try{const d=await api('/api/admin/logs');const box=document.getElementById('logs');box.innerHTML=d.logs.length?d.logs.map(x=>'<div class="log"><div class="time">'+escapeHtml(x.time)+'</div><span class="kind">'+escapeHtml(x.kind)+'</span><div class="msg">'+escapeHtml(x.message)+'</div></div>').join(''):'<div class="log">No activity yet.</div>';}catch(e){}
}
function escapeHtml(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
async function sendMessage(){
 const channel=document.getElementById('channel').value,msg=document.getElementById('message').value.trim(),st=document.getElementById('sendStatus');
 if(!channel||!msg){st.textContent='Choose a channel and type a message.';return}
 st.textContent='Sending…';
 try{const d=await api('/api/admin/send',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({channel_id:channel,message:msg})});st.textContent='✓ Sent to '+d.location;document.getElementById('message').value='';await loadLogs();}catch(e){st.textContent='✕ '+e.message;}
}
async function logout(){await fetch('/admin/logout',{method:'POST'});location.href='/admin';}
async function loadAll(){await Promise.all([loadStats(),loadChannels(),loadLogs()]);}
loadAll();setInterval(()=>{loadStats();loadLogs()},5000);
</script>
</body>
</html>
"""


async def main():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_get("/admin", admin_page)
    app.router.add_get("/admin/login", admin_login)
    app.router.add_post("/admin/login", admin_login)
    app.router.add_post("/admin/logout", admin_logout)
    app.router.add_get("/api/admin/stats", api_stats)
    app.router.add_get("/api/admin/logs", api_logs)
    app.router.add_get("/api/admin/channels", api_channels)
    app.router.add_post("/api/admin/send", api_send)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(
        runner, "0.0.0.0", int(os.getenv("PORT", "10000"))
    ).start()

    log_event("SYSTEM", "Web admin server started")
    await bot.start(os.environ["DISCORD_TOKEN"])


asyncio.run(main())
