#!/usr/bin/env python3
"""
M6+ Video Downloader with ffmpeg decryption

Downloads DASH segments (video + audio), then uses ffmpeg to decrypt
and mux into a playable MKV file.

Usage:
    python3 m6-downloader.py <mpd_url> <output_file> <key_id:key> [--progress-file <file>]
"""

import sys
import os
import json
import re
import time
import argparse
import subprocess
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlparse
import requests

# Progress tracking
progress = {
    'status': 'initializing',
    'total_segments': 0,
    'downloaded_segments': 0,
    'written_bytes': 0,
    'error': None,
    'playable': False,
    'duration': 0
}

def log(msg):
    print(f"[M6-DL] {msg}", flush=True)

def save_progress(progress_file):
    """Save progress to file for Node.js to read"""
    if progress_file:
        try:
            with open(progress_file, 'w') as f:
                json.dump(progress, f)
        except:
            pass

def parse_mpd(mpd_content, base_url):
    """Parse MPD and extract segment URLs"""
    # Remove namespaces for easier parsing
    mpd_content = re.sub(r'\sxmlns(?::[a-zA-Z0-9_-]+)?="[^"]*"', '', mpd_content)
    mpd_content = re.sub(r'<([a-zA-Z0-9_-]+):', r'<', mpd_content)
    mpd_content = re.sub(r'</([a-zA-Z0-9_-]+):', r'</', mpd_content)
    mpd_content = re.sub(r'\s([a-zA-Z0-9_-]+):([a-zA-Z0-9_-]+)=', r' \2=', mpd_content)

    try:
        root = ET.fromstring(mpd_content)
    except ET.ParseError as e:
        log(f"XML Parse error: {e}")
        raise

    segments = {
        'video': {'init': None, 'media': [], 'timescale': 1},
        'audio': {'init': None, 'media': [], 'timescale': 1}
    }

    # Get base URL from MPD if present
    base_url_elem = root.find('.//BaseURL')
    if base_url_elem is not None and base_url_elem.text:
        base_url = urljoin(base_url, base_url_elem.text)

    for adaptation_set in root.findall('.//AdaptationSet'):
        mime_type = adaptation_set.get('mimeType', '')
        content_type = adaptation_set.get('contentType', '')

        if 'video' in mime_type or content_type == 'video':
            track_type = 'video'
        elif 'audio' in mime_type or content_type == 'audio':
            track_type = 'audio'
        else:
            continue

        representations = adaptation_set.findall('.//Representation')
        if not representations:
            continue

        # Sort by bandwidth (highest for video)
        if track_type == 'video':
            representations.sort(key=lambda r: int(r.get('bandwidth', 0)), reverse=True)

        rep = representations[0]
        bandwidth = rep.get('bandwidth', 'unknown')
        log(f"Selected {track_type}: bandwidth={bandwidth}")

        seg_template = rep.find('.//SegmentTemplate')
        if seg_template is None:
            seg_template = adaptation_set.find('.//SegmentTemplate')

        if seg_template is None:
            continue

        timescale = int(seg_template.get('timescale', 1))
        segments[track_type]['timescale'] = timescale

        # Get initialization URL
        init_template = seg_template.get('initialization', '')
        if init_template:
            init_url = init_template.replace('$RepresentationID$', rep.get('id', ''))
            init_url = init_url.replace('$Bandwidth$', rep.get('bandwidth', ''))
            if not init_url.startswith('http'):
                init_url = urljoin(base_url, init_url)
            segments[track_type]['init'] = init_url

        # Get media template
        media_template = seg_template.get('media', '')

        # Get segment timeline
        timeline = seg_template.find('.//SegmentTimeline')
        if timeline is not None:
            current_time = 0
            for s in timeline.findall('.//S'):
                t = int(s.get('t', current_time))
                d = int(s.get('d', 0))
                r = int(s.get('r', 0))

                for i in range(r + 1):
                    seg_time = t + (i * d)
                    media_url = media_template.replace('$RepresentationID$', rep.get('id', ''))
                    media_url = media_url.replace('$Bandwidth$', rep.get('bandwidth', ''))
                    media_url = media_url.replace('$Time$', str(seg_time))

                    if not media_url.startswith('http'):
                        media_url = urljoin(base_url, media_url)

                    segments[track_type]['media'].append({
                        'url': media_url,
                        'time': seg_time,
                        'duration': d
                    })

                current_time = t + ((r + 1) * d)

    # Calculate total duration
    if segments['video']['media']:
        last_seg = segments['video']['media'][-1]
        duration = (last_seg['time'] + last_seg['duration']) / segments['video']['timescale']
        progress['duration'] = duration
        log(f"Video duration: {duration:.1f} seconds")

    log(f"Found {len(segments['video']['media'])} video segments, {len(segments['audio']['media'])} audio segments")

    return segments

def download_segment(url, session, retries=3):
    """Download a single segment with retries"""
    for attempt in range(retries):
        try:
            response = session.get(url, timeout=30)
            response.raise_for_status()
            return response.content
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(1)
            else:
                raise

def download_track(segments, track_type, output_path, session, progress_callback=None):
    """Download all segments for a track (video or audio) to a file"""
    track = segments[track_type]

    if not track['init'] or not track['media']:
        log(f"No {track_type} track found")
        return False

    with open(output_path, 'wb') as f:
        # Download init segment
        log(f"Downloading {track_type} init segment...")
        init_data = download_segment(track['init'], session)
        f.write(init_data)

        # Download media segments
        total = len(track['media'])
        for i, seg_info in enumerate(track['media']):
            seg_data = download_segment(seg_info['url'], session)
            f.write(seg_data)

            if progress_callback and (i + 1) % 10 == 0:
                progress_callback(track_type, i + 1, total)

    log(f"{track_type} download complete: {output_path}")
    return True

def decrypt_with_mp4decrypt(input_path, output_path, kid, key):
    """Decrypt a CENC-encrypted segment using mp4decrypt (Bento4)"""
    cmd = [
        'mp4decrypt',
        '--key', f'{kid}:{key}',
        input_path,
        output_path
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        log(f"mp4decrypt error: {result.stderr}")
        return False
    return True

def decrypt_and_mux(video_path, audio_path, output_path, kid, key):
    """Decrypt video and audio, then mux into MKV"""
    log(f"Decrypting with mp4decrypt (Bento4)...")
    log(f"Key ID: {kid}")

    temp_dir = os.path.dirname(output_path)
    video_dec = os.path.join(temp_dir, f"video_dec_{os.getpid()}.mp4")
    audio_dec = os.path.join(temp_dir, f"audio_dec_{os.getpid()}.mp4")

    try:
        # Decrypt video with mp4decrypt
        log("Decrypting video...")
        if not decrypt_with_mp4decrypt(video_path, video_dec, kid, key):
            log("Video decryption failed!")
            return False

        # Decrypt audio with mp4decrypt
        log("Decrypting audio...")
        if not decrypt_with_mp4decrypt(audio_path, audio_dec, kid, key):
            log("Audio decryption failed!")
            return False

        # Mux with ffmpeg (no decryption needed now)
        log("Muxing with ffmpeg...")
        cmd = [
            'ffmpeg', '-y',
            '-i', video_dec,
            '-i', audio_dec,
            '-c:v', 'copy',
            '-c:a', 'copy',
            '-movflags', '+faststart',
            output_path
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            log(f"ffmpeg mux error: {result.stderr[-300:]}")
            return False

        log("Muxing completed successfully")
        return True

    except Exception as e:
        log(f"Decrypt/mux error: {e}")
        import traceback
        traceback.print_exc()
        return False

    finally:
        # Cleanup temp files
        for f in [video_dec, audio_dec]:
            try:
                os.remove(f)
            except:
                pass

def main():
    parser = argparse.ArgumentParser(description='M6+ Video Downloader')
    parser.add_argument('mpd_url', help='URL of the MPD manifest')
    parser.add_argument('output_file', help='Output file path (will be .mkv)')
    parser.add_argument('keys', help='Decryption keys in format kid:key,kid2:key2')
    parser.add_argument('--progress-file', help='File to write progress JSON')
    parser.add_argument('--buffer-segments', type=int, default=10,
                        help='Number of segments to buffer before marking playable')

    args = parser.parse_args()
    progress_file = args.progress_file

    # Ensure output is MKV
    output_file = args.output_file
    if not output_file.endswith('.mkv'):
        output_file = output_file.rsplit('.', 1)[0] + '.mkv'

    try:
        # Parse keys
        keys_hex = {}
        for key_pair in args.keys.split(','):
            if ':' in key_pair:
                kid, key = key_pair.split(':')
                keys_hex[kid.lower()] = key.lower()

        if not keys_hex:
            raise ValueError("No valid keys provided")

        log(f"Loaded {len(keys_hex)} decryption key(s)")
        progress['keys'] = keys_hex

        # Create session for downloads
        session = requests.Session()
        session.headers['User-Agent'] = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'

        # Fetch and parse MPD
        progress['status'] = 'fetching_mpd'
        save_progress(progress_file)

        log(f"Fetching MPD: {args.mpd_url[:80]}...")
        mpd_response = session.get(args.mpd_url, timeout=30)
        mpd_response.raise_for_status()

        base_url = mpd_response.url
        mpd_content = mpd_response.text

        segments = parse_mpd(mpd_content, base_url)

        total_video = len(segments['video']['media'])
        total_audio = len(segments['audio']['media'])
        progress['total_segments'] = total_video + total_audio
        progress['status'] = 'downloading'
        save_progress(progress_file)

        # Create temp directory
        temp_dir = os.path.dirname(output_file)
        video_temp = os.path.join(temp_dir, f"video_temp_{os.getpid()}.mp4")
        audio_temp = os.path.join(temp_dir, f"audio_temp_{os.getpid()}.mp4")

        downloaded = [0]

        def update_progress(track, current, total):
            downloaded[0] += 10
            progress['downloaded_segments'] = downloaded[0]
            pct = (downloaded[0] / progress['total_segments']) * 100
            log(f"Progress: {downloaded[0]}/{progress['total_segments']} ({pct:.1f}%)")

            # Mark playable early for preview
            if downloaded[0] >= args.buffer_segments and not progress['playable']:
                progress['playable'] = True
                log("Buffer ready - starting mux...")

            save_progress(progress_file)

        # Download video
        log("Downloading video track...")
        download_track(segments, 'video', video_temp, session, update_progress)

        # Download audio
        log("Downloading audio track...")
        download_track(segments, 'audio', audio_temp, session, update_progress)

        # Decrypt and mux
        progress['status'] = 'decrypting'
        save_progress(progress_file)

        # Get first key for decryption
        kid, key = list(keys_hex.items())[0]

        if decrypt_and_mux(video_temp, audio_temp, output_file, kid, key):
            progress['status'] = 'completed'
            progress['playable'] = True

            # Get file size
            if os.path.exists(output_file):
                progress['written_bytes'] = os.path.getsize(output_file)
                log(f"Output file: {output_file} ({progress['written_bytes']} bytes)")
        else:
            progress['status'] = 'error'
            progress['error'] = 'ffmpeg decryption failed'

        # Cleanup temp files
        for f in [video_temp, audio_temp]:
            try:
                os.remove(f)
            except:
                pass

        save_progress(progress_file)
        log(f"Download {'completed' if progress['status'] == 'completed' else 'failed'}")

    except Exception as e:
        log(f"Fatal error: {e}")
        import traceback
        traceback.print_exc()
        progress['status'] = 'error'
        progress['error'] = str(e)
        save_progress(progress_file)
        sys.exit(1)

if __name__ == '__main__':
    main()
