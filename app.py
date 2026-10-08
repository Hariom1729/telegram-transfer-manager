"""Hugging Face Spaces & Gradio entrypoint.

Runs the Telegram Transfer Manager bot in the background while
serving the modern Web UI dashboard on port 7860.
"""

import asyncio
import os
from pathlib import Path
import threading

# Import the main bot runner
from app.main import main


def _start_bot_thread() -> None:
    """Run the async Telegram Transfer Manager in a dedicated background thread."""
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"[FATAL] Bot background thread encountered an error: {e}")


# Launch background Telegram Bot and Transfer Manager engine
bot_thread = threading.Thread(target=_start_bot_thread, daemon=True, name="TelegramBotWorker")
bot_thread.start()

# Load the modern UI
html_path = Path(__file__).parent / "app" / "web" / "index.html"
html_content = (
    html_path.read_text(encoding="utf-8")
    if html_path.exists()
    else "<h1>Telegram Transfer Manager</h1><p>Online &amp; Operational.</p>"
)

try:
    import gradio as gr

    # Serve modern glassmorphic web dashboard in Gradio
    with gr.Blocks(title="Telegram Transfer Manager", theme=gr.themes.Base()) as demo:
        gr.HTML(html_content)

    if __name__ == "__main__":
        port = int(os.getenv("PORT", "7860"))
        demo.launch(server_name="0.0.0.0", server_port=port)
except ImportError:
    # If Gradio is not installed, fallback to native app.main
    if __name__ == "__main__":
        bot_thread.join()
