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

    # FIX: Remove the 'expiry' key if it's a string so the library doesn't crash
    if 'expiry' in data and isinstance(data['expiry'], str):
        del data['expiry']

    creds = UserCredentials(
        token=data.get('token'),
        refresh_token=data.get('refresh_token'),
        token_uri=data.get('token_uri'),
        client_id=data.get('client_id'),
        client_secret=data.get('client_secret')
    )
    
    # This force-refreshes the token if it's dead without checking the string date
    return build('drive', 'v3', credentials=creds)

def get_mb_per_minute_ratio(height):
    if height >= 1080: return 12.0
    if height >= 720: return 8.0
    elif height >= 540: return 6.5
    elif height >= 480: return 5.0
    else: return 4.0

# --- YOUR EXACT HUD LOGIC ---
def run_ffmpeg_process(cmd, duration, display_name, target_size, desc, batch_str, offset=0):
    print(f"\n--- {desc} ---")
    process = Popen(cmd, stdout=PIPE, stderr=STDOUT, universal_newlines=True, bufsize=1)
    time_regex = re.compile(r"time=(\d{2}:\d{2}:\d{2}\.\d+)")
    total_stamp = seconds_to_hms(duration)
    start_wall_time = time.time()

    for line in process.stdout:
        match = time_regex.search(line)
        if match:
            cur_s = max(0, time_to_seconds(match.group(1)) - offset)
            current_stamp = seconds_to_hms(cur_s)
            elapsed_wall_time = time.time() - start_wall_time
            speed = cur_s / elapsed_wall_time if elapsed_wall_time > 0 else 0
            remaining_s = (duration - cur_s) / speed if speed > 0.1 else 0
            eta_str = seconds_to_hms(remaining_s)
            pct = (cur_s / duration) * 100 if duration > 0 else 0

            # YOUR ULTIMATE HUD PRINT
            sys.stdout.write(f"\r📦 {batch_str} | {display_name[:15]:<15} | {pct:5.1f}% | {current_stamp} / {total_stamp} | {speed:3.1f}x | ETA: {eta_str} | Target: {target_size:.1f}MB")
            sys.stdout.flush()
    process.wait()
    return process.returncode == 0

# --- CORE LOGIC (Restored from your snippet) ---
def process_video(service, file_id, fname, data, batch_str, file_num):
    display_name = f"File {file_num}"
    temp_in, final_out = "temp_in.mp4", "final_out.mp4"
    
    # Download
    request = service.files().get_media(fileId=file_id)
    with io.FileIO(temp_in, 'wb') as fh:
        downloader = MediaIoBaseDownload(fh, request, chunksize=1024*1024*10)
        done = False
        while not done: _, done = downloader.next_chunk()

    # Metadata
    cmd_s = ['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height,avg_frame_rate', '-of', 'csv=p=0', temp_in]
    s_res = subprocess.run(cmd_s, capture_output=True, text=True).stdout.strip().split(',')
    src_h = int(s_res[1])
    src_fps = float(s_res[2].split('/')[0])/float(s_res[2].split('/')[1]) if '/' in s_res[2] else 30.0
    total_dur = float(subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', temp_in], capture_output=True, text=True).stdout.strip())
    src_size = os.path.getsize(temp_in) / (1024*1024)

    time_points = data['times']
    mode, do_fade = data['mode'], data['fade']
    segments = []
    total_trimmed_dur = 0.0
    for i in range(0, len(time_points), 2):
        if i+1 >= len(time_points): break
        s_s = time_to_seconds(time_points[i])
        e_s = min(time_to_seconds(time_points[i+1]), total_dur)
        if s_s < e_s:
            segments.append((time_points[i], e_s - s_s))
            total_trimmed_dur += (e_s - s_s)

    target_h = min(src_h, TARGET_HEIGHT)
    mb_rate = get_mb_per_minute_ratio(target_h)
    target_size_mb = mb_rate * (total_trimmed_dur / 60)
    if do_fade: mode = 'E'
    elif mode == 'E' and target_size_mb >= (src_size * 0.9): mode = 'T'
    bitrate = int((target_size_mb * 8192 - (96 * total_trimmed_dur)) / total_trimmed_dur)

    vf = f"scale=w='min(iw,{TARGET_WIDTH})':h='min(ih,{TARGET_HEIGHT})':force_original_aspect_ratio=decrease,setsar=1,scale=trunc(iw/2)*2:trunc(ih/2)*2"
    if src_fps > 30.5: vf += ",fps=fps=30"

    # Single Segment Execution
    start, dur = segments[0]
    if mode == 'T':
        cmd = ['ffmpeg', '-y', '-ss', start, '-i', temp_in, '-t', str(dur), '-c', 'copy', '-map', '0', '-movflags', '+faststart', final_out]
    else:
        cmd = ['ffmpeg', '-y', '-ss', start, '-i', temp_in, '-t', str(dur), '-vf', vf, '-c:v', 'libx264', '-crf', str(TARGET_CRF_VALUE), '-pix_fmt', 'yuv420p', '-maxrate', f"{bitrate}k", '-bufsize', f"{bitrate*2}k"]
        if do_fade: cmd += ['-af', f"afade=t=out:st={dur - FADE_DURATION}:d={FADE_DURATION}"]
        else: cmd += ['-c:a', 'aac', '-b:a', '96k']
        cmd += ['-movflags', '+faststart', final_out]
    
    success = run_ffmpeg_process(cmd, dur, display_name, target_size_mb, "PROCESSING", batch_str)

    if success:
        media = MediaFileUpload(final_out, resumable=True)
        service.files().create(body={'name': fname.replace(".mp4", " done.mp4"), 'parents': [OUTPUT_FOLDER_ID]}, media_body=media).execute()

    os.remove(temp_in)
    if os.path.exists(final_out): os.remove(final_out)
    return f"✅ SUCCESS [{mode}]: {display_name}", success

if __name__ == "__main__":
    service = get_drive_service()
    fh = io.BytesIO()
    MediaIoBaseDownload(fh, service.files().get_media(fileId=CONFIG_FILE_ID)).next_chunk()
    
    trim_data = {}
    for line in fh.getvalue().decode().splitlines():
        line = line.strip()
        if not line or line.startswith('#'): continue
        parts = [p.strip() for p in line.split('---') if p.strip()]
        if len(parts) >= 3:
            fade = parts[-1].upper() == 'F'
            times = parts[2:-1] if fade else parts[2:]
            trim_data[parts[0]] = {'mode': parts[1].upper(), 'times': times, 'fade': fade}

    results = service.files().list(q=f"'{INPUT_FOLDER_ID}' in parents and trashed = false").execute()
    files = sorted(results.get('files', []), key=lambda x: x['name'])
    valid_files = [f for f in files if f['name'] in trim_data]
    
    batch_history = []
    for i, f in enumerate(valid_files):
        print("\n=== BATCH HISTORY ===")
        for res in batch_history: print(res)
        msg, success = process_video(service, f['id'], f['name'], trim_data[f['name']], f"[{i+1}/{len(valid_files)}]", i+1)
        batch_history.append(msg)
