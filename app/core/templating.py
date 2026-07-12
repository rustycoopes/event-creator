from pathlib import Path

from fastapi.templating import Jinja2Templates
from organizeme_chrome import register_chrome

APP_DIR = Path(__file__).resolve().parent.parent

templates = Jinja2Templates(directory=APP_DIR / "templates")
register_chrome(templates.env, app_service_name="event-creator")
