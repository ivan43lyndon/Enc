import os
import subprocess
import sys
import time
import io
import json
import re
import warnings
from subprocess import Popen, PIPE, STDOUT
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload
from google.oauth2.credentials import Credentials as UserCredentials
import asyncio
from playwright.async_api import async_playwright

# Start a timer at the very beginning of the script
START_TIME = time.time()
TIMEOUT_LIMIT = 20000  # 5 hours and 33 minutes (safe buffer)

warnings.filterwarnings("ignore", category=FutureWarning)

# --- CONFIG (From Secrets) ---
DRIVE_TOKEN = os.environ.get('DRIVE_TOKEN')
INPUT_FOLDER_ID = '1G7nC7CrMi_8HdtVGxdR-aNdak9FrVAcd' 
OUTPUT_FOLDER_ID = '14KAhaiTisjuybP2Pc6mcbLau8JoyDq5y'
CONFIG_FILE_ID = '1rE51zdRaXCIrxmWZhRjRZaKIuRvadDo3'

TARGET_WIDTH = 1280
TARGET_HEIGHT = 720
TARGET_CRF_VALUE = 22
FADE_DURATION = 1

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

# --- UPDATED SNIFFER FOR PIXELDRAIN ---
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
            if current_wall_time - last_print_time > 5:
                cur_s = time_to_seconds(match.group(1))
                pct = (cur_s / duration) * 100 if duration > 0 else 0
                print(f"📦 {batch_str} | {display_name} | {pct:5.1f}% | {match.group(1)} / {seconds_to_hms(duration)}", flush=True)
                last_print_time = current_wall_time
                
    process.wait()
    if process.returncode != 0:
        print(f"❌ FFMPEG FAILED on {display_name}", flush=True)
    return process.returncode == 0

def process_video(service, file_id, fname, data, batch_str, file_num):
    if time.time() - START_TIME > TIMEOUT_LIMIT:
        print("\n⏳ TIMEOUT REACHED. Exiting for restart...", flush=True)
        sys.exit(99)
    
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

    try:
        q_check = "name = '" + safe_q_name + "' and '" + OUTPUT_FOLDER_ID + "' in parents and trashed = false"
        check = service.files().list(q=q_check, fields="files(id)").execute().get('files', [])
        if check:
            print(f"⏩ SKIPPING: {display_name} (Already exists)", flush=True)
            return f"⏩ SKIPPED", True
    except Exception as e:
        print(f"❌ Skip Check Error: {e}")
        return False

    if source_input.startswith("http"):
        print(f"🕵️ Resolving: {display_name}...", flush=True)
        resolved_link, session_cookies = asyncio.run(resolve_any_link(source_input))
        
        if not resolved_link:
            return f"❌ FAILED: No stream found", False

        UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        headers = f"Referer: {source_input}\r\nUser-Agent: {UA}\r\n"
        if session_cookies:
            headers += f"Cookie: {session_cookies}\r\n"

        raw_download_cmd = [
            'ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
            '-reconnect', '1', '-reconnect_at_eof', '1', '-reconnect_streamed', '1',
            '-headers', headers,
            '-i', resolved_link,
            '-c', 'copy', '-bsf:a', 'aac_adtstoasc', '-movflags', 'faststart', temp_in
        ]
        subprocess.run(raw_download_cmd)
    else:
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
            return False

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
            return False
    
    if not os.path.exists(temp_in) or os.path.getsize(temp_in) < 10000:
        return f"❌ FAILED: File empty", False
        
    print(f"✅ DOWNLOAD COMPLETE: {display_name}", flush=True) 

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
        return False

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
            cmd = ['ffmpeg', '-hide_banner', '-y', '-ss', str(start), '-i', temp_in, '-t', str(dur), '-c', 'copy', '-map', '0', seg_out]
        else:
            vf = vf_base
            cmd = ['ffmpeg', '-hide_banner', '-y', '-ss', str(start), '-i', temp_in, '-t', str(dur), '-vf', vf, '-c:v', 'libx264', '-crf', str(TARGET_CRF_VALUE), '-pix_fmt', 'yuv420p', '-maxrate', f"{bitrate}k", '-bufsize', f"{bitrate*2}k", '-preset', 'medium']
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
        subprocess.run(['ffmpeg', '-hide_banner', '-y', '-f', 'concat', '-safe', '0', '-i', f"list_{file_num}.txt", '-c', 'copy', '-movflags', '+faststart', final_out])
        for s in segment_files: os.remove(s)
        os.remove(f"list_{file_num}.txt")
    else:
        os.rename(segment_files[0], final_out)

    print(f"📤 Uploading: {display_name}...", flush=True)
    media = MediaFileUpload(final_out, mimetype='video/mp4', resumable=True)
    request = service.files().create(body={'name': output_name, 'parents': [OUTPUT_FOLDER_ID]}, media_body=media)
    response = None
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                print(f"⬆️ {batch_str} | Upload Progress: {int(status.progress() * 100)}%", flush=True)
        except Exception as e:
            print(f"⚠️ Upload flicker: {e}. Retrying...", flush=True)
            time.sleep(5)

    print(f"✅ UPLOAD COMPLETE: {display_name}\n", flush=True)
    if os.path.exists(temp_in): os.remove(temp_in)
    if os.path.exists(final_out): os.remove(final_out)
    return True

if __name__ == "__main__":
    try:
        service = get_drive_service()
        fh = io.BytesIO()
        MediaIoBaseDownload(fh, service.files().get_media(fileId=CONFIG_FILE_ID)).next_chunk()
        config_lines = fh.getvalue().decode().splitlines()
        
        file_count = 0
        for line in config_lines:
            line = line.strip()
            if not line or line.startswith('#'): continue
            parts = [p.strip() for p in line.split('---') if p.strip()]
            if len(parts) >= 3:
                file_count += 1
                source_val = parts[0]
                mode_val = parts[1].upper()
                fade = parts[-1].upper() == 'F'
                times = parts[2:-1] if fade else parts[2:]
                data = {'mode': mode_val, 'times': times, 'fade': fade}
                
                # Wrap the video processing in a retry/exit logic
                try:
                    process_video(service, source_val, "link", data, f"[{file_count}]", file_count)
                except Exception as e:
                    print(f"⚠️ Process Error: {e}")
                    sys.exit(99) # Exit to trigger resume/restart

        print("\n✅ ALL ENTRIES PROCESSED.", flush=True)
        sys.exit(0)

    except Exception as e:
        print(f"💥 Critical Connection Error: {e}")
        sys.exit(99) # Trigger restart for SSL/Network issues
