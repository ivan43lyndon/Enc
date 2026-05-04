import os
import subprocess
import re
import sys
import time
import io
import json
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload
from google.oauth2.service_account import Credentials

# --- 1. CONFIGURATION ---
START_TIME = time.time()
MAX_RUN_TIME = 5 * 3600  # 5h 45m
TARGET_WIDTH = 1280
TARGET_HEIGHT = 720
AUDIO_BITRATE_KBPS = 96
TARGET_CRF_VALUE = 22
TARGET_MB_PER_MINUTE = 0
SKIP_ENCODE_MARGIN_PERCENT = 10
FADE_DURATION = 1

DRIVE_TOKEN = os.environ.get('DRIVE_TOKEN')

# !!! REPLACE THESE IDs !!!
INPUT_FOLDER_ID = 'YOUR_INPUT_FOLDER_ID_HERE' 
OUTPUT_FOLDER_ID = 'YOUR_OUTPUT_FOLDER_ID_HERE'
CONFIG_FILE_ID = 'YOUR_1_TXT_FILE_ID_HERE'

# --- 2. MATH & PARSING ---

def get_mb_per_minute_ratio(height):
    if height >= 1080: return 12.0
    if height >= 720: return 8.0
    elif height >= 540: return 6.5
    elif height >= 480: return 5.0
    else: return 4.0

def time_to_seconds(time_str):
    if not time_str: return 0.0
    try:
        if ':' in time_str:
            parts = time_str.split(':')
            if len(parts) == 3: return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
            elif len(parts) == 2: return float(parts[0]) * 60 + float(parts[1])
        return float(time_str)
    except: return 0.0

def parse_trim_file(content):
    trim_data = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith('#'): continue
        parts = [p.strip() for p in line.split('---') if p.strip()]
        if len(parts) >= 3:
            filename = parts[0]
            mode = parts[1].upper()
            fade = False
            if parts[-1].upper() == 'F':
                fade = True
                times = parts[2:-1]
            else:
                times = parts[2:]
            trim_data[filename] = {'mode': mode, 'times': times, 'fade': fade}
    return trim_data

def get_video_metadata(input_file):
    cmd_s = ['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height,avg_frame_rate', '-of', 'csv=p=0', input_file]
    cmd_d = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', input_file]
    try:
        s_res = subprocess.run(cmd_s, capture_output=True, text=True).stdout.strip().split(',')
        w, h = int(s_res[0]), int(s_res[1])
        num, den = s_res[2].split('/')
        fps = float(num) / float(den) if float(den) != 0 else 30.0
        d_res = subprocess.run(cmd_d, capture_output=True, text=True).stdout.strip()
        dur = float(d_res) if d_res else 0.0
        return w, h, dur, fps
    except: return 0, 0, 0.0, 30.0

# --- 3. DRIVE BRIDGE ---

def get_drive_service():
    # Bulletproof parsing for the token
    try:
        data = json.loads(DRIVE_TOKEN)
    except:
        try:
            data = json.loads(eval(DRIVE_TOKEN))
        except:
            data = eval(DRIVE_TOKEN)
    
    creds = Credentials.from_service_account_info(data)
    return build('drive', 'v3', credentials=creds)

def file_exists_in_drive(service, name, folder_id):
    query = f"name = '{name}' and '{folder_id}' in parents and trashed = false"
    results = service.files().list(q=query, fields="files(id)").execute()
    return len(results.get('files', [])) > 0

# --- 4. CORE PROCESSING ---

def process_video(service, file_id, fname, data):
    temp_in = "temp_input.mp4"
    final_out = "final_output.mp4"
    
    # Download
    request = service.files().get_media(fileId=file_id)
    fh = io.FileIO(temp_in, 'wb')
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done: _, done = downloader.next_chunk()

    src_w, src_h, total_dur, src_fps = get_video_metadata(temp_in)
    src_size = os.path.getsize(temp_in) / (1024*1024)
    mode, time_points, do_fade = data['mode'], data['times'], data['fade']

    segments = []
    total_trimmed_dur = 0.0
    for i in range(0, len(time_points), 2):
        if i+1 >= len(time_points): continue
        s_s = time_to_seconds(time_points[i])
        limit = total_dur if total_dur > 0 else 999999
        e_s = min(time_to_seconds(time_points[i+1]), limit)
        if s_s < e_s:
            segments.append((time_points[i], e_s - s_s))
            total_trimmed_dur += (e_s - s_s)

    if not segments: 
        if os.path.exists(temp_in): os.remove(temp_in)
        return False

    target_h = min(src_h, TARGET_HEIGHT)
    mb_rate = get_mb_per_minute_ratio(target_h)
    target_size_mb = mb_rate * (total_trimmed_dur / 60)

    if do_fade: mode = 'E'
    elif mode == 'E' and target_size_mb >= (src_size * (1.0 - (SKIP_ENCODE_MARGIN_PERCENT / 100.0))):
        mode = 'T'

    bitrate = int((target_size_mb * 8192 - (96 * total_trimmed_dur)) / total_trimmed_dur) if total_trimmed_dur > 0 else 1000
    
    fps_filter = ",fps=fps=30" if src_fps > 30.5 else ""
    vf = f"scale=w='min(iw,{TARGET_WIDTH})':h='min(ih,{TARGET_HEIGHT})':force_original_aspect_ratio=decrease{fps_filter},setsar=1,scale=trunc(iw/2)*2:trunc(ih/2)*2"
    
    if do_fade:
        vf += f",fade=t=out:st={total_trimmed_dur - FADE_DURATION}:d={FADE_DURATION}"
        af = f"afade=t=out:st={total_trimmed_dur - FADE_DURATION}:d={FADE_DURATION}"

    # FFMPEG EXECUTION
    start, dur = segments[0]
    if mode == 'T':
        cmd = ['ffmpeg', '-y', '-ss', start, '-i', temp_in, '-t', str(dur), '-c', 'copy', '-map', '0', '-movflags', '+faststart', final_out]
    else:
        cmd = ['ffmpeg', '-y', '-ss', start, '-i', temp_in, '-t', str(dur), '-vf', vf, '-c:v', 'libx264', '-crf', str(TARGET_CRF_VALUE), '-pix_fmt', 'yuv420p', '-profile:v', 'high', '-preset', 'medium', '-maxrate', f"{bitrate}k", '-bufsize', f"{bitrate*2}k"]
        if do_fade: cmd += ['-af', af]
        else: cmd += ['-c:a', 'aac', '-b:a', '96k']
        cmd += ['-movflags', '+faststart', final_out]

    subprocess.run(cmd, stderr=subprocess.DEVNULL)

    if os.path.exists(final_out):
        out_name = os.path.splitext(fname)[0].replace(".", " ") + ".mp4"
        media = MediaFileUpload(final_out, resumable=True)
        service.files().create(body={'name': out_name, 'parents': [OUTPUT_FOLDER_ID]}, media_body=media).execute()
        os.remove(final_out)
    
    if os.path.exists(temp_in): os.remove(temp_in)
    return True

# --- 5. MAIN ---

if __name__ == "__main__":
    service = get_drive_service()
    
    # 1.txt
    request = service.files().get_media(fileId=CONFIG_FILE_ID)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done: _, done = downloader.next_chunk()
    trim_data = parse_trim_file(fh.getvalue().decode())

    # Files
    results = service.files().list(q=f"'{INPUT_FOLDER_ID}' in parents and trashed = false", fields="files(id, name)").execute()
    files = results.get('files', [])

    for f in sorted(files, key=lambda x: x['name']):
        if time.time() - START_TIME > MAX_RUN_TIME:
            break

        if f['name'] in trim_data:
            out_name = os.path.splitext(f['name'])[0].replace(".", " ") + ".mp4"
            if not file_exists_in_drive(service, out_name, OUTPUT_FOLDER_ID):
                process_video(service, f['id'], f['name'], trim_data[f['name']])
