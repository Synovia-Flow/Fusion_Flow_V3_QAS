"""Console output safety for Windows subprocess jobs."""
import sys


def configure_console_output():
    """Avoid UnicodeEncodeError when scripts print arrows/dashes on Windows."""
    for stream in (getattr(sys, 'stdout', None), getattr(sys, 'stderr', None)):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(errors='replace')
