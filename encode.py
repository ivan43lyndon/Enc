import os
import json
import io
import subprocess
import sys
import shutil
import re
import time
from subprocess import Popen, PIPE, STDOUT
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload

# --- 1. CONFIGURATION ---
INPUT_FOLDER_ID = '1G7nC7CrMi_8HdtVGxdR-aNdak9FrVAcd' 
OUTPUT_FOLDER_ID = '14KAhaiTisjuybP2Pc6mcbLau8JoyDq5y'
TRIM_FILE_ID = '1rE51zdRaXCIrxmWZhRjRZaKIuRvadDo3'

TEMP_DIR = './Encoding_Cache'
os.makedirs(TEMP_DIR, exist_ok=True)

# Video Settings
TARGET_WIDTH = 1280
TARGET_HEIGHT = 720
AUDIO_BITRATE_KBPS = 96
TARGET_CRF_VALUE = 22 
SKIP_ENCODE_MARGIN_PERCENT = 10 
FADE_DURATION = 1 

# Time Sentry Settings
START_TIME = time.time()
MAX_RUNTIME_SECONDS = 5.5 * 3600  # 5.5 Hours buffer for GitHub's 6h limit

# --- 2. DRIVE API UTILITIES ---

def file_exists_in_drive(service, name, folder_id):
    """Checks if the output file already exists to avoid double-work."""
    query = f"name = '{name}' and '{folder_id}' in parents and trashed = false"
    results = service.files().list(q=query, fields="files(id)").execute()
    return len(results.get('files', [])) > 0

def download_file(service, file_id, local_path):
    request = service.files().get_media(fileId=file_id)
    fh = io.FileIO(local_path, 'wb')
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while done is False:
        status, done = downloader.next_chunk()
    return local_path

def upload_file(service, local_path, folder_id, file_label):
    file_metadata = {'name': os.path.basename(local_path), 'parents': [folder_id]}
    media = MediaFileUpload(local_path, mimetype='video/mp4', resumable=True, chunksize=5*1024*1024)
    request = service.files().create(body=file_metadata, media_body=media, fields='id')
    
    response = None
    print(f"📤 Starting upload: {file_label}")
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                percent = int(status.progress() * 100)
                sys.stdout.write(f"\r📤 Upload Progress: [{percent}%] ")
                sys.stdout.flush()
        except Exception as e:
            print(f"\n⚠️ Connection issue, retrying...")
            time.sleep(5) 
    print(f"\n✅ Upload Complete: {file_label}")

# --- 3. ENCODING LOGIC ---

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

def seconds_to_hms(seconds):
    if seconds < 0 or seconds == float('inf'): return "00:00:00"
    s = int(seconds)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"

def parse_trim_file(local_path):
    trim_data = {}
    if not os.path.exists(local_path): return trim_data
    with open(local_path, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'): continue
            parts = [p.strip() for p in line.split('---') if p.strip()]
            if len(parts) >= 3:
                filename = parts[0]
                mode = parts[1].upper()
                fade = parts[-1].upper() == 'F'
                times = parts[2:-1] if fade else parts[2:]
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
        return w, h, float(d_res), fps
    except: return 0, 0, 0.0, 30.0

def run_ffmpeg_process(cmd, duration, file_label, target_size, desc, batch_str):
    print(f"\n--- {desc} ---")
    process = Popen(cmd, stdout=PIPE, stderr=STDOUT, universal_newlines=True, bufsize=1)
    time_regex = re.compile(r"time=(\d{2}:\d{2}:\d{2}\.\d+)")
    start_wall = time.time()
    for line in process.stdout:
        match = time_regex.search(line)
        if match:
            cur_s = time_to_seconds(match.group(1))
            elapsed = time.time() - start_wall
            speed = cur_s / elapsed if elapsed > 0 else 0
            pct = (cur_s / duration) * 100 if duration > 0 else 0
            sys.stdout.write(f"\r📦 {batch_str} | {file_label:<10} | {pct:5.1f}% | {speed:3.1f}x | Target: {target_size:.1f}MB")
            sys.stdout.flush()
    process.wait()
    return process.returncode == 0

def process_video(input_path, output_path, data, batch_str, file_label):
    mode, time_points, do_fade = data['mode'], data['times'], data['fade']
    src_w, src_h, total_dur, src_fps = get_video_metadata(input_path)
    src_size = os.path.getsize(input_path) / (1024*1024)
    
    segments = []
    total_trimmed_dur = 0.0
    for i in range(0, len(time_points), 2):
        if i+1 >= len(time_points): continue
        s_s, e_s = time_to_seconds(time_points[i]), min(time_to_seconds(time_points[i+1]), total_dur or 999999)
        if s_s < e_s:
            segments.append((time_points[i], e_s - s_s))
            total_trimmed_dur += (e_s - s_s)
            
    if not segments: return False
    
    target_h = min(src_h, TARGET_HEIGHT)
    mb_rate = get_mb_per_minute_ratio(target_h)
    target_size_mb = mb_rate * (total_trimmed_dur / 60)
    
    if do_fade: mode = 'E'
    elif mode == 'E' and target_size_mb >= (src_size * (1.0 - (SKIP_ENCODE_MARGIN_PERCENT / 100.0))): mode = 'T'
    
    bitrate = int((target_size_mb * 8192 - (96 * total_trimmed_dur)) / total_trimmed_dur) if total_trimmed_dur > 0 else 1000
    vf = f"scale=w='min(iw,{TARGET_WIDTH})':h='min(ih,{TARGET_HEIGHT})':force_original_aspect_ratio=decrease,setsar=1,scale=trunc(iw/2)*2:trunc(ih/2)*2"
    
    if len(segments) == 1:
        start, dur = segments[0]
        if mode == 'T':
            cmd = ['ffmpeg', '-y', '-ss', start, '-i', input_path, '-t', str(dur), '-c', 'copy', '-map', '0', '-movflags', '+faststart', output_path]
        else:
            cmd = ['ffmpeg', '-y', '-ss', start, '-i', input_path, '-t', str(dur), '-vf', vf + (f",fade=t=out:st={dur-FADE_DURATION}:d={FADE_DURATION}" if do_fade else ""), '-c:v', 'libx264', '-crf', str(TARGET_CRF_VALUE), '-pix_fmt', 'yuv420p', '-maxrate', f"{bitrate}k", '-bufsize', f"{bitrate*2}k", '-movflags', '+faststart', output_path]
        return run_ffmpeg_process(cmd, dur, file_label, target_size_mb, "PROCESSING", batch_str)
    else:
        # Multi-segment logic (simplified for script length)
        temp_parts = os.path.join(TEMP_DIR, "parts")
        os.makedirs(temp_parts, exist_ok=True)
        segment_files = []
        for i, (start, dur) in enumerate(segments):
            seg_path = os.path.join(temp_parts, f"part_{i}.mp4")
            segment_files.append(seg_path)
            cmd = ['ffmpeg', '-y', '-ss', start, '-i', input_path, '-t', str(dur), '-c', 'copy' if mode == 'T' else 'libx264', seg_path]
            run_ffmpeg_process(cmd, dur, file_label, 0, f"Segment {i+1}", batch_str)
        list_path = os.path.join(temp_parts, "list.txt")
        with open(list_path, 'w') as f:
            for s in segment_files: f.write(f"file '{os.path.abspath(s)}'\n")
        subprocess.run(['ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', list_path, '-c', 'copy', output_path], capture_output=True)
        shutil.rmtree(temp_parts)
        return os.path.exists(output_path)

# --- 4. MAIN EXECUTION LOOP ---

if __name__ == "__main__":
    if not os.environ.get('DRIVE_TOKEN'):
        print("❌ ERROR: DRIVE_TOKEN secret missing."); sys.exit(1)
    
    creds_info = json.loads(os.environ.get('DRIVE_TOKEN'))
    creds = Credentials.from_authorized_user_info(creds_info)
    service = build('drive', 'v3', credentials=creds)
    
    local_trim = os.path.join(TEMP_DIR, "trim_config.txt")
    download_file(service, TRIM_FILE_ID, local_trim)
    trim_data = parse_trim_file(local_trim)

    results = service.files().list(q=f"'{INPUT_FOLDER_ID}' in parents and trashed = false", fields="files(id, name)").execute()
    valid_files = [f for f in results.get('files', []) if f['name'] in trim_data]
    
    count = 0
    for i, g_file in enumerate(valid_files):
        filename = g_file['name']
        output_name = "OUT_" + filename
        file_label = f"File {i+1}"

        # 1. SKIP IF EXISTS
        if file_exists_in_drive(service, output_name, OUTPUT_FOLDER_ID):
            print(f"⏩ Skipping {file_label}: Already finished.")
            continue

        # 2. TIME CHECKPOINT
        if (time.time() - START_TIME) > MAX_RUNTIME_SECONDS:
            print(f"\n⚠️ TIME LIMIT REACHED. Stopping session. Run again to finish remaining files.")
            sys.exit(0)

        print(f"\n\n=== BATCH PROGRESS: {i+1}/{len(valid_files)} ===")
        local_in = os.path.join(TEMP_DIR, filename)
        local_out = os.path.join(TEMP_DIR, output_name)

        print(f"📥 Downloading: {file_label}")
        download_file(service, g_file['id'], local_in)

        if process_video(local_in, local_out, trim_data[filename], f"[{i+1}/{len(valid_files)}]", file_label):
            upload_file(service, local_out, OUTPUT_FOLDER_ID, file_label)
            count += 1
        
        if os.path.exists(local_in): os.remove(local_in)
        if os.path.exists(local_out): os.remove(local_out)

    print(f"\n\nFINAL BATCH REPORT: Successfully processed {count} files.")
