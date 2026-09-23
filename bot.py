import os
import asyncio
import tempfile
from pathlib import Path

import aiohttp
import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv


# =========================
# SETTINGS
# =========================

MAX_GIF_SIZE = 9 * 1024 * 1024
DOWNLOAD_TIMEOUT = 300
FFMPEG_TIMEOUT = 300

VIDEO_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".webm",
    ".mkv",
    ".avi",
    ".m4v",
}

IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".bmp",
    ".gif",
}


# =========================
# LOAD TOKEN
# =========================

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN was not found")


# =========================
# BOT
# =========================

intents = discord.Intents.default()

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


# =========================
# WHEN BOT STARTS
# =========================

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}", flush=True)

    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)", flush=True)
    except Exception as e:
        print(f"Failed to sync commands: {e}", flush=True)


# =========================
# DOWNLOAD FILE
# =========================

async def download_file(url, destination):
    timeout = aiohttp.ClientTimeout(
        total=DOWNLOAD_TIMEOUT
    )

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as response:
            if response.status != 200:
                raise RuntimeError(
                    f"Could not download the file. HTTP {response.status}"
                )

            with open(destination, "wb") as file:
                while True:
                    chunk = await response.content.read(1024 * 1024)

                    if not chunk:
                        break

                    file.write(chunk)


# =========================
# RUN FFMPEG WITH TIMEOUT
# =========================

async def run_ffmpeg(command):
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=FFMPEG_TIMEOUT
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        raise RuntimeError(
            "FFmpeg took too long to finish. "
            "Try a shorter or smaller video."
        )

    if process.returncode != 0:
        error_message = stderr.decode(errors="ignore")

        raise RuntimeError(
            f"FFmpeg conversion failed:\n"
            f"{error_message[-2000:]}"
        )


# =========================
# IMAGE TO GIF
# =========================

async def convert_image_to_gif(
    input_file,
    output_file,
    width
):
    command = [
        "ffmpeg",
        "-y",
        "-loop",
        "1",
        "-i",
        input_file,
        "-t",
        "1",
        "-vf",
        (
            f"scale='min({width},iw)':-2:"
            "flags=lanczos,"
            "split[s0][s1];"
            "[s0]palettegen="
            "max_colors=256:"
            "stats_mode=diff[p];"
            "[s1][p]paletteuse="
            "dither=sierra2_4a"
        ),
        "-r",
        "15",
        "-gifflags",
        "-offsetting",
        "-loop",
        "0",
        output_file,
    ]

    print(f"Converting image at width={width}", flush=True)

    await run_ffmpeg(command)

    if not os.path.exists(output_file):
        raise RuntimeError("FFmpeg did not create the GIF.")


# =========================
# VIDEO TO GIF
# =========================

async def convert_video_to_gif(
    input_file,
    output_file,
    width,
    fps
):
    scale_filter = (
        f"fps={fps},"
        f"scale='min({width},iw)':-2:"
        "flags=lanczos,"
        "split[s0][s1];"
        "[s0]palettegen="
        "max_colors=256:"
        "stats_mode=diff[p];"
        "[s1][p]paletteuse="
        "dither=sierra2_4a"
    )

    command = [
        "ffmpeg",
        "-y",
        "-i",
        input_file,
        "-vf",
        scale_filter,
        "-loop",
        "0",
        output_file,
    ]

    print(
        f"Converting video at width={width}, fps={fps}",
        flush=True
    )

    await run_ffmpeg(command)

    if not os.path.exists(output_file):
        raise RuntimeError("FFmpeg did not create the GIF.")


# =========================
# AUTOMATIC COMPRESSION
# =========================

async def smart_convert(
    input_file,
    output_file,
    is_image,
    status_callback=None
):
    if is_image:
        settings = [
            (1200, 15),
            (1000, 15),
            (900, 15),
            (800, 15),
            (720, 15),
            (600, 15),
            (480, 12),
            (400, 10),
            (320, 8),
            (240, 6),
        ]
    else:
        settings = [
            (1080, 30),
            (1080, 24),
            (1080, 20),
            (1080, 15),
            (900, 24),
            (900, 20),
            (900, 15),
            (720, 24),
            (720, 20),
            (720, 15),
            (600, 15),
            (480, 12),
            (400, 10),
            (320, 8),
            (240, 6),
        ]

    for width, fps in settings:
        if os.path.exists(output_file):
            os.remove(output_file)

        print(
            f"Trying width={width}, fps={fps}",
            flush=True
        )

        if status_callback:
            fps_text = f" at {fps} FPS" if not is_image else ""
            await status_callback(
                f"⚙️ Converting to GIF — {width}px{fps_text}..."
            )

        if is_image:
            await convert_image_to_gif(
                input_file,
                output_file,
                width
            )
        else:
            await convert_video_to_gif(
                input_file,
                output_file,
                width,
                fps
            )

        size = os.path.getsize(output_file)
        size_mb = size / (1024 * 1024)

        print(
            f"GIF size: {size_mb:.2f} MB",
            flush=True
        )

        if size <= MAX_GIF_SIZE:
            print(
                "Automatic compression found an acceptable quality.",
                flush=True
            )
            return

    raise RuntimeError(
        "The GIF is still too large after automatic compression."
    )


# =========================
# PROCESS ATTACHMENT
# =========================

async def process_attachment(attachment, status_callback=None):
    filename = attachment.filename
    extension = Path(filename).suffix.lower()

    if (
        extension not in VIDEO_EXTENSIONS
        and extension not in IMAGE_EXTENSIONS
    ):
        raise RuntimeError(
            "That file type isn't supported. "
            "Use an image or video."
        )

    with tempfile.TemporaryDirectory() as temp_dir:
        input_file = os.path.join(temp_dir, filename)
        output_file = os.path.join(temp_dir, "converted.gif")

        print(f"Downloading: {filename}", flush=True)

        if status_callback:
            await status_callback("📥 Downloading your file...")

        await download_file(
            attachment.url,
            input_file
        )

        is_image = extension in IMAGE_EXTENSIONS

        print("Starting automatic conversion...", flush=True)

        if status_callback:
            await status_callback("⚙️ Converting to GIF...")

        await smart_convert(
            input_file,
            output_file,
            is_image,
            status_callback
        )

        print("Conversion finished.", flush=True)

        # Read the finished GIF before the temporary directory disappears.
        with open(output_file, "rb") as gif_file:
            return gif_file.read()


# =========================
# /GIF COMMAND
# =========================

@bot.tree.command(
    name="gif",
    description="Convert a video or image into a high-quality GIF."
)
@app_commands.allowed_contexts(
    guilds=True,
    dms=True,
    private_channels=True
)
@app_commands.allowed_installs(
    guilds=True,
    users=True
)
@app_commands.describe(
    file="The video or image you want to convert."
)
async def gif(
    interaction: discord.Interaction,
    file: discord.Attachment
):
    print("=== GIF COMMAND STARTED ===", flush=True)

    await interaction.response.defer()

    print("=== GIF DEFER FINISHED ===", flush=True)
    print(f"=== GIF FILE: {file.filename} ===", flush=True)

    async def update_status(message):
        try:
            await interaction.edit_original_response(
                content=message
            )
        except Exception as e:
            print(f"STATUS UPDATE ERROR: {e}", flush=True)

    try:
        await update_status("📥 Starting GIF conversion...")

        gif_data = await process_attachment(
            file,
            update_status
        )

        await update_status("📤 Uploading your finished GIF...")

        await interaction.edit_original_response(
            content="✅ Done! High-quality GIF:",
            attachments=[
                discord.File(
                    __import__("io").BytesIO(gif_data),
                    filename="converted.gif"
                )
            ]
        )

    except Exception as e:
        print(f"GIF ERROR: {e}", flush=True)

        await interaction.followup.send(
            "❌ Conversion failed:\n"
            f"`{str(e)[:1500]}`"
        )


# =========================
# RIGHT-CLICK MESSAGE → APPS → CONVERT TO GIF
# =========================

@bot.tree.context_menu(name="Convert to GIF")
@app_commands.allowed_contexts(
    guilds=True,
    dms=True,
    private_channels=True
)
@app_commands.allowed_installs(
    guilds=True,
    users=True
)
async def convert_message_to_gif(
    interaction: discord.Interaction,
    message: discord.Message
):
    print(
        "=== MESSAGE GIF COMMAND STARTED ===",
        flush=True
    )

    await interaction.response.defer()

    print(
        "=== MESSAGE GIF DEFER FINISHED ===",
        flush=True
    )

    attachment = None

    for item in message.attachments:
        extension = Path(item.filename).suffix.lower()

        if (
            extension in VIDEO_EXTENSIONS
            or extension in IMAGE_EXTENSIONS
        ):
            attachment = item
            break

    if attachment is None:
        await interaction.followup.send(
            "❌ That message doesn't contain a supported image or video."
        )
        return

    async def update_status(message_text):
        try:
            await interaction.edit_original_response(
                content=message_text
            )
        except Exception as e:
            print(f"STATUS UPDATE ERROR: {e}", flush=True)

    try:
        await update_status("📥 Starting GIF conversion...")

        gif_data = await process_attachment(
            attachment,
            update_status
        )

        await update_status("📤 Uploading your finished GIF...")

        await interaction.edit_original_response(
            content="✅ Done! High-quality GIF:",
            attachments=[
                discord.File(
                    __import__("io").BytesIO(gif_data),
                    filename="converted.gif"
                )
            ]
        )

    except Exception as e:
        print(
            f"MESSAGE GIF ERROR: {e}",
            flush=True
        )

        await interaction.followup.send(
            "❌ Conversion failed:\n"
            f"`{str(e)[:1500]}`"
        )


# =========================
# RENDER WEB SERVER
# =========================

async def health_check(request):
    return web.Response(
        text="GIF Converter is online!"
    )


async def start_web_server():
    app = web.Application()

    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)

    runner = web.AppRunner(app)

    await runner.setup()

    port = int(
        os.getenv("PORT", "10000")
    )

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        port
    )

    await site.start()

    print(
        f"Web server running on port {port}",
        flush=True
    )


# =========================
# START EVERYTHING
# =========================

async def main():
    await start_web_server()
    await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
