# HTTP Shortcuts → Telegram Bot

This setup lets you share a link from Android apps into Telegram, then send it to the existing Drive Saver bot. It does **not** call the Telegram Bot API from the phone, so the bot token is never stored in HTTP Shortcuts.

## Configure HTTP Shortcuts

1. Open HTTP Shortcuts and create a **Global Variable** of type **Static Variable** named `SharedURL`.
2. Enable **Allow Receiving Value from Share Dialog** and choose **Text** as the shared value. Save it. (The app's variable editor documents this option.)
3. Create a **Browser Shortcut** named `Send link to Drive Saver`.
4. Set its URL to Telegram's share endpoint: `https://t.me/share/url?url={SharedURL}`. Insert the variable using the app's `{}` variable picker and enable URL encoding for the variable if that option is shown. Do not type the braces manually if the picker can insert the placeholder.
5. Save. In the shortcut's trigger settings, enable it as a Direct Share target if offered on your Android version.

## Use it

1. In YouTube, TikTok, a browser, or another app, tap **Share**.
2. Select **Send link to Drive Saver**.
3. Telegram opens its share flow. Choose your Drive Saver bot and press **Send**.

Telegram may require choosing the destination chat and confirming Send; this is intentional and avoids exposing a bot token inside the shortcut. The bot then receives the link using its normal Telegram message handler.

## Important limitations and safety

- Colab must be running and connected to Telegram to process the link immediately. This shortcut does not keep Colab alive.
- The shortcut does not upload content itself; it forwards the shared text through Telegram.
- Never put `TELEGRAM_BOT_TOKEN`, Google credentials, or cookies in the shortcut URL, headers, or exported shortcut file.
- Only share links you are authorized to access. Login-protected sites may still require a supported session/cookie setup in Colab.
- The bot's video-quality selection remains manual; this shortcut does not choose a quality.
- This uses Telegram's share interface, not a direct Bot API call. Therefore it is safer but may require one extra tap to select the bot and send.

Official HTTP Shortcuts docs:
- Sharing text into a shortcut: https://http-shortcuts.rmy.ch/variables
- Browser shortcuts: https://http-shortcuts.rmy.ch/shortcuts
- Telegram share URL: https://t.me/share/url
