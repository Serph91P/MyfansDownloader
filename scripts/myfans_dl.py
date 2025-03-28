import os
import sys
import time
import json
from queue import Queue, Empty
import subprocess
import configparser
from tqdm import tqdm
from scripts.filename_utils import *
import concurrent.futures
import threading
import m3u8
import logging
from logging.handlers import RotatingFileHandler
from typing import Optional, List, Dict, Any, Tuple
from urllib.parse import urljoin
import requests
from requests import Session
import re
from urllib3.util import Retry

# Get log file path from environment or use default
log_file = os.getenv('LOG_FILE', 'myfans_downloader.log')

# Create logger
logger = logging.getLogger('myfans_downloader')
logger.setLevel(logging.INFO)

# Create handlers
console_handler = logging.StreamHandler()
file_handler = RotatingFileHandler(log_file, maxBytes=10485760, backupCount=5)  # 10MB file size

# Create formatters
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
console_handler.setFormatter(formatter)
file_handler.setFormatter(formatter)

# Add handlers to logger
logger.addHandler(console_handler)
logger.addHandler(file_handler)

# Prevent log propagation to avoid duplicate logs
logger.propagate = False

# Am Anfang der Datei nach den Imports hinzufügen:
log_lock = threading.Lock()

# Neue Hilfsfunktion für Thread-sicheres Logging
def thread_safe_log(level, message, progress_queue=None):
    with log_lock:
        if level == 'info':
            logger.info(message)
        elif level == 'error':
            logger.error(message)
        elif level == 'warning':
            logger.warning(message)
        elif level == 'debug':
            logger.debug(message)
        
        if progress_queue:
            progress_queue.put(message)

def read_headers_from_file(filename):
    headers = {}
    config_dir = os.getenv('CONFIG_DIR', '')
    header_path = os.path.join(config_dir, filename)
    
    if not os.path.isfile(header_path):
        raise FileNotFoundError(f"Header file not found at {header_path}")
        
    with open(header_path, 'r') as file:
        for line in file:
            if ': ' in line:
                key, value = line.strip().split(': ', 1)
                headers[key.lower()] = value
    
    # Validate token presence
    if 'authorization' not in headers or not headers['authorization'].startswith('Token token='):
        raise ValueError("Invalid or missing authorization token in headers file")
        
    return headers

def get_posts_for_page(base_url, page, headers):
    url = base_url + str(page)
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    json_data = response.json()
    return json_data.get("data", [])

def verify_video_file(file_path: str) -> bool:
    """Verify if a video file is valid"""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", file_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        return result.returncode == 0
    except Exception as e:
        logger.error(f"Error verifying video file {file_path}: {e}")
        return False

def safe_urljoin(base: str, url: str) -> str:
    """Safely join URL parts ensuring no None values"""
    if not base or not url:
        raise ValueError("Base URL and URL parts must not be None")
    return urljoin(base, url)

def make_request(session: requests.Session, url: str, headers: dict, timeout: int = 30, max_retries: int = 5) -> requests.Response:
    """Make a request with automatic retry for connection resets"""
    if not url:
        raise ValueError("URL cannot be None")
    
    retry_count = 0
    while retry_count < max_retries:
        try:
            return session.get(url, headers=headers, timeout=timeout)
        except (requests.exceptions.ConnectionError, requests.exceptions.ReadTimeout) as e:
            retry_count += 1
            wait_time = min(2 ** retry_count, 60)  # Exponential backoff, max 60s
            logger.warning(f"Connection error on attempt {retry_count}/{max_retries}: {str(e)}")
            logger.info(f"Waiting {wait_time}s before retry...")
            time.sleep(wait_time)
            
            # Erstelle eine neue Session nach einem Verbindungsabbruch
            if retry_count >= 3:
                logger.info("Creating new session after connection failures")
                session = requests.Session()
                session.headers.update(headers)
                
            if retry_count == max_retries:
                logger.error(f"Max retries reached for URL: {url}")
                raise

def DL_File(m3u8_url_download, output_file, input_post_id, max_retries=3, retry_delay=5, progress_queue=None, download_state=None):
    try:
        # Get segment download threads from environment or use default
        segment_threads = int(os.getenv('SEGMENT_DOWNLOAD_THREADS', '20'))
        logger.info(f"Using {segment_threads} threads for segment downloads")
        
        # Add M3U8 URL validation
        if not m3u8_url_download:
            logger.error(f"Invalid M3U8 URL for post {input_post_id}")
            return False

        # Check if file already exists and is complete
        if os.path.exists(output_file) and os.path.getsize(output_file) > 0:
            if verify_video_file(output_file):
                message = f"Verified existing file: {os.path.basename(output_file)}"
                logger.info(message)
                if progress_queue:
                    progress_queue.put(message)
                if download_state:
                    download_state.mark_completed(input_post_id)
                return True
            else:
                message = f"Corrupted file found, redownloading: {os.path.basename(output_file)}"
                logger.warning(message)
                if progress_queue:
                    progress_queue.put(message)
                os.remove(output_file)

        # Setup directories
        output_folder = os.path.dirname(output_file)
        ts_file = output_file.replace('.mp4', '.ts')
        temp_folder = output_file.replace('.mp4', '.ts_parts')
        
        os.makedirs(output_folder, exist_ok=True)
        os.makedirs(temp_folder, exist_ok=True)

        # Setup session with headers and retry mechanism
        headers = read_headers_from_file("header.txt")
        session = requests.Session()
        session.headers.update(headers)
        
        # Use connection pooling for better performance
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=segment_threads,
            pool_maxsize=segment_threads,
            max_retries=Retry(
                total=5,
                backoff_factor=0.5,
                status_forcelist=[500, 502, 503, 504],
                allowed_methods=["GET", "HEAD"]
            )
        )
        session.mount('http://', adapter)
        session.mount('https://', adapter)


        for attempt in range(max_retries):
            try:
                # Get master playlist
                logger.info(f"Fetching master M3U8 from URL: {m3u8_url_download}")
                response = session.get(m3u8_url_download, timeout=30)
                response.raise_for_status()
                master_content = response.text

                # Parse master playlist
                master_playlist = m3u8.loads(master_content)
                master_playlist.base_uri = os.path.dirname(m3u8_url_download) + '/'

                if not master_playlist.playlists:
                    logger.error(f"No variants found in master playlist")
                    continue

                # Get highest quality variant
                variant = sorted(
                    [p for p in master_playlist.playlists if p.stream_info and p.stream_info.bandwidth],
                    key=lambda x: x.stream_info.bandwidth,
                    reverse=True
                )[0]

                # Get variant playlist URL
                base_uri = os.path.dirname(m3u8_url_download)
                if not base_uri:
                    base_uri = m3u8_url_download
                variant_url = safe_urljoin(base_uri + '/', variant.uri if variant.uri else '')
                logger.info(f"Fetching variant playlist from: {variant_url}")

                # Get variant playlist
                response = session.get(variant_url, timeout=30)
                response.raise_for_status()
                variant_content = response.text

                # Parse variant playlist
                playlist = m3u8.loads(variant_content)
                playlist.base_uri = os.path.dirname(variant_url) + '/'

                if not playlist.segments:
                    logger.error(f"No segments found in variant playlist")
                    continue

                total_segments = len(playlist.segments)
                logger.info(f"Found {total_segments} segments for post {input_post_id}")
                
                if progress_queue:
                    progress_queue.put(f"Downloading {total_segments} segments with {segment_threads} parallel threads")

                # Download segments concurrently
                segment_files = [None] * total_segments  # Pre-allocate list with correct order
                processed_count = 0
                
                def download_segment(i, segment):
                    nonlocal processed_count
                    
                    if not segment.uri:
                        logger.error(f"Invalid segment {i}: missing URI")
                        return i, None
                        
                    seg_path = os.path.join(temp_folder, f"segment_{i:05d}.ts")
                    
                    # Skip if segment already exists
                    if os.path.exists(seg_path) and os.path.getsize(seg_path) > 0:
                        return i, seg_path
                        
                    # Try to download segment with retries
                    for seg_retry in range(3):
                        try:
                            seg_url = safe_urljoin(playlist.base_uri, segment.uri) if not segment_uri_is_absolute(segment.uri) else segment.uri
                            response = session.get(seg_url, timeout=30)
                            response.raise_for_status()

                            with open(seg_path, 'wb') as f:
                                f.write(response.content)

                            if os.path.exists(seg_path) and os.path.getsize(seg_path) > 0:
                                return i, seg_path
                        except Exception as e:
                            logger.error(f"Error downloading segment {i}: {str(e)}")
                            if seg_retry == 2:  # Last attempt
                                return i, None
                            time.sleep(retry_delay)
                    
                    return i, None

                # Use ThreadPoolExecutor for concurrent downloads
                with tqdm(total=total_segments, desc=f"Segments for {input_post_id}") as pbar:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=segment_threads) as executor:
                        futures = {executor.submit(download_segment, i, segment): i 
                                  for i, segment in enumerate(playlist.segments)}
                        
                        # Process completed downloads as they finish
                        for future in concurrent.futures.as_completed(futures):
                            try:
                                idx, file_path = future.result()
                                if file_path:
                                    segment_files[idx] = file_path
                                pbar.update(1)
                                processed_count += 1
                                
                                # Log progress occasionally
                                if processed_count % 50 == 0 or processed_count == total_segments:
                                    success_rate = len([f for f in segment_files if f]) / processed_count * 100
                                    thread_safe_log('info', f"Progress: {processed_count}/{total_segments} segments ({success_rate:.1f}% success)", progress_queue)
                            except Exception as e:
                                logger.error(f"Error processing segment result: {str(e)}")
                
                # Filter out None values (failed downloads)
                valid_segments = [f for f in segment_files if f]
                success_rate = len(valid_segments) / total_segments * 100
                
                logger.info(f"Downloaded {len(valid_segments)}/{total_segments} segments ({success_rate:.1f}% success)")
                if progress_queue:
                    progress_queue.put(f"Downloaded {len(valid_segments)}/{total_segments} segments ({success_rate:.1f}% success)")

                if len(valid_segments) < total_segments * 0.9:  # Less than 90% segments downloaded
                    logger.error(f"Too many failed segments: only {success_rate:.1f}% downloaded successfully")
                    if attempt < max_retries - 1:  # Not the last attempt
                        logger.info(f"Retrying download, attempt {attempt + 2}/{max_retries}")
                        continue

                # Merge segments
                logger.info("Merging segments...")
                if progress_queue:
                    progress_queue.put("Merging segments...")
                
                with open(ts_file, 'wb') as outfile:
                    for seg_file in valid_segments:
                        if os.path.exists(seg_file):
                            with open(seg_file, 'rb') as infile:
                                outfile.write(infile.read())

                # Convert to MP4
                logger.info("Converting to MP4...")
                if progress_queue:
                    progress_queue.put("Converting to MP4...")
                
                result = subprocess.run(
                    ["ffmpeg", "-y", "-i", ts_file, "-c", "copy", output_file],
                    capture_output=True,
                    text=True
                )

                if result.returncode != 0:
                    logger.error(f"FFmpeg error: {result.stderr}")
                    continue

                # Verify final file
                if verify_video_file(output_file):
                    # Cleanup
                    try:
                        if os.path.exists(ts_file):
                            os.remove(ts_file)
                        
                        # Delete segments
                        for seg_file in valid_segments:
                            if os.path.exists(seg_file):
                                os.remove(seg_file)
                                
                        # Remove temp directory
                        if os.path.exists(temp_folder):
                            os.rmdir(temp_folder)
                    except Exception as e:
                        logger.warning(f"Error during cleanup: {str(e)}")
                    
                    logger.info(f"Successfully downloaded {input_post_id}")
                    if progress_queue:
                        progress_queue.put(f"Successfully downloaded {input_post_id}")
                    
                    return True

            except Exception as e:
                logger.error(f"Download attempt {attempt + 1} failed: {str(e)}")
                if progress_queue:
                    progress_queue.put(f"Download attempt {attempt + 1} failed: {str(e)}")
                
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)

        return False

    except Exception as e:
        logger.exception(f"Fatal error in DL_File: {str(e)}")
        if progress_queue:
            progress_queue.put(f"Fatal error: {str(e)}")
        return False

def segment_uri_is_absolute(uri: str) -> bool:
    return uri.lower().startswith(("http://", "https://"))

def process_post_id(input_post_id, session, headers, selected_resolution, output_dir, filename_config, progress_bar=None, progress_queue=None):
    try:
        # Use the passed session instead of creating new ones
        data, resolution_info, error = get_video_info(input_post_id, session, headers)
        
        if error:
            message = f"Error fetching video info for post ID {input_post_id}: {error}"
            logger.error(message)
            if progress_queue:
                progress_queue.put(message)
            return False

        # Log available resolutions
        if resolution_info:
            logger.info(f"Available resolutions for post {input_post_id}: {list(resolution_info.keys())}")
        else:
            logger.error(f"No resolution info available for post {input_post_id}")
            return False

        # Check if it's a video post
        if not data.get('videos', {}).get('main'):
            message = f"Post ID {input_post_id} is not a video post"
            logger.error(message)
            if progress_queue:
                progress_queue.put(message)
            return False

        # Select resolution with fallback logging
        if selected_resolution == 'best':
            for res in ['fhd', 'hd', 'sd', 'ld']:
                if res in resolution_info:
                    selected_resolution = res
                    logger.info(f"Selected best available resolution for post {input_post_id}: {res}")
                    break

        # Verify selected resolution exists
        if selected_resolution not in resolution_info:
            available = ', '.join(resolution_info.keys())
            message = f"Resolution {selected_resolution} not available for post {input_post_id}. Available: {available}"
            logger.warning(message)
            if progress_queue:
                progress_queue.put(message)
            # Try fallback
            for res in ['fhd', 'hd', 'sd', 'ld']:
                if res in resolution_info:
                    selected_resolution = res
                    message = f"Falling back to {res} resolution"
                    logger.info(message)
                    if progress_queue:
                        progress_queue.put(message)
                    break
            else:
                logger.error(f"No valid resolution found for post {input_post_id}")
                return False

        # Get video URL
        video_url = resolution_info[selected_resolution].get("url")
        if not video_url:
            logger.error(f"No video URL found for post {input_post_id}")
            return False

        # Log video URL (masked for security)
        masked_url = video_url[:30] + "..." + video_url[-30:] if len(video_url) > 60 else video_url
        logger.info(f"Video URL for post {input_post_id}: {masked_url}")

        # Check access level with detailed logging
        logger.info(f"Post {input_post_id} - Free: {data.get('free')}, Subscribed: {data.get('subscribed')}")
        if data.get('free') is False and not data.get('subscribed'):
            message = f"No access to post ID {input_post_id} (subscription required)"
            logger.error(message)
            if progress_queue:
                progress_queue.put(message)
            return False

        # Validate URL before attempting download
        if not validate_video_url(video_url, headers):
            logger.error(f"Video URL validation failed for post {input_post_id}")
            return False

        # Log video URL (masked for security)
        masked_url = video_url[:30] + "..." + video_url[-30:] if len(video_url) > 60 else video_url
        logger.info(f"Video URL for post {input_post_id}: {masked_url}")

        # Validate URL accessibility
        try:
            head_response = session.head(video_url)
            head_response.raise_for_status()
            logger.info(f"Video URL is accessible for post {input_post_id}")
        except Exception as e:
            logger.error(f"Video URL is not accessible for post {input_post_id}: {str(e)}")
            return False

        # Setup output path
        filename = generate_filename(data, filename_config, output_dir)
        output_folder = os.path.join(output_dir, data['user']['username'], "videos")
        full_path = os.path.join(output_folder, filename)
        
        # Check existing file
        if os.path.exists(full_path) and os.path.getsize(full_path) > 0:
            if verify_video_file(full_path):
                message = f"File already exists and verified: {filename}"
                logger.info(message)
                if progress_queue:
                    progress_queue.put(message)
                if progress_bar:
                    progress_bar.update(1)
                return True
            else:
                message = f"Corrupted file found, will redownload: {filename}"
                logger.warning(message)
                if progress_queue:
                    progress_queue.put(message)
                os.remove(full_path)

        # Create output directory
        os.makedirs(output_folder, exist_ok=True)

        # Start download
        message = f"Starting download of video {input_post_id}"
        logger.info(message)
        if progress_queue:
            progress_queue.put(message)

        success = DL_File(
            video_url,
            full_path,
            input_post_id,
            progress_queue=progress_queue
        )

        if success:
            message = f"Successfully downloaded video: {filename}"
            logger.info(message)
        else:
            message = f"Failed to download video for post ID {input_post_id}"
            logger.error(message)
        
        if progress_queue:
            progress_queue.put(message)
        if progress_bar:
            progress_bar.update(1)
        
        return success

    except Exception as e:
        error = f"Error processing post {input_post_id}: {str(e)}"
        logger.error(error)
        if progress_queue:
            progress_queue.put(error)
        if progress_bar:
            progress_bar.update(1)
        return False

def download_videos_concurrently(session, post_ids, selected_resolution, output_dir, filename_config, progress_queue=None, max_workers=3):
    # Ändere max_workers auf 1 und stelle sicher, dass wir strikt sequentiell arbeiten
    max_workers = 1  # Override to force sequential downloads
    
    headers = read_headers_from_file("header.txt")
    total_posts = len(post_ids)
    message = f"Starting download of {total_posts} posts strictly one at a time..."
    logger.info(message)
    if progress_queue:
        progress_queue.put(message)
    
    progress_bar = tqdm(total=total_posts, desc="Downloading videos", unit="video")

    # Sequentieller Download statt ThreadPoolExecutor
    for post_id in post_ids:
        try:
            message = f"Processing post ID: {post_id}"
            logger.info(message)
            if progress_queue:
                progress_queue.put(message)
                
            process_post_id(
                post_id,
                session,
                headers,
                selected_resolution,
                output_dir,
                filename_config,
                progress_bar,
                progress_queue
            )
            
            # Warte immer, bis ein Video fertig ist, bevor das nächste beginnt
            time.sleep(1)  # Kleine Pause zwischen Videos
            
        except Exception as e:
            error = f"Error processing post {post_id}: {e}"
            logger.error(error)
            if progress_queue:
                progress_queue.put(error)
        
        progress_bar.update(1)

    progress_bar.close()
    if progress_queue:
        progress_queue.put("Download process completed")

def download_single_file(session, post_id, selected_resolution, output_dir, filename_config):
    headers = read_headers_from_file("header.txt")
    try:
        response = session.get(f"https://api.myfans.jp/api/v2/posts/{post_id}", headers=headers)
        response.raise_for_status()
        process_post_id(post_id, session, headers, selected_resolution, output_dir, filename_config)
    except requests.RequestException as e:
        print(f"API request failed: {e}")

def check_disk_space(path, required_bytes):
    """Check if there's enough disk space available"""
    try:
        stat = os.statvfs(path)
        free_bytes = stat.f_frsize * stat.f_bavail
        return free_bytes >= required_bytes
    except Exception as e:
        logger.error(f"Failed to check disk space: {e}")
        return False

def start_download(username, post_type, download_type, progress_queue, download_state=None, post_id=None, resolution='best'):
    """Handle downloads initiated from the web interface"""
    try:
        if post_id:
            # Single post download
            message = f"Starting download for post ID: {post_id}"
            logger.info(message)
            progress_queue.put(message)
            
            session = requests.Session()
            config_file_path = os.path.join(os.getenv('CONFIG_DIR', ''), 'config.ini')
            
            config = configparser.ConfigParser()
            config.read(config_file_path)
            
            output_dir = os.getenv('DOWNLOADS_DIR', config.get('Settings', 'output_dir'))
            filename_config = read_filename_config(config)
            
            if post_type == 'videos':
                download_single_file(session, post_id, resolution, output_dir, filename_config)
            else:  # images
                headers = read_headers_from_file("header.txt")
                handle_image_download(post_id, session, headers, output_dir, filename_config, progress_queue)
            progress_queue.put("DONE")
            return

        message = f"Starting download for user: {username}, type: {post_type}, mode: {download_type}"
        logger.info(message)
        progress_queue.put(message)
        
        session = requests.Session()
        config_file_path = os.path.join(os.getenv('CONFIG_DIR', ''), 'config.ini')
        
        if not os.path.isfile(config_file_path):
            error = "Error: config.ini not found"
            logger.error(error)
            progress_queue.put(error)
            return
            
        config = configparser.ConfigParser()
        config.read(config_file_path)
        
        # Get configuration
        output_dir = os.getenv('DOWNLOADS_DIR', config.get('Settings', 'output_dir'))
        filename_config = read_filename_config(config)
        
        # Process downloads based on type
        if post_type == 'videos':
            user_info_url = f"https://api.myfans.jp/api/v2/users/show_by_username?username={username}"
            message = f"Fetching user info from: {user_info_url}"
            logger.info(message)
            progress_queue.put(message)
            
            response = session.get(user_info_url, headers=read_headers_from_file("header.txt"))
            response.raise_for_status()
            user_data = response.json()
            
            message = f"Successfully retrieved user data for: {username}"
            logger.info(message)
            progress_queue.put(message)
            
            # Fetch posts
            back_number_plan = user_data.get('current_back_number_plan')
            user_id = user_data.get('id')
            
            if not user_id:
                error = "Failed to retrieve user ID. Please check the username and try again."
                logger.error(error)
                progress_queue.put(error)
                return
                
            message = f"Found user ID: {user_id}"
            logger.info(message)
            progress_queue.put(message)
            
            # Fetch regular posts
            base_url = f"https://api.myfans.jp/api/v2/users/{user_id}/posts?page="
            progress_queue.put("Fetching regular posts...")
            video_posts = []
            page = 1
            
            while True:
                try:
                    message = f"Fetching page {page} of regular posts..."
                    logger.info(message)
                    progress_queue.put(message)
                    
                    response = session.get(base_url + str(page), headers=read_headers_from_file("header.txt"))
                    response.raise_for_status()
                    json_data = response.json()
                    
                    if not json_data.get("data") or len(json_data["data"]) == 0:
                        message = "No more regular posts found"
                        logger.info(message)
                        progress_queue.put(message)
                        break
                        
                    current_page_videos = [post for post in json_data["data"] if post.get("kind") == "video"]
                    video_posts.extend(current_page_videos)
                    
                    message = f"Found {len(current_page_videos)} videos on page {page}"
                    logger.info(message)
                    progress_queue.put(message)
                    
                    page += 1
                    
                except requests.RequestException as e:
                    error = f"Error fetching page {page}: {e}"
                    logger.error(error)
                    progress_queue.put(error)
                    break

            # Fetch back number plan posts if available
            if back_number_plan:
                message = "Starting to fetch back number plan posts..."
                logger.info(message)
                progress_queue.put(message)
                
                back_plan_url = f"https://api.myfans.jp/api/v2/users/{user_id}/back_number_posts?page="
                page = 1
                
                while True:
                    try:
                        message = f"Fetching back plan page {page}..."
                        logger.info(message)
                        progress_queue.put(message)
                        
                        response = session.get(back_plan_url + str(page), headers=read_headers_from_file("header.txt"))
                        response.raise_for_status()
                        json_data = response.json()
                        
                        if not json_data.get("data") or len(json_data["data"]) == 0:
                            message = "No more back plan posts found"
                            logger.info(message)
                            progress_queue.put(message)
                            break
                            
                        current_page_videos = [post for post in json_data["data"] if post.get("kind") == "video"]
                        video_posts.extend(current_page_videos)
                        
                        message = f"Found {len(current_page_videos)} back plan videos on page {page}"
                        logger.info(message)
                        progress_queue.put(message)
                        
                        page += 1
                        
                    except requests.RequestException as e:
                        error = f"Error fetching back plan page {page}: {e}"
                        logger.error(error)
                        progress_queue.put(error)
                        break

            message = f"Total video posts found: {len(video_posts)}"
            logger.info(message)
            progress_queue.put(message)

            # Filter posts based on download_type
            if download_type == 'free':
                filtered_posts = [post for post in video_posts if post.get("free")]
            elif download_type == 'subscribed':
                filtered_posts = [post for post in video_posts if not post.get("free")]
            else:
                filtered_posts = video_posts

            # Check which files already exist
            existing_files, missing_files = check_existing_files(filtered_posts, output_dir, filename_config)

            message = f"Found {len(existing_files)} existing files, {len(missing_files)} files to download"
            logger.info(message)
            progress_queue.put(message)

            if missing_files:
                message = f"Starting download of {len(missing_files)} missing files..."
                logger.info(message)
                progress_queue.put(message)
                download_videos_concurrently(session, missing_files, resolution, output_dir, filename_config, progress_queue)
            else:
                message = "All files already downloaded!"
                logger.info(message)
                progress_queue.put(message)

            progress_queue.put("DONE")

        elif post_type == 'images':
            base_url = f"https://api.myfans.jp/api/v2/users/{user_id}/posts?page="
            progress_queue.put("Fetching image posts...")
            image_posts = []
            page = 1
            
            while True:
                try:
                    message = f"Fetching page {page} of image posts..."
                    logger.info(message)
                    progress_queue.put(message)
                    
                    response = session.get(base_url + str(page), headers=read_headers_from_file("header.txt"))
                    response.raise_for_status()
                    json_data = response.json()
                    
                    if not json_data.get("data"):
                        break
                        
                    current_page_images = [post for post in json_data["data"] if post.get("kind") == "image"]
                    image_posts.extend(current_page_images)
                    
                    message = f"Found {len(current_page_images)} images on page {page}"
                    logger.info(message)
                    progress_queue.put(message)
                    
                    page += 1
                    
                except requests.RequestException as e:
                    error = f"Error fetching page {page}: {e}"
                    logger.error(error)
                    progress_queue.put(error)
                    break

            # Filter posts based on download_type
            if download_type == 'free':
                filtered_posts = [post for post in image_posts if post.get("free")]
            elif download_type == 'subscribed':
                filtered_posts = [post for post in image_posts if not post.get("free")]
            else:
                filtered_posts = image_posts

            message = f"Starting download of {len(filtered_posts)} filtered image posts..."
            logger.info(message)
            progress_queue.put(message)

            post_ids = [post.get("id") for post in filtered_posts]
            download_images_concurrently(session, post_ids, output_dir, filename_config, progress_queue, download_state)

        progress_queue.put("DONE")
        
    except Exception as e:
        error = f"Error: {str(e)}"
        logger.error(error)
        progress_queue.put(error)
        raise

def download_images_concurrently(session, post_ids, output_dir, filename_config, progress_queue=None, download_state=None, max_workers=1):
    headers = read_headers_from_file("header.txt")
    total_posts = len(post_ids)
    message = f"Starting download of {total_posts} image posts one at a time..."
    if progress_queue:
        progress_queue.put(message)
    
    progress_bar = tqdm(total=total_posts, desc="Downloading images", unit="post")

    def handle_image_download(input_post_id):
        try:
            if download_state and download_state.is_completed(input_post_id):
                message = f"Skipping already downloaded image post ID {input_post_id}"
                logger.info(message)
                if progress_queue:
                    progress_queue.put(message)
                progress_bar.update(1)
                return

            url = f"https://api.myfans.jp/api/v2/posts/{input_post_id}"
            response = session.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()
            
            images = data.get('images', {}).get('main', [])
            if not images:
                error = f"No images found for post ID {input_post_id}"
                logger.error(error)
                if progress_queue:
                    progress_queue.put(error)
                if download_state:
                    download_state.mark_failed(input_post_id, error)
                progress_bar.update(1)
                return

            name_creator = data['user']['username']
            output_folder = os.path.join(output_dir, name_creator, "images")
            os.makedirs(output_folder, exist_ok=True)

            for idx, image in enumerate(images):
                image_url = image.get('url')
                if not image_url:
                    continue

                file_name = generate_filename(data, filename_config, output_folder)
                if len(images) > 1:
                    base, ext = os.path.splitext(file_name)
                    file_name = f"{base}_{idx + 1}{ext}"

                full_path = os.path.join(output_folder, file_name)
                
                if os.path.exists(full_path):
                    message = f"Image already exists: {file_name}"
                    logger.info(message)
                    if progress_queue:
                        progress_queue.put(message)
                    continue

                img_response = session.get(image_url, headers=headers)
                img_response.raise_for_status()

                with open(full_path, 'wb') as f:
                    f.write(img_response.content)

                message = f"Downloaded image: {file_name}"
                logger.info(message)
                if progress_queue:
                    progress_queue.put(message)

            if download_state:
                download_state.mark_completed(input_post_id)
            progress_bar.update(1)

        except Exception as e:
            error = f"Error downloading images for post {input_post_id}: {str(e)}"
            logger.error(error)
            if progress_queue:
                progress_queue.put(error)
            if download_state:
                download_state.mark_failed(input_post_id, str(e))
            progress_bar.update(1)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(handle_image_download, post_id) for post_id in post_ids]
        for future in concurrent.futures.as_completed(futures):
            try:
                future.result()
            except Exception as e:
                if progress_queue:
                    progress_queue.put(f"An error occurred during download: {e}")

    progress_bar.close()
    if progress_queue:
        progress_queue.put("Image download process completed")

class DownloadState:
    def __init__(self, state_dir="/config"):
        self.state_file = os.path.join(state_dir, "download_state.json")
        self.state = self._load_state()
        self._cleanup_incomplete()

    def _cleanup_incomplete(self):
        """Check for incomplete downloads and mark them for retry"""
        downloads_dir = os.getenv('DOWNLOADS_DIR', '/downloads')
        for post_id, info in self.state["downloads"].items():
            if info["status"] == "in_progress":
                # Check if the download was interrupted
                temp_folder = os.path.join(downloads_dir, f"{post_id}_parts")
                if os.path.exists(temp_folder):
                    self.state["downloads"][post_id]["status"] = "incomplete"
                    self.state["downloads"][post_id]["segments_downloaded"] = len(
                        [f for f in os.listdir(temp_folder) if f.endswith('.ts')]
                    )
        self.save_state()

    def _load_state(self):
        """Load download state from JSON file"""
        try:
            if os.path.exists(self.state_file):
                with open(self.state_file, 'r') as f:
                    return json.loads(f.read())
            return {"completed_files": [], "downloads": {}}
        except Exception as e:
            logger.error(f"Error loading state file: {e}")
            return {"completed_files": [], "downloads": {}}

    def save_state(self):
        """Save current state to JSON file"""
        try:
            with open(self.state_file, 'w') as f:
                json.dump(self.state, f)
        except Exception as e:
            logger.error(f"Error saving state file: {e}")

    def add_download(self, post_id, segments_total=0):
        """Add new download to state"""
        self.state["downloads"][str(post_id)] = {
            "status": "in_progress",
            "segments_total": segments_total,
            "segments_downloaded": 0
        }
        self.save_state()

    def update_progress(self, post_id, segments_done):
        """Update download progress"""
        if str(post_id) in self.state["downloads"]:
            self.state["downloads"][str(post_id)]["segments_downloaded"] = segments_done
            self.save_state()

    def mark_completed(self, post_id):
        """Mark download as completed"""
        post_id = str(post_id)
        if post_id not in self.state["completed_files"]:
            self.state["completed_files"].append(post_id)
        if post_id in self.state["downloads"]:
            del self.state["downloads"][post_id]
        self.save_state()

    def mark_failed(self, post_id, error):
        """Mark download as failed"""
        self.state["downloads"][str(post_id)] = {
            "status": "failed",
            "error": error
        }
        self.save_state()

    def is_completed(self, post_id):
        """Check if download is already completed"""
        return str(post_id) in self.state["completed_files"]

def get_available_resolutions(main_videos):
    """Get all available resolutions from video data"""
    resolutions = {}
    for video in main_videos:
        res = video.get("resolution")
        if res:
            # Map API resolutions to display names
            res_map = {
                'fhd': '1080p (Full HD)',
                'hd': '720p (HD)',
                'sd': '480p (SD)',
                'ld': '360p (LD)'
            }
            resolutions[res] = res_map.get(res, res)
    
    # Always add 'best' option
    resolutions['best'] = 'Best Available'
    return resolutions

def get_video_info(input_post_id, session, headers):
    try:
        url = f"https://api.myfans.jp/api/v2/posts/{input_post_id}"
        response = session.get(url, headers=headers)
        response.raise_for_status()
        
        data = response.json()
        main_videos = data.get('videos', {}).get('main', [])
        
        if not main_videos:
            logger.error(f"No video content found for post {input_post_id}")
            return None, None, "No videos found"
            
        logger.info(f"Found {len(main_videos)} video variants for post {input_post_id}")
        
        available_resolutions = []
        resolution_info = {}
        
        for video in main_videos:
            res = video.get("resolution")
            if res:
                available_resolutions.append(res)
                resolution_info[res] = {
                    "url": video.get("url"),
                    "size": video.get("size", 0),
                    "duration": video.get("duration", 0)
                }
        
        return data, resolution_info, None
    except requests.RequestException as e:
        logger.error(f"API request failed for post {input_post_id}: {str(e)}")
        return None, None, str(e)
    except Exception as e:
        logger.error(f"Unexpected error for post {input_post_id}: {str(e)}")
        return None, None, str(e)

def handle_image_download(post_id, session, headers, output_dir, filename_config, progress_queue=None):
    """Handle downloading of a single image post"""
    try:
        url = f"https://api.myfans.jp/api/v2/posts/{post_id}"
        response = session.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()
        
        images = data.get('images', {}).get('main', [])
        if not images:
            error = f"No images found for post ID {post_id}"
            logger.error(error)
            if progress_queue:
                progress_queue.put(error)
            return False

        name_creator = data['user']['username']
        output_folder = os.path.join(output_dir, name_creator, "images")
        os.makedirs(output_folder, exist_ok=True)

        for idx, image in enumerate(images):
            image_url = image.get('url')
            if not image_url:
                continue

            file_name = generate_filename(data, filename_config, output_folder)
            if len(images) > 1:
                base, ext = os.path.splitext(file_name)
                file_name = f"{base}_{idx + 1}{ext}"

            full_path = os.path.join(output_folder, file_name)
            
            if os.path.exists(full_path):
                message = f"Image already exists: {file_name}"
                logger.info(message)
                if progress_queue:
                    progress_queue.put(message)
                continue

            img_response = session.get(image_url, headers=headers)
            img_response.raise_for_status()

            with open(full_path, 'wb') as f:
                f.write(img_response.content)

            message = f"Downloaded image: {file_name}"
            logger.info(message)
            if progress_queue:
                progress_queue.put(message)

        return True

    except Exception as e:
        error = f"Error downloading images for post {post_id}: {str(e)}"
        logger.error(error)
        if progress_queue:
            progress_queue.put(error)
        return False

def validate_video_url(url, headers):
    """Validate video URL is accessible"""
    try:
        session = requests.Session()
        session.headers.update(headers)  # Use session with headers
        
        response = session.head(url, allow_redirects=True, timeout=10)
        
        if response.status_code != 200:
            logger.error(f"URL validation failed with status code {response.status_code}")
            return False
            
        content_type = response.headers.get('content-type', '')
        valid_types = ['video', 'application/vnd.apple.mpegurl', 'application/x-mpegurl']
        if not any(t in content_type.lower() for t in valid_types):
            logger.error(f"Invalid content type: {content_type}")
            return False
            
        return True
        
    except Exception as e:
        logger.error(f"URL validation error: {str(e)}")
        return False

def check_existing_files(filtered_posts: List[Dict], output_dir: str, filename_config: Dict) -> Tuple[List[str], List[str]]:
    """
    Check which files already exist and verify their integrity.
    Returns tuple of (existing_files, missing_files) where each is a list of post IDs.
    """
    existing_files = []
    missing_files = []
    
    for post in filtered_posts:
        post_id = post.get('id')
        if not post_id:
            continue
        
        # Get username
        username = post.get('user', {}).get('username', 'unknown')
        output_folder = os.path.join(output_dir, username, "videos")
        
        # Einfach prüfen, ob irgendeine Datei im Ordner den post_id enthält
        found_valid_file = False
        if os.path.exists(output_folder):
            for filename in os.listdir(output_folder):
                if post_id in filename and filename.endswith('.mp4'):
                    full_path = os.path.join(output_folder, filename)
                    if os.path.getsize(full_path) > 0 and verify_video_file(full_path):
                        existing_files.append(post_id)
                        logger.info(f"Found existing verified file for post ID {post_id}: {filename}")
                        found_valid_file = True
                        break
        
        if not found_valid_file:
            missing_files.append(post_id)
            
    return existing_files, missing_files

def generate_filename(post: Dict, filename_config: Dict, output_dir: str) -> str:
    """Generate a unique filename for the video"""
    username = post.get('user', {}).get('username', 'unknown')
    post_id = post.get('id', 'unknown')
    
    # Direkter Zugriff auf die wichtigsten Felder und Logging des vollständigen post-Objekts
    logger.debug(f"Post data for filename generation: {json.dumps(post, default=str)[:2000]}...")

    # Debug: Zeige die verfügbaren Felder im Post
    logger.debug(f"Post fields: {list(post.keys())}")

    # Debug: Zeige den Inhalt bestimmter Felder
    if 'posted_at' in post:
        logger.debug(f"posted_at: {post['posted_at']}")
    if 'created_at' in post:
        logger.debug(f"created_at: {post['created_at']}")
    
    # Extrahiere das Datum - versuche verschiedene Felder
    post_date = None
    
    # 1. Prüfe posted_at
    if post.get('posted_at'):
        try:
            if isinstance(post.get('posted_at'), str) and 'T' in post.get('posted_at'):
                post_date = post.get('posted_at').split('T')[0]
                logger.info(f"Found date in posted_at: {post_date}")
        except Exception as e:
            logger.error(f"Error parsing posted_at: {e}")
    
    # 2. Prüfe created_at
    if not post_date and post.get('created_at'):
        try:
            if isinstance(post.get('created_at'), str) and 'T' in post.get('created_at'):
                post_date = post.get('created_at').split('T')[0]
                logger.info(f"Found date in created_at: {post_date}")
        except Exception as e:
            logger.error(f"Error parsing created_at: {e}")
    
    # 3. Prüfe timestamp
    if not post_date and post.get('timestamp'):
        try:
            from datetime import datetime
            timestamp = post.get('timestamp')
            if isinstance(timestamp, (int, float)):
                date_obj = datetime.fromtimestamp(timestamp)
                post_date = date_obj.strftime('%Y-%m-%d')
                logger.info(f"Found date in timestamp: {post_date}")
        except Exception as e:
            logger.error(f"Error parsing timestamp: {e}")
    
    # 4. Prüfe published_at
    if not post_date and post.get('published_at'):
        try:
            if isinstance(post.get('published_at'), str) and 'T' in post.get('published_at'):
                post_date = post.get('published_at').split('T')[0]
                logger.info(f"Found date in published_at: {post_date}")
        except Exception as e:
            logger.error(f"Error parsing published_at: {e}")
    
    # 5. Notfall: aktuelle Zeit verwenden statt unknown_date
    if not post_date:
        from datetime import datetime
        post_date = datetime.now().strftime('%Y-%m-%d')
        logger.warning(f"No date found in post data, using current date: {post_date}")
    
    # Get title or use part of post ID
    title = post.get('title', '')
    if not title or title.strip() == '':
        title = ""  # Kein Titel - nur ID verwenden
    else:
        title = clean_filename(title)
    
    # Stelle sicher, dass die vollständige Post-ID im Dateinamen enthalten ist
    pattern = filename_config.get('pattern', '{creator}_{date}_{id}')
    if '{id}' not in pattern:
        pattern += '_{id}'  # Füge ID hinzu, wenn nicht im Pattern
    
    filename = pattern.replace('{creator}', username) \
                     .replace('{date}', post_date) \
                     .replace('{title}', title) \
                     .replace('{id}', post_id)
    
    # Stelle sicher, dass die Erweiterung korrekt ist
    if not filename.endswith('.mp4'):
        filename += '.mp4'
    
    logger.info(f"Generated filename for post {post_id}: {filename}")
    return filename

def clean_filename(filename: str) -> str:
    """Clean a string to make it safe for filenames"""
    # Replace problematic characters
    invalid_chars = '<>:"/\\|?*'
    for char in invalid_chars:
        filename = filename.replace(char, '_')
    
    # Remove or replace other problematic characters
    filename = re.sub(r'[\x00-\x1f]', '', filename)
    filename = filename.strip('. ')  # Remove leading/trailing dots and spaces
    
    # Limit length
    max_length = 100
    if len(filename) > max_length:
        name, ext = os.path.splitext(filename)
        filename = name[:max_length-len(ext)] + ext
        
    return filename if filename else 'unnamed'

def read_filename_config(config: configparser.ConfigParser) -> Dict:
    """Read filename configuration from config file"""
    try:
        filename_config = {
            'pattern': config.get('Filename', 'pattern', fallback='{creator}_{date}_{id}'),
            'separator': config.get('Filename', 'separator', fallback='_'),
            'numbers': config.get('Filename', 'numbers', fallback=''),
            'letters': config.get('Filename', 'letters', fallback='')
        }
        return filename_config
    except Exception as e:
        logger.error(f"Error reading filename config: {e}")
        return {
            'pattern': '{creator}_{date}_{id}',
            'separator': '_',
            'numbers': '',
            'letters': ''
        }

def validate_filename_config(filename_config: Dict) -> bool:
    """Validate filename configuration"""
    required_keys = ['pattern', 'separator']
    
    # Check for required keys
    for key in required_keys:
        if key not in filename_config:
            logger.error(f"Missing required key in filename config: {key}")
            return False
            
    # Validate pattern
    pattern = filename_config['pattern']
    required_fields = ['{creator}', '{date}', '{id}']
    
    for field in required_fields:
        if field not in pattern:
            logger.error(f"Missing required field in filename pattern: {field}")
            return False
            
    return True

def main():
    session = requests.Session()
    config_file_path = 'config.ini'

    if os.path.isfile(config_file_path):
        config = configparser.ConfigParser()
        config.read(config_file_path)
        try:
            output_dir = config.get('Settings', 'output_dir')
        except (configparser.NoSectionError, configparser.NoOptionError):
            print("Error: 'output_dir' not found in [Settings] section of config.ini.")
            sys.exit(1)

        try:
            max_workers = config.getint('Threads', 'threads')
            if max_workers < 1:
                print("Error: 'threads' must be a positive integer. Using default value 10.")
                max_workers = 10
        except (configparser.NoSectionError, configparser.NoOptionError, ValueError):
            print("Warning: 'threads' not found or invalid in [Threads] section. Using default value 10.")
            max_workers = 10

    else:
        output_dir = input("Enter the output directory: ")
        if output_dir == '0':
            return
        config = configparser.ConfigParser()
        config['Settings'] = {'output_dir': output_dir}
        config['Filename'] = {
            'pattern': '{creator}_{date}',
            'separator': '_',
            'numbers': '1234',
            'letters': 'FudgeRK'
        }
        config['Threads'] = {'threads': '10'}
        with open(config_file_path, 'w') as configfile:
            config.write(configfile)
        print(f"Created default config.ini with pattern '{config['Filename']['pattern']}' and threads=10.")
        max_workers = 10

    filename_config = read_filename_config(config)
    validate_filename_config(filename_config)

    while True:
        name_creator = input("Enter a creator's username (without @) or type '0' to exit: ")
        if name_creator.lower() == '0':
            sys.exit()
        new_base_url = f"https://api.myfans.jp/api/v2/users/show_by_username?username={name_creator}"
        headers = read_headers_from_file("header.txt")
        try:
            response = requests.get(new_base_url, headers=headers)
            response.raise_for_status()
            new_json_data = response.json()
            user_id = new_json_data.get("id")
            if user_id:
                break
            else:
                print("Failed to retrieve user ID from the API endpoint. Please try again.")
        except requests.RequestException as e:
            print(f"An error occurred while connecting to the API: {e}")

    print("Select an option:")
    print("1. Download all video posts")
    print("2. Download a single video post by ID")
    choice = input("Enter your choice (1/2): ")

    if choice == '1':
        # First get back number plan info
        user_info_url = f"https://api.myfans.jp/api/v2/users/show_by_username?username={name_creator}"  # Changed URL format
        print("Fetching user info and plans...")
        try:
            response = session.get(user_info_url, headers=headers)
            response.raise_for_status()
            user_data = response.json()
            back_number_plan = user_data.get('current_back_number_plan')
            user_id = user_data.get('id')  # Get user_id from the response
            
            if not user_id:
                print("Failed to retrieve user ID. Please check the username and try again.")
                return
            
            # Fetch regular posts
            base_url = f"https://api.myfans.jp/api/v2/users/{user_id}/posts?page="
            print("Fetching regular posts...")
            video_posts = []
            page = 1
            
            with tqdm(desc="Fetching regular posts") as pbar:
                while True:
                    try:
                        response = requests.get(base_url + str(page), headers=headers)
                        response.raise_for_status()
                        json_data = response.json()
                        
                        if not json_data.get("data"):
                            break
                            
                        for post in json_data["data"]:
                            if post.get("kind") == "video":
                                video_posts.append(post)
                        
                        page += 1
                        pbar.update(1)
                        
                    except requests.RequestException as e:
                        print(f"\nError fetching page {page}: {e}")
                        break
            
            # Fetch back number plan posts if available
            if back_number_plan:
                print("\nFetching back number plan posts...")
                back_plan_url = f"https://api.myfans.jp/api/v2/users/{user_id}/back_number_posts?page="
                page = 1
                
                with tqdm(desc="Fetching back plan posts") as pbar:
                    while True:
                        try:
                            response = requests.get(back_plan_url + str(page), headers=headers)
                            response.raise_for_status()
                            json_data = response.json()
                            
                            if not json_data.get("data"):
                                break
                                
                            for post in json_data["data"]:
                                if post.get("kind") == "video":
                                    video_posts.append(post)
                            
                            page += 1
                            pbar.update(1)
                            
                        except requests.RequestException as e:
                            print(f"\nError fetching back plan page {page}: {e}")
                            break
            
            print(f"\nTotal video posts found: {len(video_posts)}")

            print("Select which posts to download:")
            print("1. Free posts only")
            print("2. Subscribe posts only")
            print("3. All posts")
            save_choice = input("Enter your choice (1/2/3): ").strip()

            if save_choice == "1":
                post_ids = [post.get("id") for post in video_posts if post.get("free")]
            elif save_choice == "2":
                post_ids = [post.get("id") for post in video_posts if not post.get("free")]
            else:
                post_ids = [post.get("id") for post in video_posts]

            if not post_ids:
                print("No posts match the selected criteria.")
                return

            selected_resolution = 'fhd'
            download_videos_concurrently(session, post_ids, selected_resolution, output_dir, filename_config)

        except requests.RequestException as e:
            print(f"An error occurred while fetching posts: {e}")

    elif choice == '2':
        post_id = input("Enter the post ID to download: ")
        selected_resolution = 'fhd'
        download_single_file(session, post_id, selected_resolution, output_dir, filename_config)

    else:
        print("Invalid choice.")
        return

if __name__ == "__main__":
    main()