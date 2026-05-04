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

# --- GITHUB SECRETS & CONFIG ---
DRIVE_TOKEN = os.environ.get('DRIVE_TOKEN')
INPUT_FOLDER_ID = '1G7nC7CrMi_8HdtVGxdR-aNdak9FrVAcd' 
OUTPUT_FOLDER_ID = '14KAhaiTisjuybP2Pc6mcbLau8JoyDq5y'
TRIM_FILE_ID = '1rE51zdRaXCIrxmWZhRjRZaKIuRvadDo3'

TEMP_DIR = './Encoding_Cache'
os.makedirs(TEMP_DIR, exist_ok=True)

# YOUR ORIGINAL CONSTANTS
TARGET_WIDTH, TARGET_HEIGHT = 1280, 720
TARGET_CRF_VALUE = 22 
SKIP_ENCODE_MARGIN_PERCENT = 10

def get_mb_per_minute_ratio(height):
    if height >= 720: return 8.0
    elif height >= 540: return 6.5
    elif height >= 480: return 5.0
    else: return 4.0

# --- PRIVACY HELPERS ---

def get_drive_service():
    creds_dict = json.loads(DRIVE_TOKEN)
    creds = Credentials.from_authorized_user_info(creds_dict)
    return build('drive', 'v3', credentials=creds)

def file_exists_in_drive(service, name, folder_id):
    query = f"name = '{name}' and '{folder_id}' in parents and trashed = false"
    results = service.files().list(q=query, fields="files(id)", stderr=subprocess.DEVNULL).execute()
    return len(results.get('files', [])) > 0

# --- YOUR ORIGINAL LOGIC (PORTED TO API) ---

def time_to_seconds(time_str):
    if not time_str: return 0.0
    try:
        if ':' in time_str:
            parts = time_str.split(':')
            if len(parts) == 3: return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
            elif len(parts) == 2: return float(parts[0]) * 60 + float(parts[1])
        return float(time_str)
    except: return 0.0

def get_video_metadata(input_file):
    cmd_s = ['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height,avg_frame_rate', '-of', 'csv=p=0', input_file]
    cmd_d = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', input_file]
    try:
        s_res = subprocess.run(cmd_s, capture_output=True, text=True, stderr=subprocess.DEVNULL).stdout.strip().split(',')
        w, h = int(s_res[0]), int(s_res[1])
        num, den = s_res[2].split('/')
        fps = float(num) / float(den) if float(den) != 0 else 30.0
        d_res = subprocess.run(cmd_d, capture_output=True, text=True, stderr=subprocess.DEVNULL).stdout.strip()
        return w, h, float(d_res), fps
    except: return 0, 0, 0.0, 30.0

def run_ffmpeg_process(cmd, duration, file_label, target_size, desc, batch_str):
    print(f"\n--- {desc} ---")
    process = Popen(cmd, stdout=PIPE, stderr=STDOUT, universal_newlines=True, bufsize=1)
    time_regex = re.compile(r"time=(\d{2}:\d{2}:\d{2}\.\d+)")
    start_t = time.time()
    for line in process.stdout:
        match = time_regex.search(line)
        if match:
            cur_s = time_to_seconds(match.group(1))
            elapsed = time.time() - start_t
            speed = cur_s / elapsed if elapsed > 0 else 0
            pct = (cur_s / duration) * 100 if duration > 0 else 0
            sys.stdout.write(f"\r📦 {batch_str} | {file_label:<10} | {pct:5.1f}% | Speed: {speed:3.1f}x | Target: {target_size:.1f}MB")
            sys.stdout.flush()
    process.wait()
    return process.returncode == 0

def process_video(input_path, output_path, data, batch_str, file_label):
    fname = os.path.basename(input_path)
    base_name = os.path.splitext(fname)[0]
    mode, time_points = data['mode'], data['times']

    src_w, src_h, total_dur, src_fps = get_video_metadata(input_path)
    src_size = os.path.getsize(input_path) / (1024*1024)

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

    if not segments: return False

    target_h = min(src_h, TARGET_HEIGHT)
    mb_rate = get_mb_per_minute_ratio(target_h)
    target_size_mb = mb_rate * (total_trimmed_dur / 60)

    if mode == 'E' and target_size_mb >= (src_size * (1.0 - (SKIP_ENCODE_MARGIN_PERCENT / 100.0))):
        mode = 'T'

    bitrate = int((target_size_mb * 8192 - (96 * total_trimmed_dur)) / total_trimmed_dur) if total_trimmed_dur > 0 else 1000
    fps_filter = ",fps=fps=30" if src_fps > 30.5 else ""
    vf = f"scale=w='min(iw,{TARGET_WIDTH})':h='min(ih,{TARGET_HEIGHT})':force_original_aspect_ratio=decrease{fps_filter},setsar=1,scale=trunc(iw/2)*2:trunc(ih/2)*2"

    if len(segments) == 1:
        start, dur = segments[0]
        if mode == 'T':
            cmd = ['ffmpeg', '-y', '-ss', start, '-i', input_path, '-t', str(dur), '-c', 'copy', '-map', '0', '-movflags', '+faststart', output_path]
            return run_ffmpeg_process(cmd, dur, file_label, 0, "DIRECT TRIM", batch_str)
        else:
            cmd = ['ffmpeg', '-y', '-ss', start, '-i', input_path, '-t', str(dur), '-vf', vf, '-c:v', 'libx264', '-crf', str(TARGET_CRF_VALUE), '-pix_fmt', 'yuv420p', '-profile:v', 'high', '-preset', 'medium', '-maxrate', f"{bitrate}k", '-bufsize', f"{bitrate*2}k", '-c:a', 'aac', '-b:a', '96k', '-movflags', '+faststart', output_path]
            return run_ffmpeg_process(cmd, dur, file_label, target_size_mb, "DIRECT ENCODE", batch_str)

    else:
        # YOUR MULTI-SEGMENT LOGIC
        temp_work_dir = os.path.join(TEMP_DIR, "parts")
        os.makedirs(temp_work_dir, exist_ok=True)
        segment_files = []
        for i, (start, dur) in enumerate(segments):
            seg_path = os.path.join(temp_work_dir, f"part_{i}.mp4")
            segment_files.append(seg_path)
            if mode == 'T':
                cmd = ['ffmpeg', '-y', '-ss', start, '-i', input_path, '-t', str(dur), '-c', 'copy', '-map', '0', '-movflags', '+faststart', seg_path]
            else:
                cmd = ['ffmpeg', '-y', '-ss', start, '-i', input_path, '-t', str(dur), '-vf', vf, '-c:v', 'libx264', '-crf', str(TARGET_CRF_VALUE), '-pix_fmt', 'yuv420p', '-profile:v', 'high', '-maxrate', f"{bitrate}k", '-bufsize', f"{bitrate*2}k", '-c:a', 'aac', '-b:a', '96k', '-movflags', '+faststart', seg_path]
            run_ffmpeg_process(cmd, dur, file_label, 0, f"Segment {i+1}", batch_str)

        list_path = os.path.join(temp_work_dir, "list.txt")
        with open(list_path, 'w') as f:
            for s in segment_files: f.write(f"file '{os.path.abspath(s)}'\n")
        
        subprocess.run(['ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', list_path, '-c', 'copy', '-movflags', '+faststart', output_path], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        shutil.rmtree(temp_work_dir)
        return os.path.exists(output_path)

# --- MAIN EXECUTION ---
if __name__ == "__main__":
    service = get_drive_service()
    
    # 1. Get Trim Config
    request = service.files().get_media(fileId=TRIM_FILE_ID)
    trim_content = request.execute().decode('utf-8')
    trim_data = {}
    for line in trim_content.splitlines():
        if not line or line.startswith('#'): continue
        parts = [p.strip() for p in line.split('---') if p.strip()]
        if len(parts) >= 4:
            trim_data[parts[0]] = {'mode': parts[1].upper(), 'times': parts[2:]}

    # 2. List Files
    results = service.files().list(q=f"'{INPUT_FOLDER_ID}' in parents and trashed = false", fields="files(id, name)").execute()
    files = results.get('files', [])
    valid_files = [f for f in files if f['name'] in trim_data]
    valid_files.sort(key=lambda x: x['name'])

    for i, f_info in enumerate(valid_files):
        f_name = f_info['name']
        f_id = f_info['id']
        file_label = f"File {i+1}"
        batch_str = f"[{i+1}/{len(valid_files)}]"
        
        # SKIP LOGIC
        out_name = os.path.splitext(f_name)[0].replace(".", " ") + ".mp4"
        if file_exists_in_drive(service, out_name, OUTPUT_FOLDER_ID):
            print(f"✅ {batch_str} {file_label} Already Done. Skipping.")
            continue

        # DOWNLOAD
        print(f"\n📥 Downloading {file_label}...")
        local_in = os.path.join(TEMP_DIR, "input.tmp")
        with io.FileIO(local_in, 'wb') as fh:
            downloader = MediaIoBaseDownload(fh, service.files().get_media(fileId=f_id))
            done = False
            while not done:
                _, done = downloader.next_chunk()

        # PROCESS
        local_out = os.path.join(TEMP_DIR, "output.mp4")
        if process_video(local_in, local_out, trim_data[f_name], batch_str, file_label):
            # UPLOAD
            file_metadata = {'name': out_name, 'parents': [OUTPUT_FOLDER_ID]}
            media = MediaFileUpload(local_out, mimetype='video/mp4', resumable=True)
            service.files().create(body=file_metadata, media_body=media).execute()
            print(f"✅ {file_label} Uploaded.")
        
        # CLEANUP
        if os.path.exists(local_in): os.remove(local_in)
        if os.path.exists(local_out): os.remove(local_out)
