import os
import subprocess
import sys
import time
import io
import json
import re
import warnings
import asyncio
from PIL import Image, ImageDraw, ImageFont
from playwright.async_api import async_playwright
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
OUTPUT_FOLDER_ID = '1y_aDL7D3ozFVdrUQY6XOZvHD8hZK-yVd'
INPUT_FOLDER_ID = '1G7nC7CrMi_8HdtVGxdR-aNdak9FrVAcd'

# --- Grid Configuration ---
GRID_COLS = 5
GRID_ROWS = 8

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

async def resolve_any_link(input_url):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
        context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
        page = await context.new_page()
        
        hunt = {"master": None, "big_url": None, "big_size": 0}

        async def handle_response(response):
            nonlocal hunt
            u = response.url.split('?')[0].lower()
            try:
                h = response.headers
                ctype = h.get("content-type", "").lower()
                size = int(h.get("content-length", 0))

                if "master" in u and ".m3u8" in u:
                    hunt["master"] = response.url
                elif not any(bad in ctype for bad in ["image", "javascript", "css", "font", "html"]):
                    if size > hunt["big_size"]:
                        hunt["big_size"] = size
                        hunt["big_url"] = response.url
            except: pass

        page.on("response", handle_response)
        try:
            try:
                await page.goto(input_url, wait_until="domcontentloaded", timeout=25000)
            except: pass
            
            await asyncio.sleep(4)
            await page.mouse.click(960, 540)
            await asyncio.sleep(8)

            final_link = hunt["master"] or hunt["big_url"]
            cookies = await context.cookies()
            cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
            return final_link, cookie_str
        finally:
            await browser.close()

def get_video_metadata(video_path):
    try:
        cmd = [
            'ffprobe', '-v', 'error',
            '-select_streams', 'v:0',
            '-show_entries', 'format=duration,size:stream=width,height',
            '-of', 'json', video_path
        ]
        out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
        data = json.loads(out)
        
        stream = data.get('streams', [{}])[0]
        fmt = data.get('format', {})
        
        width = int(stream.get('width', 1920))
        height = int(stream.get('height', 1080))
        duration = float(fmt.get('duration', 0.0))
        size_bytes = float(fmt.get('size', 0.0))
        
        if duration == 0.0 and 'duration' in stream:
            try: duration = float(stream['duration'])
            except: pass

        return {
            "width": width, 
            "height": height, 
            "duration": duration, 
            "size_mb": size_bytes / (1024 * 1024)
        }
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
        
        # 1. Fetch all video files directly from the Input Folder
        print("🔍 Scanning Input Folder for video assets...", flush=True)
        video_files = []
        page_token = None
        
        query_parts = [
            f"'{INPUT_FOLDER_ID}' in parents",
            "trashed = false",
            "mimeType contains 'video'"
        ]
        folder_query = " and ".join(query_parts)

        while True:
            response = service.files().list(
                q=folder_query,
                fields="nextPageToken, files(id, name)",
                pageSize=1000,
                pageToken=page_token
            ).execute()
            
            video_files.extend(response.get('files', []))
            page_token = response.get('nextPageToken', None)
            if not page_token:
                break

        if not video_files:
            print("⚠️ No video files discovered in the input folder. Exiting.", flush=True)
            sys.exit(0)

        # Sort files alphabetically by name to keep execution predictable
        video_files.sort(key=lambda x: x['name'].lower())
        print(f"🎯 Found {len(video_files)} video files to process.", flush=True)

        file_count = 0

        for file_info in video_files:
            video_name = file_info['name']
            drive_video_id = file_info['id']

            if "---" in drive_video_id:
                drive_video_id = drive_video_id.split("---")[0].strip()
            
            file_count += 1
            display_name = f"File [{file_count}/{len(video_files)}]"
            print(f"\n========================================")
            print(f"🎬 Processing: {display_name}", flush=True)

            # Generate the corresponding output image name
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

            local_temp_video = f"temp_{file_count}.mp4"
            local_output_image = f"grid_{file_count}.jpg"

            # --- COMBINED DOWNLOADING BLOCK (SUPPORTING COPIED ONLINE SCRAPING) ---
            raw_input = drive_video_id.strip()
            download_success = False

            if raw_input.startswith("http"):
                print(f"🕵️ Analyzing web source for preview generation...", flush=True)
                max_download_attempts = 3

                for dl_attempt in range(1, max_download_attempts + 1):
                    print(f"🔄 [Attempt {dl_attempt}/{max_download_attempts}] Scraping fresh link and starting download...", flush=True)
                    if os.path.exists(local_temp_video):
                        os.remove(local_temp_video)

                    bracket_match = re.match(r"(.+?)\[(\d+)\]$", raw_input)
                    
                    try:
                        if bracket_match:
                        folder_url = bracket_match.group(1)
                        target_index = int(bracket_match.group(2)) - 1
                        file_list = []
                        
                        # 1. Store the cookies in a dictionary object
                        shared_data = {"session_cookies": ""}
                        
                        async def scrape_folder():
                            # ─── REMOVE THE 'nonlocal session_cookies' LINE FROM HERE ───
                            fl = []
                            async with async_playwright() as p:
                                browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
                                context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
                                page = await context.new_page()
                                
                                async def handle_response(resp):
                                    if "api.gofile.io/contents" in resp.url:
                                        try:
                                            res_data = json.loads(await resp.text())
                                            if res_data.get("status") == "ok":
                                                children = res_data.get("data", {}).get("children", {})
                                                for item in children.values():
                                                    if item.get("type") == "file":
                                                        fl.append({"name": item.get("name"), "link": item.get("link")})
                                        except: pass

                                page.on("response", handle_response)
                                try:
                                    async with page.expect_response(lambda r: "api.gofile.io/contents" in r.url, timeout=30000):
                                        await page.goto(folder_url, wait_until="commit", timeout=30000)
                                    await asyncio.sleep(2) 
                                    cookies = await context.cookies()
                                    
                                    # 2. Write straight to the dictionary object
                                    shared_data["session_cookies"] = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
                                finally:
                                    await browser.close()
                                    
                            fl.sort(key=lambda x: x["name"].lower())  
                            return fl

                        file_list = asyncio.run(scrape_folder())
                        
                        # 3. Read it back out to use in headers
                        session_cookies = shared_data["session_cookies"]
                        
                        if not file_list or target_index >= len(file_list):
                            print(f"⚠️ Scraping failed or index out of bounds on attempt {dl_attempt}.", flush=True)
                            time.sleep(5)
                            continue
                            
                        selected_file = file_list[target_index]
                        resolved_link = selected_file["link"]
                        headers = f"Referer: {folder_url}\r\nUser-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)\r\n"
                        if session_cookies:
                            headers += f"Cookie: {session_cookies}\r\n"
                        else:
                            resolved_link, session_cookies = asyncio.run(resolve_any_link(raw_input))
                            if not resolved_link:
                                print(f"⚠️ Playwright failed to resolve stream on attempt {dl_attempt}.", flush=True)
                                time.sleep(5)
                                continue

                            headers = f"Referer: {raw_input}\r\nUser-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)\r\n"
                            if session_cookies:
                                headers += f"Cookie: {session_cookies}\r\n"

                        raw_download_cmd = [
                            'ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                            '-reconnect', '1', '-reconnect_at_eof', '1', '-reconnect_streamed', '1',
                            '-headers', headers,
                            '-i', resolved_link,
                            '-c', 'copy', '-bsf:a', 'aac_adtstoasc', '-movflags', 'faststart', local_temp_video
                        ]
                        
                        DOWNLOAD_TIMEOUT = 100 
                        print(f"📥 FFMPEG downloading stream (Timeout guard: {DOWNLOAD_TIMEOUT}s)...", flush=True)
                        result = subprocess.run(raw_download_cmd, timeout=DOWNLOAD_TIMEOUT)
                        
                        if result.returncode == 0 and os.path.exists(local_temp_video) and os.path.getsize(local_temp_video) > 10000:
                            print(f"✅ Successfully grabbed file on attempt {dl_attempt}!", flush=True)
                            download_success = True
                            break
                        else:
                            print(f"⚠️ FFMPEG exited with error code {result.returncode} on attempt {dl_attempt}.", flush=True)
                            
                    except subprocess.TimeoutExpired:
                        print(f"⚠️ FFMPEG hung up and hit the {DOWNLOAD_TIMEOUT}s timeout wall on attempt {dl_attempt}.", flush=True)
                    except Exception as e:
                        print(f"⚠️ Unexpected error during scraping/download phase: {e}", flush=True)
                    
                    print("⏳ Cool-down before hitting the host again...", flush=True)
                    time.sleep(10)

                if not download_success:
                    if os.path.exists(local_temp_video): os.remove(local_temp_video)
                    print(f"❌ FAILED: Unable to download payload for {display_name} after full link-reset retries.", flush=True)
                    continue
            else:
                # Fallback standard Google Drive download logic
                print(f"📥 Downloading video file payload asset from Drive...", flush=True)
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
                    download_success = True
                except Exception as e:
                    print(f"❌ Download Failed: {e}", flush=True)
                    if os.path.exists(local_temp_video): os.remove(local_temp_video)
                    continue

            # 5. Extract timeline spacing positions and paint the contact sheet grid image canvas
            print(f"📸 Generating screenlist image canvas layout...", flush=True)
            success = generate_local_contact_sheet(local_temp_video, local_output_image, cols=GRID_COLS, rows=GRID_ROWS)
            
            if os.path.exists(local_temp_video): 
                os.remove(local_temp_video)

            if not success:
                print(f"❌ Failed to parse frames or video metadata metrics.", flush=True)
                if os.path.exists(local_output_image): os.remove(local_output_image)
                continue

            # 6. Upload the finished grid preview frame asset to the output folder
            print(f"📤 Uploading final grid preview sheet -> {display_name}", flush=True)
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

        print("\n✅ ALL VIDEOS IN THE INPUT FOLDER PROCESSED.", flush=True)
        sys.exit(0)

    except Exception as e:
        print(f"💥 Critical Failure: {e}")
        sys.exit(99)
