import sys

from loguru import logger

from adaptive_lang_study_bot.admin.app import ADMIN_CSS, ADMIN_THEME, create_admin_app
from adaptive_lang_study_bot.config import settings
from adaptive_lang_study_bot.logging_config import configure_logging


def main() -> None:
    configure_logging("INFO")

    logger.info("Starting LangBot Admin Panel...")

    app = create_admin_app()

    if not settings.admin_api_token:
        logger.error("ADMIN_API_TOKEN is not set. Refusing to start admin panel without authentication.")
        sys.exit(1)

    auth = ("admin", settings.admin_api_token)
    logger.info("Admin panel auth enabled (user: admin)")

    app.launch(
        server_name=settings.admin_host,
        server_port=settings.admin_port,
        share=False,
        auth=auth,
        theme=ADMIN_THEME,
        css=ADMIN_CSS,
    )


if __name__ == "__main__":
    main()
