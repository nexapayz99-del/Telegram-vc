# Audio Forwarder Bot (multi-user, Source + Target accounts)

Each approved user links **two of their own Telegram accounts** in the bot's DM:

| Slot | Job |
|---|---|
| 🎙 **Source account** | joins the *SOURCE* voice chat (where you talk) and listens |
| 🔊 **Target account** | joins the *TARGET* voice chat (where it plays) and plays your voice with effects |

(One Telegram account can only be in one voice chat at a time, so two accounts are required.)
Login by phone + OTP (+2FA) or Pyrogram string session. Sessions are Fernet-encrypted in MongoDB.

## Access control
- Only users **approved by the owner** can use anything (`REQUIRE_APPROVAL=true`). Others see a *Request access* button.
- Owner: Approve / Reject requests, Remove access (revokes + deletes their accounts), Ban, Unban — buttons (`/users`) or commands.
- In groups, non-approved users are silently ignored, so nobody else can stop or control your relay.

## How it works
```
Group A voice chat (you talk)  ➜  🎛 effects  ➜  Group B voice chat (it plays)
      SOURCE / Source account                      TARGET / Target account
```
The Source account only hears **other** people, so talk with your own main account — not with the Source/Target account.

1. **Accounts** (DM → 👥 Accounts): add Source + Target account (🔄 Swap if you mixed them up).
2. **Groups**: Source account is a member of Group A, Target account of Group B. Start a voice chat in both. Add the bot to both.
3. **Source & Target**: `/source` in Group A, `/target` in Group B (or DM → 💬 Chats). They must be different groups.
4. **🚀 Go Live** (or `/golive`), talk in Group A's voice chat.
5. `/stop`: you get a recording in **your bot DM only** (never sent to groups). Choose the *content* (original voice / processed output) in 🎙 Audio.

If a voice chat drops (kicked, closed, network), the bot reconnects automatically and tells you.

### If you don't hear the voice (do these in order)
1. `/golive`, then talk in Group A's voice chat **with a different account** (not the Source/Target account).
   Within ~25 s the bot DMs you either ✅ *Receiving audio* (with the detected format) or ⚠️ *No audio received*.
2. `/tonetest` — plays a clean beep into Group B's voice chat through the whole output path.
   - Beep is clean → playback is fine; the problem is capture → `/audiodebug`, `/sample`.
   - Beep is noisy/silent → `/playmode` (switch fifo ⇄ external), Stop, Go Live, `/tonetest` again.
3. `/audiodebug` — frames received, detected format, buffer, reconnects.
4. `/sample` — sends you 5 s of exactly what the Source account hears (`/sample out` = after effects).

The bot **measures the real audio format** of the source voice chat (sample rate / mono / stereo) and converts it
automatically, so a wrong assumption can no longer turn the voice into noise. Effects end in a limiter, so bass /
gain / overdrive / level can be set very high and stay loud without crackling. If the effects process stalls or
dies, it is restarted automatically.

## Commands
Accounts: `/accounts /myaccount /logout /cancel` ·
Go live: `/source /target /golive /stop /check /howto` · Audio: `/record /stoprecord /join /leave /leaveall /rejoin /mute /unmute /sendfile /list /status` ·
Effects (no limits): `/level /bass /treble /gain /overdrive /echo /effects /reset` ·
Utility: `/ping /stats /id /panel /menu /help` ·
Owner: `/users /pending /approve /remove /ban /unban /broadcast /restart`

## Keep it running (VPS)
Closing SSH stops a bot started in the terminal. Run it as a service instead:
```bash
sudo cp deploy/audio-forwarder.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now audio-forwarder
journalctl -u audio-forwarder -f        # live logs
sudo systemctl restart audio-forwarder  # REQUIRED after every git pull (enable --now does NOT restart a running service)
systemctl status audio-forwarder        # check the 'Active since' time is new; the bot also DMs the owner its version on start
```
(Edit the paths in the file if your folder is not `/root/Telegram-vc`.)

### Duplicate replies / `FileNotFoundError` / `TimeoutError`
These mean **two copies of the bot are running** with the same token. v5.2 refuses to start a second copy. To clean up:
```bash
sudo systemctl stop audio-forwarder
pkill -f "bot.main"; sleep 2
ps aux | grep bot.main | grep -v grep     # must print nothing
sudo systemctl start audio-forwarder
```
Never run `python -m bot.main` by hand while the service is running.

### Git: never commit the virtualenv
```bash
git rm -r --cached bot/venv __pycache__ 2>/dev/null; git commit -m "untrack venv"
```
(`.gitignore` already ignores `venv/` and `__pycache__/`.)

## Setup
```bash
sudo apt install ffmpeg python3-pip -y
pip install -r requirements.txt
cp .env.example .env      # fill it in
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"   # -> ENCRYPTION_KEY
python3 -m bot.main
```
Docker: `docker build -t audio-forwarder . && docker run -d --env-file .env --restart always audio-forwarder`

## GitHub
```bash
git init && git add . && git commit -m "Audio forwarder bot"
git branch -M main && git remote add origin https://github.com/<you>/<repo>.git && git push -u origin main
```
`.env` is git-ignored — never commit it.

## Notes
- Each group needs an **active voice chat**; the linked accounts must be able to join it.
- Login code must be typed with spaces (`1 2 3 4 5`) or Telegram invalidates it.
- Wrong pitch/speed → change `REC_SAMPLE_RATE` / `REC_CHANNELS` in `.env`.
- Telegram Mini Apps can't capture or play voice-chat audio, so live forwarding runs in the bot (DM + group mode).
