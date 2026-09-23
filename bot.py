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

    print(f"Logged in as {bot.user}")

    try:

        synced = await bot.tree.sync()

        print(
            f"Synced {len(synced)} global slash command(s)"
        )

    except Exception as e:

        print(
            f"Failed to sync commands: {e}"
        )

# =========================
# DOWNLOAD FILE
# =========================

async def download_file(url, destination):

    timeout = aiohttp.ClientTimeout(
        total=3600
    )

    async with aiohttp.ClientSession(
        timeout=timeout
    ) as session:

        async with session.get(url) as response:

            if response.status != 200:

                raise RuntimeError(
                    f"Could not download the file. HTTP {response.status}"
                )

            with open(
                destination,
                "wb"
            ) as file:

                while True:

                    chunk = await response.content.read(
                        1024 * 1024
                    )

                    if not chunk:
                        break

                    file.write(chunk)


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
            "flags=lanczos"
        ),

        "-r",
        "15",

        "-gifflags",
        "-offsetting",

        "-loop",
        "0",

        output_file,
    ]

    print(
        "Running image conversion..."
    )

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout, stderr = await process.communicate()

    if process.returncode != 0:

        error_message = stderr.decode(
            errors="ignore"
        )

        raise RuntimeError(
            f"FFmpeg image conversion failed:\n"
            f"{error_message[-2000:]}"
        )

    if not os.path.exists(output_file):

        raise RuntimeError(
            "FFmpeg did not create the GIF."
        )


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
        "Running high-quality video conversion..."
    )

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout, stderr = await process.communicate()

    if process.returncode != 0:

        error_message = stderr.decode(
            errors="ignore"
        )

        raise RuntimeError(
            f"FFmpeg video conversion failed:\n"
            f"{error_message[-2000:]}"
        )

    if not os.path.exists(output_file):

        raise RuntimeError(
            "FFmpeg did not create the GIF."
        )


# =========================
# SMART CONVERSION
# =========================

async def smart_convert(
    input_file,
    output_file,
    is_image
):

    settings = [
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
            f"Converting with width={width}, fps={fps}"
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

        size = os.path.getsize(
            output_file
        )

        print(
            f"GIF size: "
            f"{size / (1024 * 1024):.2f} MB"
        )

        if size <= MAX_GIF_SIZE:

            print(
                "Quality setting accepted."
            )

            return

    raise RuntimeError(
        "The GIF is still too large after automatic compression."
    )


# =========================
# /GIF COMMAND
# =========================

@bot.tree.command(
    name="gif",
    description="Convert a video or image into a high-quality GIF.",
    allowed_contexts=app_commands.AppCommandContext(
        guild=True,
        dm_channel=True,
        private_channel=True
    )
)
@app_commands.describe(
    file="The video or image you want to convert."
)
async def gif(
    interaction: discord.Interaction,
    file: discord.Attachment
):

    await interaction.response.defer()

    filename = file.filename

    extension = Path(
        filename
    ).suffix.lower()

    if (
        extension not in VIDEO_EXTENSIONS
        and extension not in IMAGE_EXTENSIONS
    ):

        await interaction.followup.send(
            "❌ That file type isn't supported."
        )

        return

    try:

        with tempfile.TemporaryDirectory() as temp_dir:

            input_file = os.path.join(
                temp_dir,
                filename
            )

            output_file = os.path.join(
                temp_dir,
                "converted.gif"
            )

            print(
                f"Downloading: {filename}"
            )

            await download_file(
                file.url,
                input_file
            )

            print(
                "Starting conversion..."
            )

            is_image = (
                extension in IMAGE_EXTENSIONS
            )

            await smart_convert(
                input_file,
                output_file,
                is_image
            )

            print(
                "Conversion finished."
            )

            await interaction.followup.send(
                "✅ Done! High-quality GIF:",
                file=discord.File(
                    output_file,
                    filename="converted.gif"
                )
            )

    except FileNotFoundError:

        await interaction.followup.send(
            "❌ FFmpeg was not found."
        )

    except Exception as e:

        print(
            f"ERROR: {e}"
        )

        await interaction.followup.send(
            f"❌ Conversion failed:\n"
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

    app.router.add_get(
        "/",
        health_check
    )

    app.router.add_get(
        "/health",
        health_check
    )

    runner = web.AppRunner(app)

    await runner.setup()

    port = int(
        os.getenv(
            "PORT",
            "10000"
        )
    )

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        port
    )

    await site.start()

    print(
        f"Web server running on port {port}"
    )


# =========================
# START EVERYTHING
# =========================

async def main():

    await start_web_server()

    await bot.start(TOKEN)


if __name__ == "__main__":

    asyncio.run(main())
