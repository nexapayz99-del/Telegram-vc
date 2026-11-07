# Audio Forwarder Bot (multi-user, per-account)

A Telegram bot where **each user links their own Telegram account** (phone + OTP + 2FA, or a Pyrogram string
session) and controls everything from the bot's private chat — no group chat needed.
Only the user who linked an account can operate it. The owner can **remove access, ban, unban, grant** from buttons.

Stack: Pyrogram (pyrofork) · py-tgcalls · MongoDB (motor) · ffmpeg · Fernet encryption for stored sessions.

## Features
- Add account: phone + OTP (+ 2FA password) or string session; one Telegram account ↔ one bot user
- Live audio: `/record` (source voice chat) → `/join <id>` (any number of target voice chats)
- Effects (no upper limit): `/level /bass /treble /gain /overdrive /echo /effects /reset`
- `/mute /unmute /rejoin /leave /leaveall /leaverecord /list /status /stats /ping /id /panel`
- Owner panel (`/users` or button): paginated user list → Ban / Unban / Remove access / Grant access
- Owner commands: `/ban /unban /remove /grant /broadcast /restart`
- `/logout` revokes the session on Telegram and deletes it from MongoDB

## Setup
1. Get `API_ID` / `API_HASH` at https://my.telegram.org and a bot token from @BotFather.
2. Create a free MongoDB Atlas cluster and copy the connection string.
3. Generate the encryption key (keep it safe, never change it):
   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```
4. `cp .env.example .env` and fill it in.
5. Install and run (Python 3.10–3.12 recommended, ffmpeg required):
   ```bash
   sudo apt install ffmpeg
   pip install -r requirements.txt
   python -m bot.main
   ```

### Docker
```bash
docker build -t audio-forwarder .
docker run -d --env-file .env --restart always audio-forwarder
```
Heroku/Railway/Render: use the `Procfile` (worker) and `Aptfile` (ffmpeg buildpack).

### Push to GitHub
```bash
git init && git add . && git commit -m "Audio forwarder bot"
git branch -M main
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```
`.env` is git-ignored — never commit it.

## How users use it
1. `/start` → **Add Account** → choose Phone or String Session.
2. Phone flow: send the number, then the code **with spaces** (`1 2 3 4 5`) — Telegram invalidates codes
   typed as one plain number. Send the 2FA password if asked. The bot deletes those messages.
3. Make sure the linked account is a member of the source and target groups (and can join their voice chats).
4. `/setrecordgroup -100123…` → `/join -100456…` → `/record`.

## Notes
- Needs an **active voice chat** in each group, and the account must be allowed to join/speak.
- If pitch/speed sounds wrong, change `REC_SAMPLE_RATE` / `REC_CHANNELS` in `.env`.
- Latency is typically ~1–2 s (ffmpeg + FIFO + voice-chat encoder).
- Only audio-effect values you choose are applied; ffmpeg clips at full scale, so very large values distort.
- Use this only with accounts and chats you are entitled to use. Telegram may rate-limit or restrict accounts
  that join many voice chats quickly.
