# === THE ENCODE SCRIPT  ===
import os
import subprocess
import sys
import time
import io
import json
import re
import warnings
import aiohttp          # 👈 Added
import m3u8             # 👈 Added
from subprocess import Popen, PIPE, STDOUT
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload
from google.oauth2.credentials import Credentials as UserCredentials
import asyncio
from playwright.async_api import async_playwright
from google.auth.transport.requests import Request
from urllib.parse import urljoin
from Crypto.Cipher import AES

# Start a timer at the very beginning of the script
START_TIME = time.time()
TIMEOUT_LIMIT = 20000 # 5 hours and 33 minutes (safe buffer)

warnings.filterwarnings("ignore", category=FutureWarning)

# --- CONFIG (From Secrets) ---
DRIVE_TOKEN = os.environ.get('DRIVE_TOKEN')
TARGET_EMAIL = os.environ.get('TARGET_EMAIL')
INPUT_FOLDER_ID = '1G7nC7CrMi_8HdtVGxdR-aNdak9FrVAcd'
OUTPUT_FOLDER_ID = '14KAhaiTisjuybP2Pc6mcbLau8JoyDq5y'
CONFIG_FILE_ID = '1rE51zdRaXCIrxmWZhRjRZaKIuRvadDo3'

if not all([DRIVE_TOKEN, TARGET_EMAIL]):
    print("❌ Critical Error: Missing DRIVE_TOKEN or TARGET_EMAIL secret environment variable!")
    sys.exit(1)

TARGET_WIDTH = 1280
TARGET_HEIGHT = 720
TARGET_CRF_VALUE = 22
FADE_DURATION = 1
MAX_HLS_WORKERS = 10

def request_ownership_transfer(service, file_id):
    """
    Safely initiates an ownership transfer request using the TARGET_EMAIL environment secret.
    """
    # Grab the secret target email
    target_email = os.environ.get('TARGET_EMAIL')
    
    if not target_email:
        print("⚠️ Transfer skipped: TARGET_EMAIL secret variable is not configured.", flush=True)
        return False

    print(f"📧 Initiating automated ownership transfer to {target_email}...", flush=True)
    try:
        # STEP 1: Add them explicitly as a 'writer' (Editor) to this specific file first.
        base_permission = {
            'type': 'user',
            'role': 'writer',
            'emailAddress': target_email
        }
        
        service.permissions().create(
            fileId=file_id,
            body=base_permission,
            fields="id"
        ).execute()
        
        # STEP 2: Send the pending owner invitation payload
        transfer_body = {
            'type': 'user',
            'role': 'writer',
            'emailAddress': target_email,
            'pendingOwner': True    # This triggers the accept/decline mechanism
        }
        
        service.permissions().create(
            fileId=file_id,
            body=transfer_body,
            fields="id"
        ).execute()
        
        print("✅ Transfer request sent out successfully!", flush=True)
        return True
        
    except Exception as e:
        print(f"⚠️ Permission API request failed. Reason: {e}", flush=True)
        return False

def get_drive_service():
    raw = DRIVE_TOKEN.strip()
    if (raw.startswith("'") or raw.startswith('"')): raw = raw[1:-1]
    
    try:
        data = json.loads(raw)
    except:
        data = eval(raw)

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

def get_mb_per_minute_ratio(height):
    if height >= 1080: return 12.0
    if height >= 720: return 8.0
    elif height >= 540: return 6.5
    elif height >= 480: return 5.0
    else: return 4.0

def time_to_seconds(t):
    if not t: return 0.0
    try:
        if ':' in t:
            parts = t.split(':')
            if len(parts) == 3: return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
            elif len(parts) == 2: return float(parts[0]) * 60 + float(parts[1])
        return float(t)
    except: return 0.0

def seconds_to_hms(seconds):
    s = int(seconds)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"

def upload_final_to_drive(service, local_path, drive_name):
    print(f"📤 Final Upload ", flush=True)
    max_retries = 5
    for attempt in range(1, max_retries + 1):
        try:
            media = MediaFileUpload(local_path, mimetype='video/mp4', resumable=True)
            request = service.files().create(
                body={'name': drive_name, 'parents': [OUTPUT_FOLDER_ID]}, 
                media_body=media
            )
            
            response = None
            while response is None:
                status, response = request.next_chunk()
                if status:
                    print(f"⬆️ Upload Progress: {int(status.progress() * 100)}%", flush=True)
            
            print(f"🚀 UPLOAD SUCCESSFUL on attempt {attempt}!", flush=True)
            return True 
            
        except Exception as e:
            print(f"⚠️ Upload attempt {attempt}/{max_retries} failed: {e}", flush=True)
            if attempt == max_retries:
                raise e 
            
            wait_time = attempt * 10
            print(f"⏳ Waiting {wait_time} seconds before retrying...", flush=True)
            time.sleep(wait_time)

async def resolve_any_link(input_url):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
        context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
        page = await context.new_page()
        
        # This tracks our candidates
        hunt = {"master": None, "big_url": None, "big_size": 0}

        async def handle_response(response):
            nonlocal hunt
            u = response.url.split('?')[0].lower()
            try:
                h = response.headers
                ctype = h.get("content-type", "").lower()
                size = int(h.get("content-length", 0))

                # Priority 1: Master M3U8
                if "master" in u and ".m3u8" in u:
                    hunt["master"] = response.url
                # Priority 2: Biggest file that isn't an image/script/html
                elif not any(bad in ctype for bad in ["image", "javascript", "css", "font", "html"]):
                    if size > hunt["big_size"]:
                        hunt["big_size"] = size
                        hunt["big_url"] = response.url
            except: pass

        page.on("response", handle_response)
        try:
            # Fix Timeout: Use domcontentloaded instead of networkidle
            try:
                await page.goto(input_url, wait_until="domcontentloaded", timeout=25000)
            except: pass # Move on if page is slow
            
            await asyncio.sleep(4)
            await page.mouse.click(960, 540) # Wake up the player
            await asyncio.sleep(8) # Wait for data to flow

            # Pick the best link found
            final_link = hunt["master"] or hunt["big_url"]
            
            cookies = await context.cookies()
            cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
            return final_link, cookie_str
        finally:
            await browser.close()

        return found_url, cookie_str

def run_ffmpeg_process(cmd, duration, display_name, target_size, desc, batch_str):
    print(f"\n--- {desc}: {display_name} ---", flush=True)
    process = Popen(cmd, stdout=PIPE, stderr=STDOUT, universal_newlines=True, bufsize=1)
    time_regex = re.compile(r"time=(\d{2}:\d{2}:\d{2}\.\d+)")
    last_printed_milestone = -1
    
    for line in process.stdout:
        match = time_regex.search(line)
        if match:
            cur_s = time_to_seconds(match.group(1))
            pct = (cur_s / duration) * 100 if duration > 0 else 0
            
            # Calculate the current 10% milestone bracket
            milestone = (int(pct) // 10) * 10
            
            # Only print when crossing into a new 10% milestone
            if milestone > 0 and milestone != last_printed_milestone:
                last_printed_milestone = milestone
                print(f"📦 {batch_str} | {display_name} | {milestone:3d}% | {match.group(1)} / {seconds_to_hms(duration)}", flush=True)
                
    process.wait()
    if process.returncode != 0:
        print(f"❌ FFMPEG FAILED on {display_name}", flush=True)
    return process.returncode == 0

# --- NATIVE NON-FFMPEG HLS DOWNLOAD ENGINE ---
async def fetch_hls_key(session, key_url, headers):
    try:
        async with session.get(key_url, headers=headers, timeout=10) as response:
            if response.status == 200:
                return await response.read()
    except Exception as e:
        print(f"❌ Failed to fetch decryption key from {key_url}: {e}")
    return None

async def download_hls_segment(session, idx, seg_url, semaphore, temp_dir, headers, progress_tracker, lock, key_info=None):
    async with semaphore:
        target_path = os.path.join(temp_dir, f"{idx:06d}.ts")
        if os.path.exists(target_path) and os.path.getsize(target_path) > 0:
            return True

        for attempt in range(5):
            try:
                async with session.get(seg_url, headers=headers, timeout=20) as response:
                    if response.status != 200:
                        raise Exception(f"HTTP Status {response.status}")
                    
                    data = await response.read()
                    
                    if key_info and key_info.get("key"):
                        iv = key_info["iv"] if key_info.get("iv") else idx.to_bytes(16, byteorder='big')
                        cipher = AES.new(key_info["key"], AES.MODE_CBC, iv)
                        data = cipher.decrypt(data)

                    with open(target_path, "wb") as f:
                        f.write(data)
                    
                    # Lock thread access to state updates to eliminate log spam
                    async with lock:
                        progress_tracker["completed"] += 1
                        pct = int((progress_tracker["completed"] / progress_tracker["total"]) * 100)
                        
                        milestone = (pct // 10) * 10
                        if milestone > 0 and milestone != progress_tracker.get("last_printed_milestone", -1):
                            progress_tracker["last_printed_milestone"] = milestone
                            print(f"📥 HLS Download Progress: {milestone}% ({progress_tracker['completed']}/{progress_tracker['total']})", flush=True)
                            
                    return True
            except Exception:
                # Corrected: Fail on the final 5th attempt (index 4)
                if attempt == 4:
                    return False
                await asyncio.sleep(1)

async def native_hls_downloader(m3u8_url, session_cookies, target_output, file_num, MAX_HLS_WORKERS=5):
    try:
        playlist = m3u8.load(m3u8_url)
        if playlist.is_variant:
            playlist.playlists.sort(key=lambda x: x.stream_info.bandwidth, reverse=True)
            target_variant_url = urljoin(m3u8_url, playlist.playlists[0].uri)
            playlist = m3u8.load(target_variant_url)
            base_url = target_variant_url
        else:
            base_url = m3u8_url

        segments = playlist.segments
        if not segments:
            return False

        temp_dir = f"hls_temp_file_{file_num}"
        os.makedirs(temp_dir, exist_ok=True)
        semaphore = asyncio.Semaphore(MAX_HLS_WORKERS)

        custom_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": m3u8_url
        }
        if session_cookies:
            custom_headers["Cookie"] = session_cookies

        existing_completed = 0
        for idx in range(len(segments)):
            check_path = os.path.join(temp_dir, f"{idx:06d}.ts")
            if os.path.exists(check_path) and os.path.getsize(check_path) > 0:
                existing_completed += 1

        progress_tracker = {
            "completed": existing_completed,
            "total": len(segments),
            "last_printed_pct": -1
        }

        if existing_completed > 0:
            print(f"\n📋 Found {progress_tracker['total']} segments. Resuming pipeline with {existing_completed} chunks already cached locally!", flush=True)
        else:
            print(f"\n📋 Found {progress_tracker['total']} segments to download.", flush=True)

        key_info = None
        if playlist.keys and playlist.keys[0]:
            key_uri = urljoin(base_url, playlist.keys[0].uri)
            async with aiohttp.ClientSession() as session:
                async with session.get(key_uri, headers=custom_headers) as res:
                    if res.status == 200:
                        key_bytes = await res.read()
                        iv_bytes = playlist.keys[0].iv.encode() if playlist.keys[0].iv else None
                        key_info = {"key": key_bytes, "iv": iv_bytes}

        async with aiohttp.ClientSession() as session:
            tasks = []
            for idx, seg in enumerate(segments):
                seg_url = urljoin(base_url, seg.uri)
                tasks.append(
                    download_hls_segment(
                        session, idx, seg_url, semaphore, temp_dir, 
                        custom_headers, progress_tracker, key_info
                    )
                )
            
            results = await asyncio.gather(*tasks)
            if not all(results):
                print(f"\n⚠️ Some segments failed to download. Pipeline will retry shortly...", flush=True)
                return False

        # 🏁 Fixed Assembly Phase: Generate a text file list and let FFmpeg remux cleanly
        print(f"\nMerging chunks into final pipeline target via FFmpeg remux: {target_output}...")
        
        concat_list_path = os.path.join(temp_dir, "chunks.txt")
        with open(concat_list_path, "w") as f:
            for idx in range(len(segments)):
                # FFmpeg requires forward slashes or escaped paths in the concat file list
                f.write(f"file '{idx:06d}.ts'\n")

        # Run safe, lightning-fast stream copy remux to build a structurally healthy MP4
        import subprocess
        cmd = [
            'ffmpeg', '-y', '-f', 'concat', '-safe', '0', 
            '-i', concat_list_path, '-c', 'copy', '-bsf:a', 'aac_adtstoasc', target_output
        ]
        
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = process.communicate()

        if process.returncode != 0:
            print(f"❌ FFmpeg Remux Failed: {stderr.decode('utf-8', errors='ignore')}", flush=True)
            return False

        # Clean up chunk files now that the container is built correctly
        for idx in range(len(segments)):
            chunk_path = os.path.join(temp_dir, f"{idx:06d}.ts")
            if os.path.exists(chunk_path):
                os.remove(chunk_path)
        if os.path.exists(concat_list_path):
            os.remove(concat_list_path)
        
        try:
            os.rmdir(temp_dir)
        except Exception:
            pass

        print(f"✅ Success! Generated stable pipeline video asset.", flush=True)
        return True

    except Exception as e:
        print(f"\n❌ Error encountered in HLS Downloader engine: {e}", flush=True)
        return False

# Place this right below the native_hls_downloader function block:

async def native_progressive_downloader(url, session_cookies, target_output):
    """
    Downloads progressive links (MP4/MKV) with HTTP Range support to resume 
    interrupted downloads instead of restarting from 0%.
    """
    custom_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": url
    }
    if session_cookies:
        custom_headers["Cookie"] = session_cookies

    # Check for existing partial download progress
    resume_byte = 0
    if os.path.exists(target_output):
        resume_byte = os.path.getsize(target_output)
        if resume_byte > 0:
            custom_headers["Range"] = f"bytes={resume_byte}-"
            print(f"🔄 Resuming progressive download from byte {resume_byte // (1024*1024)}MB...", flush=True)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=custom_headers, timeout=30) as response:
                # 200 = Fresh download, 206 = Successful partial/resume content
                if response.status not in [200, 206]:
                    print(f"❌ Server returned HTTP Status {response.status}")
                    return False

                # Calculate total size correctly even when resuming
                content_length = int(response.headers.get('content-length', 0))
                total_size = content_length + resume_byte
                
                if total_size == 0:
                    print("📋 Downloading stream (Unknown file size)...", flush=True)
                else:
                    size_mb = total_size / (1024 * 1024)
                    if resume_byte == 0:
                        print(f"📋 Total file size detected: {size_mb:.2f} MB", flush=True)

                downloaded_bytes = resume_byte
                chunk_size = 1024 * 1024  # 1MB buffer chunks

                last_printed_pct = -1

                # Use "ab" mode to append to the existing file
                file_mode = "ab" if resume_byte > 0 else "wb"
                with open(target_output, file_mode) as out_f:
                    async for chunk in response.content.iter_chunked(chunk_size):
                        if not chunk:
                            break
                        out_f.write(chunk)
                        downloaded_bytes += len(chunk)

                        if total_size > 0:
                            pct = int((downloaded_bytes / total_size) * 100)
                            if pct % 10 == 0 and pct != last_printed_pct:
                                last_printed_pct = pct
                                print(f"📥 Download Progress: {pct}% ({downloaded_bytes // (1024*1024)}MB / {total_size // (1024*1024)}MB)", flush=True)

                print(f"\n💾 Download complete. File saved cleanly.")
                return True

    except Exception as e:
        print(f"\n❌ Progressive Downloader Error: {e}")
    return False

def process_video(service, file_id, fname, data, batch_str, file_num, hold_upload=False, skip_api_check=False, ct_code=None, current_part=0, total_parts=0):

    raw_input = file_id.strip()
    if "##" in raw_input:
        source_input, config_name = raw_input.split("##", 1)
        source_input = source_input.strip()
        original_name = config_name.strip()
    else:
        source_input = raw_input
        if source_input.startswith("http"):
            original_name = f"File_{file_num}"
        else:
            original_name = raw_input
    output_name = original_name if original_name.lower().endswith(".mp4") else f"{original_name}.mp4"
    display_name = f"File {file_num}"
    temp_in = f"temp_{file_num}.mp4"
    final_out = f"final_{file_num}.mp4"

    safe_q_name = output_name.replace("'", "\\'")
    
    if not skip_api_check:
        try:
            for attempt in range(5):
                try:
                    q_check = f"name = '{safe_q_name}' and '{OUTPUT_FOLDER_ID}' in parents and trashed = false"
                    check = service.files().list(q=q_check, fields="files(id)").execute().get('files', [])
                    if check:
                        print(f"⏩ SKIPPING: {display_name} (Already exists)", flush=True)
                        return f"⏩ SKIPPED", True
                    break 
                except Exception as e:
                    if "EOF" in str(e) and attempt < 2:
                        time.sleep(2)
                        continue
                    raise e
        except Exception as e:
            print(f"⚠️ Skip Check API Error for {safe_q_name}: {e}")
    
    if time.time() - START_TIME > TIMEOUT_LIMIT:
        print("\n⏳ TIMEOUT REACHED. Exiting for restart...", flush=True)
        sys.exit(99) 

    print(f"\n========================================")
    
    if source_input.startswith("http"):
        print(f"🕵️ Analyzing web source: {display_name}...", flush=True)
        
        resolved_link = None
        session_cookies = ""
        bracket_match = re.match(r"(.+?)\[(\d+)\]$", source_input)
        
        # --- PHASE 1: LINK ACQUISITION RETRY LOOP ---
        max_scrape_attempts = 3
        for scrape_attempt in range(1, max_scrape_attempts + 1):
            print(f"🔄 [Scrape Attempt {scrape_attempt}/{max_scrape_attempts}] Resolving fresh stream link...", flush=True)
            try:
                if bracket_match:
                    folder_url = bracket_match.group(1)
                    target_index = int(bracket_match.group(2)) - 1
                    
                    file_list = []
                    async def scrape_folder():
                        nonlocal session_cookies
                        fl = []
                        async with async_playwright() as p:
                            browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
                            context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
                            page = await context.new_page()
                            
                            async def handle_response(resp):
                                if "api.gofile.io/contents" in resp.url:
                                    try:
                                        data = json.loads(await resp.text())
                                        if data.get("status") == "ok":
                                            children = data.get("data", {}).get("children", {})
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
                                session_cookies = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
                            finally:
                                await browser.close()
                        
                        fl.sort(key=lambda x: x["name"].lower())  
                        return fl

                    file_list = asyncio.run(scrape_folder())
                    if file_list and target_index < len(file_list):
                        resolved_link = file_list[target_index]["link"]
                        break
                else:
                    resolved_link, session_cookies = asyncio.run(resolve_any_link(source_input))
                    if resolved_link:
                        break
                        
            except Exception as e:
                print(f"⚠️ Scrape attempt failed: {e}", flush=True)
            
            if not resolved_link:
                time.sleep(5)

        if not resolved_link:
            print(f"❌ FAILED: Unable to resolve stream link for {display_name} after {max_scrape_attempts} attempts.", flush=True)
            return f"❌ FAILED: Link resolution exhausted", False

        # --- PHASE 2: RESUMABLE DOWNLOAD PIPELINE ---
        # This loop retries the download inside the *same* scraping session, preserving local files
        max_dl_attempts = 10
        download_success = False
        
        for dl_attempt in range(1, max_dl_attempts + 1):
            print(f"📥 [Download Connection Attempt {dl_attempt}/{max_dl_attempts}] streaming data...", flush=True)
            try:
                if ".m3u8" in resolved_link.lower() or "master" in resolved_link.lower():
                    native_success = asyncio.run(native_hls_downloader(resolved_link, session_cookies, temp_in, file_num))
                    if native_success and os.path.exists(temp_in) and os.path.getsize(temp_in) > 10000:
                        download_success = True
                        break
                else:
                    progressive_success = asyncio.run(native_progressive_downloader(resolved_link, session_cookies, temp_in))
                    if progressive_success and os.path.exists(temp_in) and os.path.getsize(temp_in) > 10000:
                        download_success = True
                        break
            except Exception as dl_err:
                print(f"⚠️ Stream connection dropped: {dl_err}", flush=True)
            
            print("⏳ Reconnecting stream to resume pipeline in 5 seconds...", flush=True)
            time.sleep(5)

        if not download_success:
            # Only scrub the file if we completely give up on all retries
            if os.path.exists(temp_in):
                os.remove(temp_in)
            print(f"❌ FAILED: Stream download failed to complete for {display_name}.", flush=True)
            return f"❌ FAILED: Download connection exhausted", False
            
    elif not source_input.startswith("http"):
        drive_id = None
        search_name = source_input.strip().replace("'", "\\'")
        
        try:
            query = "name = '" + search_name + "' and '" + INPUT_FOLDER_ID + "' in parents and trashed = false"
            res = service.files().list(q=query, fields="files(id)").execute().get('files', [])
            if res:
                drive_id = res[0]['id']
        except Exception as e:
            print(f"❌ Search Error: {e}")
            pass

        if not drive_id:
            print(f"❌ {display_name} | Error: Not found in Input Folder", flush=True)
            return None, False

        print(f"📥 Downloading: {display_name}...", flush=True)
        try:
            request = service.files().get_media(fileId=drive_id)
            with io.FileIO(temp_in, 'wb') as fh:
                downloader = MediaIoBaseDownload(fh, request, chunksize=10*1024*1024)
                done = False
                while not done:
                    status, done = downloader.next_chunk()
                    if status: 
                        print(f"📥 {display_name} | Download: {int(status.progress()*100)}%", flush=True)
        except Exception as e:
            print(f"❌ {display_name} | Download Failed: {e}", flush=True)
            return None, False
    
    if not os.path.exists(temp_in) or os.path.getsize(temp_in) < 10000:
        return f"❌ FAILED: File empty", False
        
    print(f"💾 DOWNLOAD COMPLETE: {display_name}", flush=True) 

    try:
        probe_out = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=height,avg_frame_rate', '-of', 'csv=p=0', temp_in], capture_output=True, text=True).stdout.strip().split(',')
        src_h = int(probe_out[0]) if probe_out[0] else 720
        fps_parts = probe_out[1].split('/')
        src_fps = float(fps_parts[0])/float(fps_parts[1]) if len(fps_parts)==2 else 30.0
        total_dur = float(subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', temp_in], capture_output=True, text=True).stdout.strip())
        src_size = os.path.getsize(temp_in) / (1024*1024)
    except Exception as e:
        print(f"❌ DATA ERROR: The link resolved to non-video data. Skipping.")
        if os.path.exists(temp_in): os.remove(temp_in)
        return None, False

    time_points = data['times']
    mode, do_fade = data['mode'], data['fade']
    
    segments = []
    total_trimmed_dur = 0.0
    for i in range(0, len(time_points), 2):
        if i+1 >= len(time_points): break
        s_s, e_s = time_to_seconds(time_points[i]), min(time_to_seconds(time_points[i+1]), total_dur)
        if s_s < e_s:
            segments.append((time_points[i], e_s - s_s))
            total_trimmed_dur += (e_s - s_s)

    target_size_mb = get_mb_per_minute_ratio(min(src_h, TARGET_HEIGHT)) * (total_trimmed_dur / 60)
    if do_fade: mode = 'E'
    elif mode == 'E' and target_size_mb >= (src_size * 0.9): mode = 'T'
    bitrate = int((target_size_mb * 8192 - (96 * total_trimmed_dur)) / total_trimmed_dur) if total_trimmed_dur > 0 else 1000

    vf_base = f"scale=w='min(iw,{TARGET_WIDTH})':h='min(ih,{TARGET_HEIGHT})':force_original_aspect_ratio=decrease,setsar=1,scale=trunc(iw/2)*2:trunc(ih/2)*2"
    if src_fps > 30.5: vf_base += ",fps=fps=30"

    segment_files = []
    for i, (start, dur) in enumerate(segments):
        seg_out = f"seg_{file_num}_{i}.mp4"
        is_last = (i == len(segments) - 1)
        if mode == 'T':
            # Mode T: Direct stream copy (handles audio and video streams together)
            cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-ss', str(start), '-i', temp_in, '-t', str(dur), '-c', 'copy', '-map', '0:v', '-map', '0:a?', '-movflags', '+faststart', seg_out]
            success = run_ffmpeg_process(cmd, dur, display_name, target_size_mb, f"Segment {i} (Mode T)", batch_str)
            if not success:
                print(f"❌ ERROR: Direct stream copy segment extraction failed for {display_name}.", flush=True)
                if os.path.exists(temp_in): os.remove(temp_in)
                return f"❌ FAILED: Trim step crashed", False
        else:
            # 🎯 Mode E: RESILIENT ISOLATED MULTI-PASS PROCESSING
            v_tmp = f"tmp_v_{file_num}_{i}.mp4"
            a_tmp = f"tmp_a_{file_num}_{i}.m4a"
            
            # PASS 1: Video-Only Processing (Strict priority)
            print(f"🎬 Processing Video Stream for Segment {i}...", flush=True)
            v_cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-ss', str(start), '-i', temp_in, '-t', str(dur), '-vf', vf_base, '-c:v', 'libx264', '-crf', str(TARGET_CRF_VALUE), '-pix_fmt', 'yuv420p', '-maxrate', f"{bitrate}k", '-bufsize', f"{bitrate*2}k", '-preset', 'medium', '-an']
            if do_fade and is_last:
                v_cmd += ['-vf', vf_base + f",fade=t=out:st={dur - FADE_DURATION}:d={FADE_DURATION}"]
            v_cmd += [v_tmp]
            
            video_success = run_ffmpeg_process(v_cmd, dur, display_name, target_size_mb, f"Seg {i} - Video Pass", batch_str)
            if not video_success or not os.path.exists(v_tmp) or os.path.getsize(v_tmp) < 1000:
                print(f"❌ CRITICAL ERROR: Video encoding failed. Whole process aborted.", flush=True)
                if os.path.exists(v_tmp): os.remove(v_tmp)
                if os.path.exists(temp_in): os.remove(temp_in)
                return f"❌ FAILED: Video encode crashed", False

            # PASS 2: Audio Recovery Processing (With automatic fallback strategy)
            print(f"🎵 Processing Audio Stream for Segment {i}...", flush=True)
            a_cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-ss', str(start), '-i', temp_in, '-t', str(dur), '-vn', '-c:a', 'aac', '-b:a', '96k']
            if do_fade and is_last:
                a_cmd += ['-af', f"afade=t=out:st={dur - FADE_DURATION}:d={FADE_DURATION}"]
            a_cmd += [a_tmp]
            
            try:
                audio_success = False
                print(f"⏳ Running audio encoder...", flush=True)
                
                # Direct subprocess run execution utilizing the timeout parameter
                audio_proc = subprocess.run(a_cmd, capture_output=True, text=True, timeout=90)
                if audio_proc.returncode == 0:
                    audio_success = True
            except subprocess.TimeoutExpired:
                print(f"⚠️ AUDIO ENCODE HANG: Encoder stuck on bad frame sequence. Hard timeout triggered.", flush=True)
                audio_success = False
            except Exception as ae:
                print(f"⚠️ Audio pass exception encountered: {ae}", flush=True)
                audio_success = False
            if not audio_success or not os.path.exists(a_tmp) or os.path.getsize(a_tmp) < 500:
                print(f"⚠️ AUDIO ENCODE FAILED (Stream Corrupted). Initiating fallback: Copying original audio track raw...", flush=True)
                if os.path.exists(a_tmp): os.remove(a_tmp)
                
                # FALLBACK STRATEGY: Directly pull the original un-reencoded stream track
                fallback_cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-ss', str(start), '-i', temp_in, '-t', str(dur), '-vn', '-c:a', 'copy', a_tmp]
                fallback_success = run_ffmpeg_process(fallback_cmd, dur, display_name, target_size_mb, f"Seg {i} - Audio Fallback Pass", batch_str)
                
                if not fallback_success or not os.path.exists(a_tmp) or os.path.getsize(a_tmp) < 100:
                    print(f"⚠️ Warning: Source file has no extractable audio track. Producing silent video track assignment.", flush=True)
                    if os.path.exists(a_tmp): os.remove(a_tmp)
                    a_tmp = None

            # PASS 3: Safe Mux Phase (Combine the tracks seamlessly)
            print(f"🎛️ Muxing video and audio pipelines together for Segment {i}...", flush=True)
            if a_tmp:
                mux_cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', v_tmp, '-i', a_tmp, '-c:v', 'copy', '-c:a', 'copy', '-movflags', '+faststart', seg_out]
            else:
                # Safe fall-through for completely absent audio streams
                mux_cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', v_tmp, '-c:v', 'copy', '-movflags', '+faststart', seg_out]
                
            subprocess.run(mux_cmd)
            
            # Delete intermediate single-stream artifacts instantly
            if os.path.exists(v_tmp): os.remove(v_tmp)
            if a_tmp and os.path.exists(a_tmp): os.remove(a_tmp)

        segment_files.append(seg_out)

    if len(segment_files) > 1:
        with open(f"list_{file_num}.txt", "w") as f:
            for s in segment_files: f.write(f"file '{s}'\n")
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'concat', '-safe', '0', '-i', f"list_{file_num}.txt", '-c', 'copy', '-movflags', '+faststart', final_out])
        for s in segment_files: os.remove(s)
        os.remove(f"list_{file_num}.txt")
    elif len(segment_files) == 1:
        if os.path.exists(segment_files[0]):
            if os.path.exists(final_out): os.remove(final_out)
            os.rename(segment_files[0], final_out)
        else:
            print(f"❌ ERROR: Segment target file '{segment_files[0]}' was never generated.", flush=True)
            if os.path.exists(temp_in): os.remove(temp_in)
            return f"❌ FAILED: Missing output artifact", False
    else:
        print("❌ ERROR: No valid segments parsed.", flush=True)
        if os.path.exists(temp_in): os.remove(temp_in)
        return f"❌ FAILED: Empty partition array", False

    if hold_upload:
        print(f"📦 HOLDING: {display_name} | Group CT{ct_code} (Part {current_part} of {total_parts})", flush=True)
        if os.path.exists(temp_in): os.remove(temp_in)
        return final_out, False
        
    print(f"📤 Uploading: {display_name}...", flush=True)
    media = MediaFileUpload(final_out, mimetype='video/mp4', resumable=True)
    request = service.files().create(body={'name': output_name, 'parents': [OUTPUT_FOLDER_ID]}, media_body=media)
    response = None
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                print(f"⬆️ {display_name} | Uploading: {int(status.progress() * 100)}%", end='\r', flush=True)
        except Exception as e:
            print(f"⚠️ Upload flicker: {e}. Retrying...", flush=True)
            time.sleep(5)

    print(f"🚀 SINGLE FILE UPLOAD COMPLETE: {display_name}", flush=True)
    if os.path.exists(temp_in): os.remove(temp_in)
    if os.path.exists(final_out): os.remove(final_out)
    return None, False

if __name__ == "__main__":
    from collections import defaultdict
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        service = get_drive_service()
        fh = io.BytesIO()
        MediaIoBaseDownload(fh, service.files().get_media(fileId=CONFIG_FILE_ID)).next_chunk()
        config_lines = fh.getvalue().decode().splitlines()

        file_count = 0
        ct_groups = defaultdict(list)
        parsed_entries = []       
        ct_total_counts = defaultdict(int)  
        skipped_ct_tags = set()

        for line in config_lines:
            line_str = line.strip()
            if not line_str or line_str.startswith('#'): continue
            parts = [p.strip() for p in line_str.split('---') if p.strip()]
            if len(parts) >= 3:
                source_val = parts[0]
                mode_val = parts[1].upper()
                fade = any(p.upper() == 'F' for p in parts)
                
                ct_code = None
                for p in parts:
                    if p.upper().startswith('CT'):
                        ct_code = p[2:].strip()
                        break
                        
                times = [p for p in parts[2:] if p.upper() != 'F' and not p.upper().startswith('CT')]
                
                entry = {
                    'line': line_str,
                    'source': source_val,
                    'mode': mode_val,
                    'fade': fade,
                    'ct_code': ct_code,
                    'times': times
                }
                parsed_entries.append(entry)
                if ct_code:
                    ct_total_counts[ct_code] += 1

        for idx, entry in enumerate(parsed_entries):
            file_count += 1
            ct_code = entry['ct_code']
            
            # Cascade Skip Check
            if ct_code and ct_code in skipped_ct_tags:
                print(f"⏩ AUTOMATICALLY SKIPPING: entry [{file_count}] because Group CT{ct_code} was marked skipped.", flush=True)
                continue

            is_part_of_group = ct_code is not None
            is_first_part = is_part_of_group and len(ct_groups[ct_code]) == 0
            
            should_call_drive = (not is_part_of_group) or is_first_part
            
            data = {'mode': entry['mode'], 'times': entry['times'], 'fade': entry['fade']}
            
            try:
                if is_part_of_group and not is_first_part:
                    print(f"🛠️  CT PART {len(ct_groups[ct_code])+1}/{ct_total_counts[ct_code]}: Processing segment locally...")
                    skip_api_check_value = True
                else:
                    skip_api_check_value = False

                local_file, was_skipped = process_video(
                    service, 
                    entry['source'], 
                    "link", 
                    data, 
                    f"[{file_count}]", 
                    file_count, 
                    hold_upload=True if is_part_of_group else False, 
                    skip_api_check=skip_api_check_value, 
                    ct_code=ct_code, 
                    current_part=len(ct_groups[ct_code]) + 1 if ct_code else 0,
                    total_parts=ct_total_counts[ct_code] if ct_code else 0
                )
                
                if was_skipped and is_part_of_group and is_first_part:
                    print(f"✅ GROUP CT{ct_code} already exists on Drive. Skipping all remaining parts.")
                    skipped_ct_tags.add(ct_code)
                    continue
                
                if ct_code and local_file:
                    ct_groups[ct_code].append({'path': local_file, 'line': entry['line']})
                    
            except Exception as e:
                print(f"⚠️ Process Error: {e}")
                import traceback
                traceback.print_exc()
                sys.exit(99)
                
            if ct_code and ct_code in ct_groups:
                current_count = len(ct_groups[ct_code])
                target_count = ct_total_counts[ct_code]
                
                if current_count == target_count:
                    items = ct_groups[ct_code]
                    paths = [i['path'] for i in items]
                    first_line = items[0]['line']
                    clean_part = first_line.split("---")[0].strip()
                    
                    if "##" in clean_part:
                        raw_name = clean_part.split("##")[1].strip()
                    else:
                        raw_name = clean_part
                    if not raw_name.lower().endswith(".mp4"):
                        raw_name += ".mp4"
                    
                    if len(paths) > 1:
                        print(f"\n🧲 CONCATENATING GROUP CT{ct_code} ({len(paths)} files) NOW...")
                        ts_paths = []
                        for idx, p in enumerate(paths):
                            ts_out = f"temp_split_{ct_code}_{idx}.ts"
                            subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', p, '-c', 'copy', '-bsf:v', 'h264_mp4toannexb', '-f', 'mpegts', ts_out])
                            ts_paths.append(ts_out)
                        
                        concat_string = "concat:" + "|".join(ts_paths)
                        final_out = f"final_ct_{ct_code}.mp4"
                        
                        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', concat_string, '-c', 'copy', '-absf', 'aac_adtstoasc', '-movflags', '+faststart', final_out])
                        
                        for ts_p in ts_paths:
                            if os.path.exists(ts_p): os.remove(ts_p)
                        try:
                            upload_final_to_drive(service, final_out, raw_name)
                            print(f"🏆 MERGED GROUP CT{ct_code} UPLOAD COMPLETE!\n", flush=True)
                            try:
                                safe_raw_name = raw_name.replace("'", "\\'")
                                q_find = f"name = '{safe_raw_name}' and '{OUTPUT_FOLDER_ID}' in parents and trashed = false"
                                uploaded_files = service.files().list(q=q_find, fields="files(id)").execute().get('files', [])
                            except Exception as e:
                                print(f"⚠️ Could not initiate transfer for group file: {e}", flush=True)
                            for p in paths: 
                                if os.path.exists(p): os.remove(p)
                            if os.path.exists(final_out): os.remove(final_out)
                            del ct_groups[ct_code]
                        except Exception as upload_err:
                            print(f"❌ FATAL: Group CT{ct_code} merged but completely failed to upload after all retries: {upload_err}")
                            print("⚠️ Preserving local segments to avoid losing work! Exiting safely for workflow resume.", flush=True)
                            sys.exit(99)
                    else:
                        print(f"📦 Only 1 file for CT{ct_code}. Uploading normally.", flush=True)
                        upload_final_to_drive(service, paths[0], raw_name)
                        print(f"🚀 SINGLE CT FILE UPLOAD COMPLETE!", flush=True)
                        try:
                            safe_raw_name = raw_name.replace("'", "\\'")
                            q_find = f"name = '{safe_raw_name}' and '{OUTPUT_FOLDER_ID}' in parents and trashed = false"
                            uploaded_files = service.files().list(q=q_find, fields="files(id)").execute().get('files', [])
                        except Exception as e:
                            print(f"⚠️ Could not initiate transfer for single CT file: {e}", flush=True)
                        if os.path.exists(paths[0]): os.remove(paths[0])
        
        print("\n✅ ALL ENTRIES PROCESSED.", flush=True)
        sys.exit(0)

    except Exception as e:
        print(f"💥 Critical Connection Error: {e}")
        sys.exit(99)
        
