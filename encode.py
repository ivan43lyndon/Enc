import os
import subprocess
import re
import sys
import time
import io
import json
import warnings
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload
from google.oauth2.credentials import Credentials as UserCredentials

# Prevent Google's EOL warnings from cluttering your phone's logs
warnings.filterwarnings("ignore", category=FutureWarning)

# --- CONFIGURATION (RESTORED IDs) ---
START_TIME = time.time()
MAX_RUN_TIME = 5.75 * 3600  
TARGET_WIDTH = 1280
TARGET_HEIGHT = 720
AUDIO_BITRATE_KBPS = 96
TARGET_CRF_VALUE = 22
FADE_DURATION = 1

DRIVE_TOKEN = os.environ.get('DRIVE_TOKEN')
INPUT_FOLDER_ID = '1G7nC7CrMi_8HdtVGxdR-aNdak9FrVAcd' 
OUTPUT_FOLDER_ID = '14KAhaiTisjuybP2Pc6mcbLau8JoyDq5y'
CONFIG_FILE_ID = '1rE51zdRaXCIrxmWZhRjRZaKIuRvadDo3'

# --- GITHUB AUTH WRAPPER ---
def get_drive_service():
    raw = DRIVE_TOKEN.strip()
    if (raw.startswith("'") and raw.endswith("'")) or (raw.startswith('"') and raw.endswith('"')):
        raw = raw[1:-1]
    # Handle both dict-style and string-style tokens
    try:
        data = json.loads(raw)
    except:
        data = eval(raw)
    
    creds = UserCredentials(
        token=data.get('token'),
        refresh_token=data.get('refresh_token'),
        token_uri=data.get('token_uri'),
        client_id=data.get('client_id'),
        client_secret=data.get('client_secret')
    )
    return build('drive', 'v3', credentials=creds)

# --- YOUR ORIGINAL UTILITIES ---
def get_mb_per_minute_ratio(height):
    if height >= 1080: return 12.0
    elif height >= 720: return 8.0
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
        dur = float(d_res) if d_res else 0.0
        return w, h, dur, fps
    except: return 0, 0, 0.0, 30.0

# --- CORE ENCODING ENGINE (YOUR COLAB LOGIC) ---
def process_video(service, file_id, fname, data):
    temp_in = "temp_in.mp4"
    final_out = "final_out.mp4"
    
    # Download file from Drive to Runner
    request = service.files().get_media(fileId=file_id)
    with io.FileIO(temp_in, 'wb') as fh:
        downloader = MediaIoBaseDownload(fh, request, chunksize=1024*1024*10) # 10MB chunks
        done = False
        while not done:
            _, done = downloader.next_chunk()

    w, h, total_dur, fps = get_video_metadata(temp_in)
    mode, time_points, do_fade = data['mode'], data['times'], data['fade']
    
    segment_files = []
    
    # EXACT REPLICA OF YOUR SEGMENT LOOP
    for i in range(0, len(time_points), 2):
        if i+1 >= len(time_points): break
        s_start = time_to_seconds(time_points[i])
        s_end = min(time_to_seconds(time_points[i+1]), total_dur if total_dur > 0 else 999999)
        s_dur = s_end - s_start
        
        if s_dur > 0:
            seg_name = f"seg_{i//2}.mp4"
            target_h = min(h, TARGET_HEIGHT)
            mb_rate = get_mb_per_minute_ratio(target_h)
            target_size_mb = mb_rate * (s_dur / 60)
            bitrate = int((target_size_mb * 8192 - (96 * s_dur)) / s_dur)
            
            vf = f"scale=w='min(iw,{TARGET_WIDTH})':h='min(ih,{TARGET_HEIGHT})':force_original_aspect_ratio=decrease,fps=30,setsar=1,scale=trunc(iw/2)*2:trunc(ih/2)*2"
            
            # Your exact FFMPEG command
            cmd = ['ffmpeg', '-y', '-ss', str(s_start), '-i', temp_in, '-t', str(s_dur), 
                   '-vf', vf, '-c:v', 'libx264', '-crf', str(TARGET_CRF_VALUE), 
                   '-maxrate', f"{bitrate}k", '-bufsize', f"{bitrate*2}k", '-preset', 'medium', 
                   '-c:a', 'aac', '-b:a', '96k', '-movflags', '+faststart', seg_name]
            
            print(f"ENCODING SEGMENT: {i//2} for {fname}", flush=True)
            subprocess.run(cmd, stderr=subprocess.DEVNULL)
            segment_files.append(seg_name)

    # Your exact concatenation logic
    if len(segment_files) > 1:
        with open("concat.txt", "w") as f:
            for seg in segment_files: f.write(f"file '{seg}'\n")
        subprocess.run(['ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', 'concat.txt', '-c', 'copy', final_out], stderr=subprocess.DEVNULL)
        for s in segment_files: os.remove(s)
        os.remove("concat.txt")
    elif len(segment_files) == 1:
        os.rename(segment_files[0], final_out)

    # Your exact Fade logic
    if do_fade and os.path.exists(final_out):
        _, _, final_d, _ = get_video_metadata(final_out)
        f_cmd = ['ffmpeg', '-y', '-i', final_out, '-vf', f"fade=t=out:st={final_d-1}:d=1", '-af', f"afade=t=out:st={final_d-1}:d=1", "faded.mp4"]
        subprocess.run(f_cmd, stderr=subprocess.DEVNULL)
        os.replace("faded.mp4", final_out)

    # Upload result back to Output Folder
    if os.path.exists(final_out):
        clean_name = os.path.splitext(fname)[0].replace(".", " ") + ".mp4"
        media = MediaFileUpload(final_out, resumable=True)
        service.files().create(body={'name': clean_name, 'parents': [OUTPUT_FOLDER_ID]}, media_body=media).execute()
        os.remove(final_out)
    
    if os.path.exists(temp_in): os.remove(temp_in)
    return True

# --- MAIN EXECUTION LOOP ---
if __name__ == "__main__":
    service = get_drive_service()
    
    # Download Config File
    req = service.files().get_media(fileId=CONFIG_FILE_ID)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, req)
    done = False
    while not done: _, done = downloader.next_chunk()
    trim_data = parse_trim_file(fh.getvalue().decode())
    
    # Process Files
    results = service.files().list(q=f"'{INPUT_FOLDER_ID}' in parents and trashed = false", fields="files(id, name)").execute()
    for f in sorted(results.get('files', []), key=lambda x: x['name']):
        if f['name'] in trim_data:
            # Check if already processed
            out_check = os.path.splitext(f['name'])[0].replace(".", " ") + ".mp4"
            check_q = f"name = '{out_check}' and '{OUTPUT_FOLDER_ID}' in parents and trashed = false"
            exists = service.files().list(q=check_q).execute().get('files', [])
            
            if not exists:
                process_video(service, f['id'], f['name'], trim_data[f['name']])
            else:
                print(f"SKIPPING: {f['name']} (Already Exists)", flush=True)
