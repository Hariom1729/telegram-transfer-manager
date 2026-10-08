"""Inline Keyboard Builders for Telegram Transfer Manager UI."""

from typing import List, Optional
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from app.models.telegram_account import TelegramAccount
from app.telegram.discovery import DiscoveredChat
from app.telegram.topics import DiscoveredTopic


def build_main_menu_keyboard() -> InlineKeyboardMarkup:
    """Construct the primary dashboard inline keyboard."""
    keyboard = [
        [
            InlineKeyboardButton("📥 New Transfer", callback_data="nav:new_transfer"),
            InlineKeyboardButton("💾 Local Download", callback_data="nav:new_download"),
        ],
        [
            InlineKeyboardButton("📊 Active Transfers", callback_data="nav:active"),
            InlineKeyboardButton("📋 Transfer History", callback_data="nav:history"),
        ],
        [
            InlineKeyboardButton("🔗 Connected Accounts", callback_data="nav:accounts"),
            InlineKeyboardButton("🔄 Live Sync", callback_data="nav:live_sync"),
        ],
        [
            InlineKeyboardButton("⚙️ Settings", callback_data="nav:settings"),
            InlineKeyboardButton("ℹ️ Help", callback_data="nav:help"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def build_accounts_keyboard(
    accounts: List[TelegramAccount],
) -> InlineKeyboardMarkup:
    """Build accounts management keyboard."""
    buttons = [
        [InlineKeyboardButton("➕ Connect Telegram Account", callback_data="acc:connect")],
    ]
    for acc in accounts:
        display_name = acc.first_name or acc.username or acc.phone_number
        buttons.append(
            [
                InlineKeyboardButton(
                    f"👤 {display_name} ({acc.phone_number[-4:]})",
                    callback_data=f"acc:view:{acc.id}",
                ),
                InlineKeyboardButton(
                    "🗑 Disconnect", callback_data=f"acc:disconnect:{acc.id}"
                ),
            ]
        )
    buttons.append([InlineKeyboardButton("🏠 Home", callback_data="nav:home")])
    return InlineKeyboardMarkup(buttons)


def build_chat_selection_modes_keyboard(
    target_type: str,  # 'source' or 'dest'
) -> InlineKeyboardMarkup:
    """Build selection modes: search, category filters, manual ID."""
    prefix = "src" if target_type == "source" else "dst"
    keyboard = [
        [
            InlineKeyboardButton(
                "🔎 Search Telegram", callback_data=f"{prefix}:mode:search"
            ),
            InlineKeyboardButton(
                "📋 Recent", callback_data=f"{prefix}:mode:recent"
            ),
        ],
        [
            InlineKeyboardButton("📢 Channels", callback_data=f"{prefix}:cat:channel"),
            InlineKeyboardButton("👥 Groups", callback_data=f"{prefix}:cat:group"),
        ],
        [
            InlineKeyboardButton(
                "🔒 Private Chats", callback_data=f"{prefix}:cat:private"
            ),
            InlineKeyboardButton("🆔 Enter Chat ID", callback_data=f"{prefix}:mode:manual"),
        ],
        [
            InlineKeyboardButton("⬅️ Back", callback_data="nav:home"),
            InlineKeyboardButton("🏠 Home", callback_data="nav:home"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def build_chat_list_keyboard(
    chats: List[DiscoveredChat],
    target_type: str,  # 'source' or 'dest'
    page: int = 0,
    page_size: int = 6,
) -> InlineKeyboardMarkup:
    """Build paginated list of discovered chats."""
    prefix = "src" if target_type == "source" else "dst"
    start_idx = page * page_size
    page_chats = chats[start_idx : start_idx + page_size]

    buttons = []
    for chat in page_chats:
        icon = "📢"
        if chat.chat_type in ("group", "supergroup"):
            icon = "👥"
        elif chat.chat_type == "private":
            icon = "🔒"

        title_display = chat.title[:25] + ("..." if len(chat.title) > 25 else "")
        buttons.append(
            [
                InlineKeyboardButton(
                    f"{icon} {title_display}",
                    callback_data=f"{prefix}:pick:{chat.id}",
                )
            ]
        )

    # Pagination controls
    nav_row = []
    if page > 0:
        nav_row.append(
            InlineKeyboardButton(
                "⬅️ Prev", callback_data=f"{prefix}:page:{page - 1}"
            )
        )
    if start_idx + page_size < len(chats):
        nav_row.append(
            InlineKeyboardButton(
                "Next ➡️", callback_data=f"{prefix}:page:{page + 1}"
            )
        )
    if nav_row:
        buttons.append(nav_row)

    buttons.append(
        [
            InlineKeyboardButton("⬅️ Back", callback_data=f"{prefix}:back"),
            InlineKeyboardButton("❌ Cancel", callback_data="nav:cancel"),
        ]
    )
    return InlineKeyboardMarkup(buttons)


def build_topics_keyboard(
    topics: List[DiscoveredTopic],
    can_create: bool = True,
) -> InlineKeyboardMarkup:
    """Build forum supergroup topics selector."""
    buttons = []
    # Two topics per row
    row = []
    for topic in topics[:12]:
        row.append(
            InlineKeyboardButton(
                f"🧵 {topic.title[:18]}", callback_data=f"topic:pick:{topic.id}"
            )
        )
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    control_row = [
        InlineKeyboardButton("🔎 Search Topic", callback_data="topic:search")
    ]
    if can_create:
        control_row.append(
            InlineKeyboardButton("➕ Create Topic", callback_data="topic:create")
        )
    buttons.append(control_row)
    buttons.append(
        [
            InlineKeyboardButton("🔄 Refresh Topics", callback_data="topic:refresh"),
            InlineKeyboardButton("⬅️ Back", callback_data="topic:back"),
            InlineKeyboardButton("❌ Cancel", callback_data="nav:cancel"),
        ]
    )
    return InlineKeyboardMarkup(buttons)


def build_content_filters_keyboard(
    selected_types: List[str],
) -> InlineKeyboardMarkup:
    """Build multi-select content type buttons."""
    is_all = "all" in selected_types or "everything" in selected_types

    def check(t: str) -> str:
        return "✅ " if (is_all or t in selected_types) else "▫️ "

    keyboard = [
        [
            InlineKeyboardButton(
                f"{'✅ ' if is_all else '▫️ '}📦 Everything",
                callback_data="content:toggle:all",
            )
        ],
        [
            InlineKeyboardButton(
                f"{check('text')}💬 Text", callback_data="content:toggle:text"
            ),
            InlineKeyboardButton(
                f"{check('document')}📄 Documents",
                callback_data="content:toggle:document",
            ),
        ],
        [
            InlineKeyboardButton(
                f"{check('video')}🎬 Videos", callback_data="content:toggle:video"
            ),
            InlineKeyboardButton(
                f"{check('audio')}🎵 Audio", callback_data="content:toggle:audio"
            ),
        ],
        [
            InlineKeyboardButton(
                f"{check('photo')}🖼 Photos", callback_data="content:toggle:photo"
            ),
            InlineKeyboardButton(
                f"{check('animation')}🎞 Animations",
                callback_data="content:toggle:animation",
            ),
        ],
        [
            InlineKeyboardButton("➡️ Continue", callback_data="content:done"),
            InlineKeyboardButton("❌ Cancel", callback_data="nav:cancel"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def build_range_keyboard() -> InlineKeyboardMarkup:
    """Build message range selector buttons."""
    keyboard = [
        [
            InlineKeyboardButton("All Messages", callback_data="range:pick:all"),
            InlineKeyboardButton("First 5", callback_data="range:pick:first_5"),
            InlineKeyboardButton("First 10", callback_data="range:pick:first_10"),
        ],
        [
            InlineKeyboardButton("Last 50", callback_data="range:pick:50"),
            InlineKeyboardButton("Last 100", callback_data="range:pick:100"),
        ],
        [
            InlineKeyboardButton("Last 500", callback_data="range:pick:500"),
            InlineKeyboardButton("Last 1000", callback_data="range:pick:1000"),
        ],
        [
            InlineKeyboardButton("Custom Range / Count", callback_data="range:pick:custom"),
        ],
        [
            InlineKeyboardButton("⬅️ Back", callback_data="range:back"),
            InlineKeyboardButton("❌ Cancel", callback_data="nav:cancel"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def build_duplicate_mode_keyboard(current_mode: str = "skip") -> InlineKeyboardMarkup:
    """Build duplicate behavior keyboard."""
    skip_mark = "✅ " if current_mode == "skip" else "▫️ "
    overwrite_mark = "✅ " if current_mode == "overwrite" else "▫️ "

    keyboard = [
        [
            InlineKeyboardButton(
                f"{skip_mark}⏭ Skip existing", callback_data="dup:set:skip"
            )
        ],
        [
            InlineKeyboardButton(
                f"{overwrite_mark}🔄 Transfer again",
                callback_data="dup:set:overwrite",
            )
        ],
        [
            InlineKeyboardButton("➡️ Continue", callback_data="dup:done"),
            InlineKeyboardButton("❌ Cancel", callback_data="nav:cancel"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def build_preview_keyboard(job_id: Optional[int] = None) -> InlineKeyboardMarkup:
    """Build preview confirmation buttons."""
    keyboard = [
        [
            InlineKeyboardButton("🚀 Start", callback_data="preview:start"),
            InlineKeyboardButton("✏️ Edit", callback_data="preview:edit"),
        ],
        [
            InlineKeyboardButton("❌ Cancel", callback_data="nav:cancel"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def build_job_completion_keyboard(job_id: int) -> InlineKeyboardMarkup:
    """Build buttons shown when a transfer is finished."""
    keyboard = [
        [
            InlineKeyboardButton(
                "🔄 Retry Failed", callback_data=f"job_retry:{job_id}"
            ),
            InlineKeyboardButton(
                "📊 Details", callback_data=f"job_view:{job_id}"
            ),
        ],
        [
            InlineKeyboardButton("🏠 Home", callback_data="nav:home"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def build_paused_keyboard(job_id: int) -> InlineKeyboardMarkup:
    """Build resume and cancel buttons for paused job."""
    keyboard = [
        [
            InlineKeyboardButton("▶ Resume", callback_data=f"job_resume:{job_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"job_cancel:{job_id}"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def build_cancel_confirmation_keyboard(job_id: int) -> InlineKeyboardMarkup:
    """Build cancel confirmation prompt buttons."""
    keyboard = [
        [
            InlineKeyboardButton(
                "Yes, Cancel", callback_data=f"job_cancel_confirm:{job_id}"
            ),
            InlineKeyboardButton(
                "Keep Running", callback_data=f"job_keep_running:{job_id}"
            ),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def build_auth_code_keyboard() -> InlineKeyboardMarkup:
    """Keyboard for Telegram login code input with Request New Code button."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔄 Request New Code", callback_data="acc:new_code")],
            [InlineKeyboardButton("❌ Cancel", callback_data="nav:cancel")],
        ]
    )


def build_auth_2fa_keyboard() -> InlineKeyboardMarkup:
    """Keyboard for Telegram 2FA password prompt."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("❌ Cancel", callback_data="nav:cancel")],
        ]
    )


