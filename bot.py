import os, asyncio, tempfile, shutil
from pathlib import Path

import aiohttp, discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands

MAX = 9 * 1024 * 1024
TIMEOUT = 180
VIDEO = {".mp4",".mov",".webm",".mkv",".avi",".m4v"}
IMAGE = {".jpg",".jpeg",".png",".webp",".bmp",".gif"}
LOCK = asyncio.Semaphore(1)


bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}", flush=True)
    try:
        x = await bot.tree.sync()
        print(f"Synced {len(x)} command(s)", flush=True)
    except Exception as e:
        print(f"Sync error: {e}", flush=True)


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
    vf = f"fps={fps},scale=w='min({width},iw)':h=-2:flags=lanczos"
    cmd = [
        "ffmpeg","-y","-threads","1","-filter_threads","1",
        "-filter_complex_threads","1"
    ]
    if image:
        cmd += ["-loop","1"]
    cmd += ["-t",str(seconds),"-i",src,"-an","-sn","-dn","-vf",vf,"-loop","0",dst]
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

        # One conversion at a time keeps the 512 MB instance predictable.
        settings = [(720,10,1)] if ext in IMAGE else [(640,12,8),(480,10,8),(360,8,8)]
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
            finally:
                shutil.rmtree(d, ignore_errors=True)
        except Exception as e:
            await status(i, f"❌ {str(e)[:1500]}")


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


async def health(request):
    return web.Response(text="GIF Converter is online!")


async def main():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(
        runner, "0.0.0.0", int(os.getenv("PORT", "10000"))
    ).start()
    print("Web server started", flush=True)
    await bot.start(os.environ["DISCORD_TOKEN"])


asyncio.run(main())
