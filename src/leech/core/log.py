import logging
import logging, logging.handlers

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
handler = logging.handlers.TimedRotatingFileHandler(
    "leech.log", when="midnight", backupCount=3
)
logger = logging.getLogger("leech")
logger.addHandler(handler)
# logging.getLogger("httpx").setLevel(logging.WARNING)
# logging.getLogger("httpcore").setLevel(logging.WARNING)
