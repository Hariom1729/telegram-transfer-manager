# Telegram Transfer Manager

A production-ready, asynchronous Telegram content transfer system controlled entirely through an interactive Telegram Bot.

Transfer individual messages, media, files, documents, albums, or entire chat histories between any accessible Telegram channels, groups, supergroups, or forum supergroup topics without hard-coding any identifiers.

---

## 🏛 Architecture Overview

The system uses a clean two-component design:

```text
               ┌────────────────────────────────────────────────────────┐
               │              TELEGRAM CONTROL BOT                      │
               │            (python-telegram-bot v22)                   │
               │   • /start & interactive inline menus                  │
               │   • Account management & 2FA login                     │
               │   • Source / Destination / Topic selection             │
               │   • Live progress updates, pause, resume, cancel       │
               └────────────────────────────────────────────────────────┘
                                           │
                                           ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│                           ASYNC TRANSFER ENGINE                               │
│                                                                               │
│   ┌────────────────────┐   ┌────────────────────┐   ┌─────────────────────┐   │
│   │   TransferQueue    │──▶│   TransferWorker   │──▶│    MessageCopier    │   │
│   │ (Bounded workers)  │   │ (State persistence)│   │ (High-fidelity copy)│   │
│   └────────────────────┘   └────────────────────┘   └─────────────────────┘   │
│              │                        │                        │              │
│              ▼                        ▼                        ▼              │
│   ┌────────────────────┐   ┌────────────────────┐   ┌─────────────────────┐   │
│   │   Deduplication    │   │   RetryExecutor    │   │  Telethon MTProto   │   │
│   │ (MessageMapping DB)│   │(Auto FloodWait)    │   │(Legitimate sessions)│   │
│   └────────────────────┘   └────────────────────┘   └─────────────────────┘   │
└───────────────────────────────────────────────────────────────────────────────┘
```

- **Telegram Bot API (`python-telegram-bot`)**: Powers the administrator control interface, interactive inline menus, and progress displays.
- **Telegram MTProto Client (`Telethon`)**: Operates on content that authenticated user accounts are legitimately authorized to access.
- **Database & Storage (`SQLAlchemy` + `aiosqlite`)**: Records job states, message mappings to prevent duplicates, and crash recovery checkpoints.

---

## 📦 Requirements

- **Python**: 3.12 or newer (tested on 3.12 - 3.14)
- **Telegram Bot Token**: from [@BotFather](https://t.me/BotFather)
- **Telegram MTProto API Credentials**: `API_ID` and `API_HASH` from [my.telegram.org](https://my.telegram.org)
- **SQLite** (default) or **PostgreSQL** for database persistence
- **Docker & Docker Compose** (optional for containerized deployment)

---

## 🔑 Setup & Credentials

### 1. BotFather Setup (Bot Token)
1. Open Telegram and search for [@BotFather](https://t.me/BotFather).
2. Send `/newbot`.
3. Follow the instructions to choose a name and username (e.g. `MyTransferManagerBot`).
4. Copy the HTTP API token provided by BotFather.

### 2. Telegram API ID & API Hash Setup
1. Log in to [https://my.telegram.org](https://my.telegram.org) using your Telegram phone number.
2. Navigate to **API development tools**.
3. Create a new application (e.g. App title: `TransferManager`, Short name: `transfermgr`).
4. Copy your `API_ID` (integer) and `API_HASH` (string).

### 3. Your Telegram User ID (Admin Allowlist)
1. Find your numerical Telegram User ID using [@userinfobot](https://t.me/userinfobot).
2. For security, only IDs listed in `ADMIN_USER_IDS` are permitted to execute commands or control transfers.

---

## 🚀 Installation & Local Run

### Step 1: Clone and Navigate
```bash
git clone <repository_url>
cd telegram-transfer-manager
```

### Step 2: Create and Activate Virtual Environment
```bash
python3 -m venv .venv
source .venv/bin/activate
```

### Step 3: Install Dependencies
```bash
pip install -r requirements.txt
```

### Step 4: Configure Environment Variables
Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```

Edit `.env` with your credentials:
```env
BOT_TOKEN=your_bot_token_here
API_ID=your_api_id_here
API_HASH=your_api_hash_here
ADMIN_USER_IDS=your_telegram_user_id_here

DATABASE_URL=sqlite+aiosqlite:///./data/telegram.db
LOG_LEVEL=INFO

MAX_CONCURRENT_TRANSFERS=2
MAX_RETRY_ATTEMPTS=5
PROGRESS_UPDATE_INTERVAL=3
SESSION_DIRECTORY=./data/sessions
WEBAPP_URL=
```

### Step 5: Start the Application
```bash
python -m app.main
```

The database tables and session directory are created automatically on first run.

---

## 📱 Bot Interface Walkthrough

### 1. First-Run & Main Menu
Open your control bot in Telegram and send `/start`:

```text
🤖 Telegram Transfer Manager

Welcome!

Transfer content between Telegram chats
using a simple Telegram interface.

Choose an action:
[📥 New Transfer]     [📊 Active Transfers]
[📋 Transfer History] [🔗 Connected Accounts]
[🔄 Live Sync]        [⚙️ Settings]
[ℹ️ Help]
```

### 2. Connecting a Telegram Account
1. Click **🔗 Connected Accounts** → **➕ Connect Telegram Account**.
2. Send your phone number in international format: `+1234567890`.
3. The bot requests a verification code from Telegram.
4. Check your official Telegram app and enter the code sent by Telegram.
5. If Two-Factor Authentication (2FA) is enabled, enter your password. (Your password is used in-memory for verification only and is never saved or logged).
6. Your account is authenticated, and its session file is stored in `data/sessions/`.

### 3. Creating a Transfer Job
1. Click **📥 New Transfer**.
2. Select which connected account to use.
3. **Select Source**:
   - `🔎 Search Telegram`: search channels/groups by keyword.
   - `📢 Channels`: browse broadcast channels.
   - `👥 Groups`: browse supergroups and basic groups.
   - `🔒 Private Chats`: browse personal dialogs.
   - `🆔 Enter Chat ID`: manually enter username (e.g. `@sourcechannel`) or numeric ID (e.g. `-1001234567890`).
4. **Select Destination**:
   - Search, browse categories, or enter numeric destination chat ID.
5. **Forum Topic Detection**:
   - If the target is a Forum Supergroup, the bot automatically fetches and displays existing topics.
   - Pick an existing topic or click **🆕 Create Topic** to create one on the fly.
6. **Content Filter**:
   - Choose `📦 Everything`, or toggle specific types: `💬 Text`, `📄 Documents`, `🎬 Videos`, `🎵 Audio`, `🖼 Photos`, `🎞 Animations`.
7. **Message Range**:
   - Select `All Messages`, `Last 100`, `Last 500`, `Last 1000`, or `Custom Range` (e.g. `1 155`).
8. **Duplicate Handling**:
   - `⏭ Skip existing` (default, checks database mapping table).
   - `🔄 Transfer again`.
9. **Transfer Preview**:
   - Inspect the summary and click **🚀 Start**.

### 4. Live Progress & Real-Time Controls
While the job runs, the bot edits a live status message throttled by `PROGRESS_UPDATE_INTERVAL`:

```text
📦 Transfer #1042

Source:
📢 Source Channel

Destination:
👥 Target Group

Topic:
🧵 Programming

━━━━━━━━━━━━━━━━
████████████░░░ 82%
━━━━━━━━━━━━━━━━

1,284 / 1,560

✅ Success: 1,250
⏭ Skipped: 27
❌ Failed: 7

Speed: 8 msg/s

[⏸ Pause] [❌ Cancel]
```

- **⏸ Pause**: Finishes the current item, saves state to the database, and presents `[▶ Resume]` and `[❌ Cancel]`.
- **▶ Resume**: Picks up immediately from `last_processed_id` without restarting from message 1.
- **❌ Cancel**: Asks for confirmation and persists the cancelled state.

---

## ⚡ Direct File / Message Transfer

If you forward or send any message, document, photo, video, or audio directly to the bot in private chat:

```text
Where should I send this?

[Send to Target Channel]
[Send to Programming Group]
[Send to Archive]
```
Select the destination and optional topic, and the bot transfers it immediately without creating a bulk job.

---

## 🛡 FloodWait & Error Resilience

1. **Automatic FloodWait Handling**:
   When Telegram responds with `FloodWaitError(seconds=N)`:
   - The worker logs: `WARNING FloodWait job=1042 wait=N`
   - Persists intermediate counters to the database.
   - Pauses for `N + 1` seconds.
   - Resumes automatically without losing position.
2. **Crash Recovery After Restart**:
   If the server or container crashes during execution:
   - On reboot, `transfer_manager.recover_interrupted_jobs()` detects any jobs in `RUNNING` status.
   - Resets them to `QUEUED` and enqueues them.
   - Uses `MessageMapping` records and `last_processed_id` to continue without duplication.
3. **Transient Error Retries**:
   Network timeouts and transient RPC errors are retried with exponential backoff up to `MAX_RETRY_ATTEMPTS`.
   Non-retryable errors (e.g. lack of write permission) are logged to `transfer_errors` and the job continues with the next message.

---

## 🐳 Docker Deployment

The application is fully containerized. Persistent volumes are mounted for SQLite database and Telethon session files.

```bash
# 1. Create .env with your credentials
cp .env.example .env
nano .env

# 2. Build and run in background
docker compose up -d --build

# 3. View live logs
docker compose logs -f
```

---

## 🧪 Running Tests

Unit tests mock Telegram MTProto operations and run entirely in-memory:

```bash
pytest -v
```

All tests cover:
- Database schema and unique constraints
- Deduplication mapping logic
- FloodWait handling & exponential backoff
- Pause, resume, and cancellation state transitions
- Crash recovery on startup
- Content classification and filtering
- Input validation

---

## ✨ Modern Web Dashboard & Telegram Mini App

The application features a modern glassmorphic web interface that works both as a standalone web app and as a native Telegram Mini App (TMA):

- **Standalone Web Access**: Visit `http://localhost:7860` (or your public Hugging Face / VPS URL on port `7860`).
- **Telegram Mini App**: Set `WEBAPP_URL=https://your-domain.com` in `.env` to enable the `[✨ Modern Web Dashboard]` button directly inside Telegram.
- **Key Capabilities**:
  - Live real-time transfer progress with accurate percentage bars (always 100% on completion).
  - 1-Click re-transfer of only failed messages.
  - Interactive pause, resume, and cancellation controls.
  - Transfer history and connected account status cards.
  - Live system health and MTProto benchmark speed metrics.

---

## 🔒 Security Best Practices

1. **Authorization**: Only user IDs configured in `ADMIN_USER_IDS` can interact with the bot. Unauthorized users receive `⛔ You are not authorized to use this bot.`
2. **Secret Redaction**: Structured logging masks bot tokens, API hashes, session credentials, and login codes.
3. **Session Privacy**: Telethon `.session` files are stored locally in `data/sessions/` and are never transmitted over Telegram.
4. **Legitimate Access**: The application strictly complies with Telegram's terms and only accesses chats and media legitimately accessible to the authenticated account.

