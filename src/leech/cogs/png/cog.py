from io import BytesIO
from urllib.parse import urlparse

import discord
import httpx

from leech.core.cog.cog import Cog, CogContext
from leech.core.cog.error import CogError
from leech.core.command.runtime import Command, Argument
from leech.core.command.invocation import Invocation
from leech.core.command.native.binding import NativeBinding
from leech.core.command.mock.binding import MockBinding
from leech.core.parser.types.registry import Types, TypesRegistry

from .compressor import quantize_image

MAX_DOWNLOAD = 20 * 1024 * 1024  # 20 MiB
ALLOWED_HOSTS = {
    "cdn.discordapp.com",
    "media.discordapp.net",
}


class PngCog(Cog):
    def __init__(self, context: CogContext):
        super().__init__(context=context)
        self.context.add_on_load(self.load)

    async def load(self):
        reduce = Command(
            name="reduce",
            description="reduce an image's file size by reducing its colors.",
            usage_example=">png reduce colors=64",
            help_content=(
                "reduce image colors using Pillow palette quantization only.\n"
                "inputs: attach image, or reply to a message with an image, "
                "or provide a discord media.\n"
                "options: colors=128 url=https://cdn.discordapp.com/...\n"
                "colors must be between 2 and 256.\n"
                "output is png."
            ),
            arguments=[
                Argument(
                    name="colors",
                    description="maximum number of colors, from 2 to 256.",
                    usage_example="colors=128",
                    type=TypesRegistry.require(Types.INT),
                    required=False,
                ),
                Argument(
                    name="url",
                    description="https discord image link.",
                    usage_example="url=https://cdn.discordapp.com/...",
                    type=TypesRegistry.require(Types.STR),
                    required=False,
                ),
            ],
            subcommands={},
            binding=NativeBinding(handler=self.reduce_handler),
        )

        image = Command(
            name="png",
            description="png size reduction tools.",
            usage_example=">png reduce colors=128",
            arguments=[],
            subcommands={"reduce": reduce},
            binding=MockBinding(),
            is_root=True,
        )

        if not self.context.add_command(image.name, image):
            raise CogError("failed to load 'png' command")

    async def _download(self, url: str) -> bytes:
        parsed = urlparse(url)

        if (
            parsed.scheme != "https"
            or parsed.hostname not in ALLOWED_HOSTS
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
        ):
            raise CogError(
                "provide a valid HTTPS Discord CDN URL.",
                is_user_error=True,
            )

        try:
            async with httpx.AsyncClient(
                timeout=15,
                follow_redirects=False,
            ) as client:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()

                    length = response.headers.get("content-length")
                    if length and int(length) > MAX_DOWNLOAD:
                        raise CogError(
                            "image exceeds the 20mb download limit.",
                            is_user_error=True,
                        )

                    chunks = []
                    total = 0

                    async for chunk in response.aiter_bytes():
                        total += len(chunk)

                        if total > MAX_DOWNLOAD:
                            raise CogError(
                                "image exceeds the 20mb download limit.",
                                is_user_error=True,
                            )

                        chunks.append(chunk)

                    return b"".join(chunks)

        except CogError:
            raise
        except httpx.HTTPError as exc:
            raise CogError(
                f"couldn't download image: {exc}",
                is_user_error=True,
            ) from exc

    async def _get_source(
        self,
        invocation: Invocation,
        url: str | None,
    ) -> tuple[bytes, str]:
        message = invocation.message

        if url:
            filename = urlparse(url).path.rsplit("/", 1)[-1] or "image"
            return await self._download(url), filename

        attachments = getattr(message, "attachments", [])
        if attachments:
            attachment = attachments[0]
            return (
                await self._download(attachment.url),
                attachment.filename,
            )

        reference = getattr(message, "reference", None)
        replied = getattr(reference, "resolved", None) if reference else None

        if replied is not None:
            attachments = getattr(replied, "attachments", [])
            if attachments:
                attachment = attachments[0]
                return (
                    await self._download(attachment.url),
                    attachment.filename,
                )

        raise CogError(
            "attach an image, reply to a message with an image, "
            "or provide a discord link using url=.",
            is_user_error=True,
        )

    async def reduce_handler(self, invocation: Invocation):
        args = invocation.arguments

        colors = args.get("colors")
        if colors is None:
            colors = 128

        url = (args.get("url") or "").strip() or None

        try:
            source, _filename = await self._get_source(invocation, url)
            result = quantize_image(source, colors=colors)

        except CogError:
            raise
        except (ValueError, OSError) as exc:
            raise CogError(
                f"couldn't process this image: {exc}",
                is_user_error=True,
            ) from exc

        output = BytesIO(result)

        await invocation.message.reply(
            content=f"reduced the image to at most **{colors} colors** (png).",
            file=discord.File(
                output,
                filename="reduced.png",
            ),
        )


def create_cog(context: CogContext):
    return PngCog(context=context)
