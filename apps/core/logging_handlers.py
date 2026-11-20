import logging.handlers

class SafeRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """
    Windows-safe RotatingFileHandler.
    On Windows, when multiple processes or threads have the log file open,
    os.rename() raises PermissionError: [WinError 32].
    This handler gracefully catches the error so logging continues without crashing.
    """
    def doRollover(self):
        try:
            super().doRollover()
        except (PermissionError, OSError):
            pass
