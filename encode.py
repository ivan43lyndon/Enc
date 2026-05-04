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

# --- YOUR EXACT HUD LOGIC ---
def run_ffmpeg_process(cmd, duration, display_name, target_size, desc, batch_str):
    print(f"\n--- {desc}: {display_name} ---", flush=True)
    process = Popen(cmd, stdout=PIPE, stderr=STDOUT, universal_newlines=True, bufsize=1)
    time_regex = re.compile(r"time=(\d{2}:\d{2}:\d{2}\.\d+)")
    
    last_print_time = 0
    
    for line in process.stdout:
        match = time_regex.search(line)
        if match:
            # Only print every 5 seconds to avoid flooding the log, but keep it moving
            current_wall_time = time.time()
            if current_wall_time - last_print_time > 5:
                cur_s = time_to_seconds(match.group(1))
                pct = (cur_s / duration) * 100 if duration > 0 else 0
                # Removed the \r so GitHub Actions is forced to show the line
                print(f"馃摝 {batch_str} | {display_name} | {pct:5.1f}% | {match.group(1)} / {seconds_to_hms(duration)}", flush=True)
                last_print_time = current_wall_time
                
    process.wait()
    if process.returncode != 0:
        print(f"鉂� FFMPEG FAILED on {display_name}", flush=True)
    return process.returncode == 0


# --- CORE LOGIC (Restored from your snippet) ---
def process_video(service, file_id, fname, data, batch_str, file_num):
    # --- A. RESUME CHECK ---
    output_name = fname.replace(".mp4", ".mp4")
    # Search for the output file in your Output Folder
    q = f"'{OUTPUT_FOLDER_ID}' in parents and name = '{output_name}' and trashed = false"
    check = service.files().list(q=q).execute().get('files', [])

    display_name = f"File {file_num}"
    temp_in = "temp_in.mp4"
    final_out = "final_out.mp4"
    
    if check:
        print(f"⏩ SKIPPING: {display_name} (Already exists in Drive)", flush=True)
        return f"⏩ SKIPPED: {display_name}", True

    # --- B. TIMEOUT CHECK ---
    elapsed = time.time() - START_TIME
    if elapsed > TIMEOUT_LIMIT:
        print(f"\n⚠️ TIME LIMIT APPROACHING ({int(elapsed)}s). Stopping to prevent cutoff.", flush=True)
        sys.exit(0) # Exit cleanly to trigger the next loop
    
    # Download
    print(f"Downloading {display_name}...", flush=True)
    request = service.files().get_media(fileId=file_id)
    with io.FileIO(temp_in, 'wb') as fh:
        downloader = MediaIoBaseDownload(fh, request, chunksize=1024*1024*10)
        done = False
        while not done: _, done = downloader.next_chunk()

    # Metadata
    probe = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=height,avg_frame_rate', '-of', 'csv=p=0', temp_in], capture_output=True, text=True).stdout.strip().split(',')
    src_h = int(probe[0]) if probe[0] else 720
    fps_parts = probe[1].split('/')
    src_fps = float(fps_parts[0])/float(fps_parts[1]) if len(fps_parts)==2 else 30.0
    total_dur = float(subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', temp_in], capture_output=True, text=True).stdout.strip())
    src_size = os.path.getsize(temp_in) / (1024*1024)

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
    # --- START MULTI-SEGMENT LOOP ---
    for i, (start, dur) in enumerate(segments):
        seg_out = f"seg_{i}.mp4"
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

    # Concat segments
    if len(segment_files) > 1:
        with open("list.txt", "w") as f:
            for s in segment_files: f.write(f"file '{s}'\n")
        subprocess.run(['ffmpeg', '-hide_banner', '-y', '-f', 'concat', '-safe', '0', '-i', 'list.txt', '-c', 'copy', '-movflags', '+faststart', final_out])
        for s in segment_files: os.remove(s)
        os.remove("list.txt")
    else:
        os.rename(segment_files[0], final_out)

    # Upload
    # Upload to Drive with Resumable Retry Logic
    print(f"Uploading {display_name}...", flush=True)
    media = MediaFileUpload(final_out, mimetype='video/mp4', resumable=True)
    request = service.files().create(
        body={'name': output_name, 'parents': [OUTPUT_FOLDER_ID]},
        media_body=media
    )
    
    response = None
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                print(f"Upload Progress: {int(status.progress() * 100)}%", flush=True)
        except Exception as e:
            print(f"⚠️ Upload connection flickered: {e}. Retrying...", flush=True)
            time.sleep(5) # Wait 5 seconds and try the chunk again
    print(f"DONE: {display_name}\n", flush=True)
    if os.path.exists(temp_in): os.remove(temp_in)
    if os.path.exists(final_out): os.remove(final_out)
    return f"✅ SUCCESS [{mode}]: {display_name}", True

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
