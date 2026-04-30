import time
from datetime import timedelta

# This grabs the exact time the server starts
START_TIME = time.time()

def get_uptime():
    """Calculates the time difference and formats it cleanly."""
    uptime_seconds = int(time.time() - START_TIME)
    # timedelta automatically formats seconds into "Days, HH:MM:SS"
    return str(timedelta(seconds=uptime_seconds))
