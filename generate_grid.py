import os
import subprocess
import sys
import time
import io
import json
import re
import warnings
from PIL import Image, ImageDraw, ImageFont
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload
from google.oauth2.credentials import Credentials as UserCredentials
from google.auth.transport.requests import Request

# Start a timer
START_TIME = time.time()
TIMEOUT_LIMIT = 20000 

warnings.filterwarnings("ignore", category=FutureWarning)

# --- CONFIG (From Secrets/Environment) ---
DRIVE_TOKEN = os.environ.get('DRIVE_TOKEN')
INPUT_FOLDER_ID = '1G7nC7CrMi_8HdtVGxdR-aNdak9FrVAcd'
OUTPUT_FOLDER_ID = '14KAhaiTisjuybP2Pc6mcbLau8JoyDq5y'
CONFIG_FILE_ID = '1rE51zdRaXCIrxmWZhRjRZaKIuRvadDo3'

# --- Grid Configuration ---
GRID_COLS = 3
GRID_ROWS = 6

def get_drive_service():
    raw = DRIVE_TOKEN.strip()
    if (raw.startswith("'") or raw.startswith('"')): raw = raw[1:-1]
    
    try: data = json.loads(raw)
    except: data = eval(raw)

    if 'expiry' in data and isinstance(data['expiry'], str):
        del data['expiry']

    creds = UserCredentials(
        token=data.get('token'),
        refresh_token=data.get('refresh_token'),
        token_uri=data.get('token_uri'),
        client_id=data.get('client_id'),
        client_secret=data.get('client_secret')
    )
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            print("Token expired. Refreshing...")
            creds.refresh(Request())
        else:
            raise Exception("Token is invalid and no refresh token found!")
    
    return build('drive', 'v3', credentials=creds)

def format_time(seconds):
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    return f"{hrs:02d}:{mins:02d}:{secs:02d}"

def get_video_metadata(video_path):
    try:
        cmd = [
            'ffprobe', '-v', 'error', 
            '-show_entries', 'format=duration,size:stream=width,height', 
            '-of', 'csv=p=0', video_path
        ]
        out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip().split('\n')
        width, height = map(int, out[0].split(','))
        duration, size_bytes = map(float, out[1].split(','))
        return {"width": width, "height": height, "duration": duration, "size_mb": size_bytes / (1024 * 1024)}
    except Exception as e:
        print(f"❌ Error probing metadata: {e}")
        return None

def generate_local_contact_sheet(video_path, output_img, cols=4, rows=4):
    meta = get_video_metadata(video_path)
    if not meta: return False

    total_thumbs = cols * rows
    duration = meta["duration"]
    start_margin = duration * 0.05
    end_margin = duration * 0.95
    interval = (end_margin - start_margin) / (total_thumbs - 1) if total_thumbs > 1 else 0

    temp_frames = []
    for i in range(total_thumbs):
        timestamp = start_margin + (i * interval)
        time_str = format_time(timestamp)
        temp_thumb = f"temp_thumb_{i}.jpg"
        
        ffmpeg_cmd = [
            'ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
            '-ss', str(timestamp), '-i', video_path, '-vframes', '1', '-q:v', '3', temp_thumb
        ]
        subprocess.run(ffmpeg_cmd)
        
        if os.path.exists(temp_thumb):
            temp_frames.append((temp_thumb, time_str))

    if not temp_frames: return False

    thumb_width = 320
    aspect_ratio = meta["width"] / meta["height"]
    thumb_height = int(thumb_width / aspect_ratio)
    grid_gap, header_height, padding = 6, 85, 10
    
    grid_width = (cols * thumb_width) + ((cols - 1) * grid_gap) + (padding * 2)
    grid_height = (rows * thumb_height) + ((rows - 1) * grid_gap) + header_height + padding

    canvas = Image.new("RGB", (grid_width, grid_height), color=(245, 245, 245))
    draw = ImageDraw.Draw(canvas)
    
    try: font = ImageFont.load_default()
    except: font = None

    info_text = f"File: {os.path.basename(video_path)}\nSize: {meta['size_mb']:.2f} MB | Resolution: {meta['width']}x{meta['height']}\nDuration: {format_time(duration)}"
    draw.text((padding, 15), info_text, fill=(30, 30, 30), font=font)

    for idx, (thumb_path, time_stamp) in enumerate(temp_frames):
        c, r = idx % cols, idx // cols
        x_pos = padding + (c * (thumb_width + grid_gap))
        y_pos = header_height + (r * (thumb_height + grid_gap))
        
        with Image.open(thumb_path) as img:
            resized_thumb = img.resize((thumb_width, thumb_height), Image.Resampling.LANCZOS)
            canvas.paste(resized_thumb, (x_pos, y_pos))
            draw.text((x_pos + 5, y_pos + thumb_height - 15), time_stamp, fill=(255, 255, 255), font=font)
        os.remove(thumb_path)

    canvas.save(output_img, "JPEG", quality=92)
    return True

if __name__ == "__main__":
    try:
        service = get_drive_service()
        
        # 1. Download the list of target filenames from the config file
        print("📥 Fetching configuration name list from Drive...", flush=True)
        fh = io.BytesIO()
        MediaIoBaseDownload(fh, service.files().get_media(fileId=CONFIG_FILE_ID)).next_chunk()
        config_lines = fh.getvalue().decode().splitlines()

        file_count = 0

        for line in config_lines:
            video_name = line.strip()
            # Ignore empty lines or comments
            if not video_name or video_name.startswith('#'): continue
            
            file_count += 1
            display_name = f"File [{file_count}] -> {video_name}"
            print(f"\n========================================")
            print(f"🎬 Processing: {display_name}", flush=True)

            # Generate the corresponding output image name (e.g., "video.mp4" -> "video_preview.jpg")
            base_name, _ = os.path.splitext(video_name)
            output_image_name = f"{base_name}_preview.jpg"
            safe_q_name = output_image_name.replace("'", "\\'")

            # 2. Skip Check: See if this preview image asset already exists in the output folder
            try:
                q_check = f"name = '{safe_q_name}' and '{OUTPUT_FOLDER_ID}' in parents and trashed = false"
                check = service.files().list(q=q_check, fields="files(id)").execute().get('files', [])
                if check:
                    print(f"⏩ SKIPPING: Preview grid already exists in Output Folder.", flush=True)
                    continue
            except Exception as e:
                print(f"⚠️ Skip Check API Error: {e}", flush=True)

            # Timeout break check safely before executing downloads
            if time.time() - START_TIME > TIMEOUT_LIMIT:
                print("\n⏳ TIMEOUT REACHED. Exiting for workflow continuation...", flush=True)
                sys.exit(99)

            # 3. Search for the source video file in the input folder
            search_name = video_name.replace("'", "\\'")
            drive_video_id = None
            try:
                query = f"name = '{search_name}' and '{INPUT_FOLDER_ID}' in parents and trashed = false"
                res = service.files().list(q=query, fields="files(id)").execute().get('files', [])
                if res:
                    drive_video_id = res[0]['id']
            except Exception as e:
                print(f"❌ Search Query Error: {e}", flush=True)

            if not drive_video_id:
                print(f"❌ Error: '{video_name}' not found inside Input Folder. Skipping.", flush=True)
                continue

            # 4. Stream down the target video locally to pull snapshots from
            local_temp_video = f"temp_{file_count}.mp4"
            local_output_image = f"grid_{file_count}.jpg"

            print(f"📥 Downloading video file payload asset...", flush=True)
            try:
                request = service.files().get_media(fileId=drive_video_id)
                with io.FileIO(local_temp_video, 'wb') as f_handle:
                    downloader = MediaIoBaseDownload(f_handle, request, chunksize=10*1024*1024)
                    done = False
                    while not done:
                        status, done = downloader.next_chunk()
                        if status:
                            print(f"📥 Download Progress: {int(status.progress()*100)}%", end='\r', flush=True)
                print(f"\n💾 Download complete.", flush=True)
            except Exception as e:
                print(f"❌ Download Failed: {e}", flush=True)
                if os.path.exists(local_temp_video): os.remove(local_temp_video)
                continue

            # 5. Extract timeline spacing positions and paint the contact sheet grid image canvas
            print(f"📸 Generating screenlist image canvas layout...", flush=True)
            success = generate_local_contact_sheet(local_temp_video, local_output_image, cols=GRID_COLS, rows=GRID_ROWS)
            
            # Remove video payload immediately after snapshot processing to free up disk space
            if os.path.exists(local_temp_video): 
                os.remove(local_temp_video)

            if not success:
                print(f"❌ Failed to parse frames or video metadata metrics.", flush=True)
                if os.path.exists(local_output_image): os.remove(local_output_image)
                continue

            # 6. Upload the finished grid preview frame asset to the output folder
            print(f"📤 Uploading final grid preview sheet -> {output_image_name}", flush=True)
            try:
                media = MediaFileUpload(local_output_image, mimetype='image/jpeg', resumable=True)
                request = service.files().create(
                    body={'name': output_image_name, 'parents': [OUTPUT_FOLDER_ID]}, 
                    media_body=media
                )
                response = None
                while response is None:
                    status, response = request.next_chunk()
                    if status:
                        print(f"⬆️ Uploading Image: {int(status.progress() * 100)}%", end='\r', flush=True)
                print(f"\n🚀 PREVIEW SHEET SAVED SUCCESSFUL!", flush=True)
            except Exception as e:
                print(f"⚠️ Upload Failed: {e}", flush=True)
            finally:
                if os.path.exists(local_output_image): 
                    os.remove(local_output_image)

        print("\n✅ ALL FILENAMES FROM CONFIG FILE PROCESSED.", flush=True)
        sys.exit(0)

    except Exception as e:
        print(f"💥 Critical Failure: {e}")
        sys.exit(99)
