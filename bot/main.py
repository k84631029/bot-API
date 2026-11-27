"""Entry point: `python -m bot.main`.

Runs with a webhook when WEBHOOK_BASE_URL (or Render's RENDER_EXTERNAL_URL) is
set, otherwise with long polling (local development).
"""
from __future__ import annotations

import logging

from telegram import BotCommand, Update
from telegram.request import BaseRequest
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, TypeHandler, filters)

from .catalog import Catalog, RuntimeSettings
from .config import Settings, load_settings
from .g4s import G4SClient
from .handlers import owner, user
from .handlers.common import BTN_ADMIN, Services
from .jobs import cleanup_job, make_notifiers, provider_balance_job, reconcile_job
from .orders import OrderService
from .ratelimit import RateLimiter
from .repo import Repo
from .security import Crypto
from .store import FirestoreStore, Store

log = logging.getLogger("bot")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("صار خطأ غير متوقع، جرّب مرة ثانية.")
        except Exception:
            pass


def build_app(settings: Settings, store: Store | None = None, api: G4SClient | None = None,
              request: BaseRequest | None = None) -> Application:
    """store/api/request are injectable for tests; production builds them here."""
    crypto = Crypto(settings.encryption_keys, settings.hmac_key)
    if store is None:
        store = FirestoreStore.connect(settings.firebase_credentials_json, settings.google_project_id)
    repo = Repo(store, crypto, settings.log_retention_days)
    if api is None:
        api = G4SClient(settings.g4s_api_token, settings.g4s_base_url, proxy=settings.g4s_proxy_url)
    catalog = Catalog(api)
    runtime = RuntimeSettings(repo, settings.default_margin_percent)

    async def post_shutdown(app: Application) -> None:
        await api.aclose()

    async def post_init(app: Application) -> None:
        # The blue "Menu" button next to the text box opens /start.
        try:
            await app.bot.set_my_commands([BotCommand("start", "القائمة الرئيسية")])
        except Exception as exc:  # not critical: the bot works without it
            log.warning("Could not set bot commands: %s", exc)

    builder = Application.builder().token(settings.bot_token)
    if request is not None:
        builder = builder.request(request).get_updates_request(request)
    app = (builder
           .post_init(post_init)
           .post_shutdown(post_shutdown)
           # One slow provider call must not block other customers. Wallet
           # safety comes from Firestore transactions, not from serial updates.
           .concurrent_updates(32)
           .build())
    notify_user, alert_owner = make_notifiers(app)
    orders = OrderService(repo, api, catalog, runtime, notify_user, alert_owner)
    app.bot_data["svc"] = Services(
        settings=settings, repo=repo, api=api, catalog=catalog, runtime=runtime, orders=orders,
        action_limiter=RateLimiter(settings.actions_per_minute),
        order_limiter=RateLimiter(settings.orders_per_minute),
    )

    app.add_handler(TypeHandler(Update, user.gate), group=-1)

    app.add_handler(CommandHandler("start", user.start))
    app.add_handler(CommandHandler("admin", owner.panel))
    app.add_handler(CommandHandler("credit", owner.credit))
    app.add_handler(CommandHandler("debit", owner.debit))
    app.add_handler(CommandHandler("user", owner.user_info))
    app.add_handler(CommandHandler("ban", owner.ban))
    app.add_handler(CommandHandler("unban", owner.unban))
    app.add_handler(CommandHandler("products", owner.products_cmd))
    app.add_handler(CommandHandler("margin", owner.margin))
    app.add_handler(CommandHandler("maintenance", owner.maintenance))
    app.add_handler(CommandHandler("order", owner.order_info))
    app.add_handler(CommandHandler("refund", owner.refund))
    app.add_handler(CommandHandler("recheck", owner.recheck))
    app.add_handler(CommandHandler("logs", owner.logs))

    app.add_handler(CallbackQueryHandler(user.menu, pattern=r"^m:(home|shop|bal|top|ord|led)$"))
    app.add_handler(CallbackQueryHandler(user.categories, pattern=r"^c:\d+$"))
    app.add_handler(CallbackQueryHandler(user.category, pattern=r"^k:[0-9a-f]{10}:\d+$"))
    app.add_handler(CallbackQueryHandler(user.product, pattern=r"^p:\d+$"))
    app.add_handler(CallbackQueryHandler(user.choose_qty, pattern=r"^q:\d+$"))
    app.add_handler(CallbackQueryHandler(user.confirm_order, pattern=r"^o:[0-9a-f-]{36}$"))
    app.add_handler(CallbackQueryHandler(user.cancel, pattern=r"^x$"))
    app.add_handler(CallbackQueryHandler(user.order_detail, pattern=r"^od:[0-9a-f-]{36}$"))
    app.add_handler(CallbackQueryHandler(owner.panel_button, pattern=r"^a:"))

    app.add_handler(MessageHandler(filters.Text([BTN_ADMIN]), owner.panel))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, user.text_input))
    app.add_error_handler(on_error)

    jq = app.job_queue
    jq.run_repeating(reconcile_job, interval=settings.order_poll_seconds, first=15)
    jq.run_repeating(provider_balance_job, interval=settings.balance_check_seconds, first=30)
    jq.run_repeating(cleanup_job, interval=600, first=600)
    return app


def main() -> None:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    # httpx logs full request URLs at INFO, which include the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # Access logs would print the webhook path, which contains the secret.
    logging.getLogger("tornado.access").setLevel(logging.WARNING)

    settings = load_settings()
    app = build_app(settings)
    updates = ["message", "callback_query"]
    if settings.use_webhook:
        path = f"tg/{settings.webhook_secret}"
        log.info("Starting webhook on port %s", settings.port)
        app.run_webhook(
            listen="0.0.0.0",
            port=settings.port,
            url_path=path,
            webhook_url=f"{settings.webhook_base_url}/{path}",
            secret_token=settings.webhook_secret,
            allowed_updates=updates,
            drop_pending_updates=False,
        )
    else:
        log.info("Starting long polling (no WEBHOOK_BASE_URL)")
        app.run_polling(allowed_updates=updates)


if __name__ == "__main__":
    main()
