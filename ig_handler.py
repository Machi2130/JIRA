import asyncio
import logging
import os
import tempfile
import instaloader
from telegram import Update, InputMediaPhoto
from telegram.ext import CommandHandler, ContextTypes

logger = logging.getLogger(__name__)


def _shortcode(url: str) -> str:
    parts = url.rstrip("/").split("/")
    return parts[-1] if parts[-1] else parts[-2]


def _download_sync(shortcode: str, tmpdir: str) -> list[str]:
    L = instaloader.Instaloader(
        download_videos=False,
        download_video_thumbnails=False,
        save_metadata=False,
        post_metadata_txt_pattern="",
        quiet=True,
    )
    post = instaloader.Post.from_shortcode(L.context, shortcode)
    L.download_post(post, target=tmpdir)
    return sorted(
        os.path.join(tmpdir, f)
        for f in os.listdir(tmpdir)
        if f.lower().endswith((".jpg", ".jpeg", ".png"))
    )


async def _cmd_ig(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: /ig <instagram_url>")
        return

    shortcode = _shortcode(ctx.args[0])
    status = await update.message.reply_text("Downloading...")

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            loop = asyncio.get_event_loop()
            images = await loop.run_in_executor(None, _download_sync, shortcode, tmpdir)
        except Exception as e:
            logger.error("IG download failed for %s: %s", shortcode, e)
            await status.edit_text(f"Failed: {e}")
            return

        if not images:
            await status.edit_text("No images found in that post.")
            return

        await status.delete()

        if len(images) == 1:
            with open(images[0], "rb") as f:
                await update.message.reply_photo(f.read())
        else:
            # Telegram caps media groups at 10
            media = [InputMediaPhoto(open(p, "rb").read()) for p in images[:10]]
            await update.message.reply_media_group(media)


def register(app, allowed_chat_id: int) -> None:
    async def guarded(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.id != allowed_chat_id:
            return
        await _cmd_ig(update, ctx)

    app.add_handler(CommandHandler("ig", guarded))
