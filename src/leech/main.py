import os
from dotenv import load_dotenv


def main():
    load_dotenv()
    from .leech import Leech
    from leech.core.log import logger

    leech = Leech()
    discord_token = os.getenv("DISCORD_TOKEN")
    if not discord_token:
        logger.fatal("failed to load 'DISCORD_TOKEN' environment variable.")
        exit(1)
    leech.run(discord_token)
