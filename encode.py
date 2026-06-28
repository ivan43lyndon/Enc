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
    last_print_time = 0
    
    for line in process.stdout:
        match = time_regex.search(line)
        if match:
            current_wall_time = time.time()
            if current_wall_time - last_print_time > 1:
                cur_s = time_to_seconds(match.group(1))
                pct = (cur_s / duration) * 100 if duration > 0 else 0
                print(f"📦 {batch_str} | {display_name} | {pct:5.1f}% | {match.group(1)} / {seconds_to_hms(duration)}", flush=True)
                last_print_time = current_wall_time
                
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

async def download_hls_segment(session, idx, seg_url, semaphore, temp_dir, headers, progress_tracker, key_info=None):
    async with semaphore:
        target_path = os.path.join(temp_dir, f"{idx:06d}.ts")
        if os.path.exists(target_path) and os.path.getsize(target_path) > 0:
            progress_tracker["completed"] += 1
            pct = int((progress_tracker["completed"] / progress_tracker["total"]) * 100)
            print(f"📥 HLS Download Progress: {pct}% ({progress_tracker['completed']}/{progress_tracker['total']})", end='\r', flush=True)
            return True

        for attempt in range(3):
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
                    
                    progress_tracker["completed"] += 1
                    pct = int((progress_tracker["completed"] / progress_tracker["total"]) * 100)
                    print(f"📥 HLS Download Progress: {pct}% ({progress_tracker['completed']}/{progress_tracker['total']})", end='\r', flush=True)
                    return True
            except Exception:
                if attempt == 2:
                    return False
                await asyncio.sleep(1)

async def native_hls_downloader(m3u8_url, session_cookies, target_output):
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

        temp_dir = f"hls_temp_{int(time.time())}"
        os.makedirs(temp_dir, exist_ok=True)
        semaphore = asyncio.Semaphore(MAX_HLS_WORKERS)

        custom_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": m3u8_url
        }
        if session_cookies:
            custom_headers["Cookie"] = session_cookies

        progress_tracker = {
            "completed": 0,
            "total": len(segments)
        }

        print(f"📋 Found {progress_tracker['total']} segments to download.", flush=True)

        async with aiohttp.ClientSession() as session:
            key_cache = {}
            tasks = []
            for idx, segment in enumerate(segments):
                seg_url = urljoin(base_url, segment.uri)
                key_info = None
                
                if segment.key and segment.key.method == "AES-128":
                    key_url = urljoin(base_url, segment.key.uri)
                    if key_url not in key_cache:
                        key_data = await fetch_hls_key(session, key_url, custom_headers)
                        iv_data = bytes.fromhex(segment.key.iv.strip('0x')) if segment.key.iv else None
                        key_cache[key_url] = {"key": key_data, "iv": iv_data}
                    key_info = key_cache[key_url]

                tasks.append(download_hls_segment(session, idx, seg_url, semaphore, temp_dir, custom_headers, progress_tracker, key_info))
            
            results = await asyncio.gather(*tasks)

        if all(results):
            print(f"\n💾 Segment downloads complete. Assembling output...")
            with open(target_output, "wb") as out_f:
                for idx in range(len(segments)):
                    chunk_path = os.path.join(temp_dir, f"{idx:06d}.ts")
                    with open(chunk_path, "rb") as chunk_f:
                        out_f.write(chunk_f.read())
                    os.remove(chunk_path)
            os.rmdir(temp_dir)
            return True
    except Exception as e:
        print(f"\n❌ Custom Native Downloader Thread Panic: {e}")
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
        
        # We will retry the ENTIRE scraping + downloading process up to 3 times
        max_download_attempts = 3
        download_success = False

        for dl_attempt in range(1, max_download_attempts + 1):
            print(f"🔄 [Attempt {dl_attempt}/{max_download_attempts}] Scraping fresh link and starting download...", flush=True)
            
            # Clean up any leftover dead file from a previous failed attempt
            if os.path.exists(temp_in):
                os.remove(temp_in)

            bracket_match = re.match(r"(.+?)\[(\d+)\]$", source_input)
            
            # --- PHASE 1: SCRAPE THE LINK ---
            try:
                if bracket_match:
                    folder_url = bracket_match.group(1)
                    target_index = int(bracket_match.group(2)) - 1
                    
                    file_list = []
                    session_cookies = ""
                    
                    async def scrape_folder():
                        nonlocal session_cookies
                        fl = []
                        async with async_playwright() as p:
                            browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
                            context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
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
                    resolved_link, session_cookies = asyncio.run(resolve_any_link(source_input))
                    if not resolved_link:
                        print(f"⚠️ Playwright failed to resolve stream on attempt {dl_attempt}.", flush=True)
                        time.sleep(5)
                        continue

                if ".m3u8" in resolved_link.lower() or "master" in resolved_link.lower():
                    print(f"🚀 HLS Stream Detected! Initiating non-FFmpeg Asynchronous Downloader pipeline...", flush=True)
                    native_success = asyncio.run(native_hls_downloader(resolved_link, session_cookies, temp_in))
                    if native_success and os.path.exists(temp_in) and os.path.getsize(temp_in) > 10000:
                        print(f"✅ Successfully grabbed file on attempt {dl_attempt}!", flush=True)
                        download_success = True
                        break
                    else:
                        print(f"⚠️ Native HLS downloader failed on attempt {dl_attempt}.", flush=True)
                        continue

                # 🔁 LEAVE THE ORIGINAL FFMPEG DOWNBOARDS AS A FALLBACK (For non-HLS URLs)
                headers = f"Referer: {source_input}\r\nUser-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)\r\n"
                if session_cookies:
                    headers += f"Cookie: {session_cookies}\r\n"

                raw_download_cmd = [
                    'ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                    '-reconnect', '1', '-reconnect_at_eof', '1', '-reconnect_streamed', '1',
                    '-headers', headers,
                    '-i', resolved_link,
                    '-c', 'copy', '-bsf:a', 'aac_adtstoasc', '-movflags', 'faststart', temp_in
                ]
                
                DOWNLOAD_TIMEOUT = 100 
                print(f"📥 FFMPEG downloading stream (Timeout guard: {DOWNLOAD_TIMEOUT}s)...", flush=True)
                result = subprocess.run(raw_download_cmd, timeout=DOWNLOAD_TIMEOUT)
                
                if result.returncode == 0 and os.path.exists(temp_in) and os.path.getsize(temp_in) > 10000:
                    print(f"✅ Successfully grabbed file on attempt {dl_attempt}!", flush=True)
                    download_success = True
                    break 
                else:
                    print(f"⚠️ FFMPEG exited with error code {result.returncode} on attempt {dl_attempt}.", flush=True)
                    
            except subprocess.TimeoutExpired:
                print(f"⚠️ FFMPEG hung up and hit the {DOWNLOAD_TIMEOUT}s timeout wall on attempt {dl_attempt}.", flush=True)
            except Exception as e:
                print(f"⚠️ Unexpected error during scraping/download phase: {e}", flush=True)
            
            # If we get here, the attempt failed. Wait a bit before resetting.
            print("⏳ Cool-down before hitting the host again...", flush=True)
            time.sleep(10)

        # If all attempts completely tanked, drop out of the file processing entirely
        if not download_success:
            if os.path.exists(temp_in):
                os.remove(temp_in)
            print(f"❌ FAILED: Unable to download {display_name} after {max_download_attempts} full link-reset retries.", flush=True)
            return f"❌ FAILED: All download retries exhausted", False
            
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
            cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-ss', str(start), '-i', temp_in, '-t', str(dur), '-c', 'copy', '-map', '0:v', '-map', '0:a', '-movflags', '+faststart']
        else:
            vf = vf_base
            cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-ss', str(start), '-i', temp_in, '-t', str(dur), '-vf', vf, '-c:v', 'libx264', '-crf', str(TARGET_CRF_VALUE), '-pix_fmt', 'yuv420p', '-maxrate', f"{bitrate}k", '-bufsize', f"{bitrate*2}k", '-preset', 'medium']
            
            if do_fade and is_last:
                cmd += ['-af', f"afade=t=out:st={dur - FADE_DURATION}:d={FADE_DURATION}", '-vf', vf + f",fade=t=out:st={dur - FADE_DURATION}:d={FADE_DURATION}"]
            else:
                cmd += ['-c:a', 'aac', '-b:a', '96k']
        cmd += [seg_out]
        
        run_ffmpeg_process(cmd, dur, display_name, target_size_mb, f"Segment {i}", batch_str)
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
    try:
        q_find = f"name = '{safe_q_name}' and '{OUTPUT_FOLDER_ID}' in parents and trashed = false"
        uploaded_files = service.files().list(q=q_find, fields="files(id)").execute().get('files', [])
    except Exception as e:
        print(f"⚠️ Could not initiate transfer for single file: {e}", flush=True)
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
        
