# 🎬 VIP Video Downloader Bot (Render Ready)

A modern, high-speed Python Telegram Bot that extracts video metadata, direct download links, and cloud streaming options from **TeraBox**, **DiskWala**, and **Flezen** links. Built with an integrated `aiohttp` health-check server to run 24/7 on **Render.com Free Web Services** without port-scan timeout shutdowns.

---

## ⚙️ Commands

```
/exp <link>   Reliable & Fast [Recommended]
/expHD <link> For HD Videos [Slow]
/get <link>   Unstable [Use for small files]
/dw <link>    Download Diskwala video
/fz <link>    Download Flezen video
/all <link>   Auto-detect link type
```

---

## 🎥 Video-Only Playback Design

- **Playable Videos Only**: Media is delivered strictly as a native Telegram video player with built-in streaming support (`supports_streaming=True`).
- **Telemetry Timing Caption**:
  ```
  📦 filename.mp4
  📐 Size: 126.5 KB

  ⬇️ Download: 2s
  📩 Upload: 2s
  ⏱ Total: 6s
  ```

---

## 🚀 Step-by-Step Guide to Host on Render (https://render.com/)

### Step 1: Push Code to GitHub
```bash
git init
git add .
git commit -m "Video Downloader Bot Render Ready"
git remote add origin https://github.com/<your-username>/<your-repo-name>.git
git branch -M main
git push -u origin main
```

### Step 2: Deploy on Render.com
1. Go to [Render Dashboard](https://dashboard.render.com/) and sign in.
2. Click **New +** → Select **Web Service**.
3. Connect your GitHub repository.
4. Set the following configurations:
   - **Name:** `video-downloader-bot`
   - **Runtime:** `Python 3`
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python main.py`
   - **Instance Type:** `Free`
5. Under **Environment Variables**, add:
   - `BOT_TOKEN`: `8297267475:AAHQ5uvwsH4tQS9gVPnHeHGciPtcagU5Big` (or your BotFather token)
   - `PORT`: `10000`
   - *(Optional)* `TERABOX_COOKIE`: Your `ndus` session cookie from terabox.com
6. Click **Create Web Service**.

Render will deploy the service, start the background polling, and keep the server healthy via `/health`.
