import sys

from loguru import logger

# Keep tracebacks, but never print variable values in them: they can include API keys and
# passwords from the settings.
logger.remove()
logger.add(sys.stderr, level="DEBUG", diagnose=False)
