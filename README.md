# Audio Forwarder Bot (multi-user, Recorder + Broadcaster)

Each approved user links **two of their own Telegram accounts** in the bot's DM:

| Slot | Job |
|---|---|
| 🎙 **Recorder** | joins the *source* voice chat and listens |
| 📡 **Broadcaster** | joins the *target* voice chat(s) and plays the audio with your effects |

(One Telegram account can only be in one voice chat at a time, so two accounts are required.)
Login by phone + OTP (+2FA) or Pyrogram string session. Sessions are Fernet-encrypted in MongoDB.

## Access control
- Only users **approved by the owner** can use anything (`REQUIRE_APPROVAL=true`). Others see a *Request access* button.
- Owner: Approve / Reject requests, Remove access (revokes + deletes their accounts), Ban, Unban — buttons (`/users`) or commands.
- In groups, non-approved users are silently ignored, so nobody else can stop or control your relay.

## Two ways to use it
**1. DM menus** — `/start` → Accounts · Audio · Effects · Chats · Help (all with submenus/back buttons).
**2. Group mode** — add the bot to your groups; your Recorder and Broadcaster accounts must be members too.
- In the **source** group: `/record` → `/stoprecord`
- In the **target** group: `/join` → `/leave`
- `/leaveall`, `/mute`, `/unmute`, `/bass 30`, `/echo 500 0.6` … work in groups too.

When recording stops, the recording file (mp3) is sent to your target chats (or your DM). Toggle with `/sendfile`.
Add the bot as admin in target channels so it can post the file.

## Commands
Accounts: `/accounts /myaccount /logout /cancel` ·
Audio: `/record [id] /stoprecord /join [id] /leave [id] /leaveall /rejoin /mute /unmute /sendfile /setrecordgroup /list /status` ·
Effects (no limits): `/level /bass /treble /gain /overdrive /echo /effects /reset` ·
Utility: `/ping /stats /id /panel /menu /help` ·
Owner: `/users /pending /approve /remove /ban /unban /broadcast /restart`

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
