"""Step 5 - Upload video to YouTube."""

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)


def run(script: dict) -> dict:
    log.info("=== STEP 5: Upload ===")

    video_path = script.get("final_video")
    if not video_path or not Path(video_path).exists():
        log.error("No video file to upload")
        return script

    try:
        import pickle
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request

        SCOPES      = ["https://www.googleapis.com/auth/youtube.upload"]
        TOKEN_FILE  = "config/youtube_token.pickle"
        SECRET_FILE = "config/client_secrets.json"

        creds = None
        if Path(TOKEN_FILE).exists():
            with open(TOKEN_FILE, "rb") as f:
                creds = pickle.load(f)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(SECRET_FILE, SCOPES)
                creds = flow.run_local_server(port=0)
            with open(TOKEN_FILE, "wb") as f:
                pickle.dump(creds, f)

        youtube = build("youtube", "v3", credentials=creds)

        body = {
            "snippet": {
                "title":       script.get("title", "Top 10 Facts")[:100],
                "description": script.get("description", "")[:5000],
                "tags":        script.get("tags", [])[:500],
                "categoryId":  "22"
            },
            "status": {"privacyStatus": "public"}
        }

        media = MediaFileUpload(video_path, chunksize=-1, resumable=True,
                                mimetype="video/mp4")
        req   = youtube.videos().insert(part=",".join(body.keys()),
                                        body=body, media_body=media)

        response = None
        while response is None:
            status, response = req.next_chunk()
            if status:
                pct = int(status.progress() * 100)
                log.info(f"Upload progress: {pct}%")

        vid_id = response.get("id")
        url    = f"https://www.youtube.com/shorts/{vid_id}"
        log.info(f"Uploaded: {url}")
        script["youtube_url"] = url

    except Exception as e:
        err = str(e)
        if "quotaExceeded" in err or "rateLimitExceeded" in err or "429" in err:
            log.error(
                "YouTube daily upload quota exceeded. "
                "Limit resets at midnight Pacific time. "
                "The video is saved and will upload next run."
            )
        elif "expired" in err.lower() or "invalid_grant" in err.lower():
            log.error(
                "YouTube token expired. "
                "Delete config/youtube_token.pickle and re-run to re-authenticate."
            )
        else:
            log.error(f"Upload failed: {e}")

    if script.get("script_path"):
        with open(script["script_path"], "w", encoding="utf-8") as f:
            json.dump(script, f, indent=2, ensure_ascii=False)

    return script
