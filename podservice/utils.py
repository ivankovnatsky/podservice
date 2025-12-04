"""Shared utility functions for podservice."""

import logging
import re
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


def sanitize_filename(filename: str) -> str:
    """Sanitize filename to be filesystem-safe."""
    # Remove or replace problematic characters
    filename = re.sub(r'[<>:"/\\|?*]', "", filename)
    # Replace multiple spaces with single space
    filename = re.sub(r"\s+", " ", filename)
    # Trim and limit length
    filename = filename.strip()[:200]
    return filename


def convert_thumbnail_to_jpeg(thumbnail_path: Path) -> Optional[Path]:
    """
    Convert thumbnail to JPEG format for better compatibility.

    Args:
        thumbnail_path: Path to the thumbnail file

    Returns:
        Path to JPEG file, or original path if conversion fails/not needed
    """
    if not HAS_PIL:
        return thumbnail_path

    try:
        # Only convert if not already JPEG
        if thumbnail_path.suffix.lower() in ['.jpg', '.jpeg']:
            return thumbnail_path

        jpeg_path = thumbnail_path.with_suffix('.jpg')

        # Open and convert image
        with Image.open(thumbnail_path) as img:
            # Convert to RGB if necessary (removes alpha channel)
            if img.mode in ('RGBA', 'LA', 'P'):
                rgb_img = Image.new('RGB', img.size, (255, 255, 255))
                if img.mode == 'P':
                    img = img.convert('RGBA')
                rgb_img.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
                img = rgb_img
            elif img.mode != 'RGB':
                img = img.convert('RGB')

            # Save as JPEG with high quality
            img.save(jpeg_path, 'JPEG', quality=95, optimize=True)

        # Delete original file
        thumbnail_path.unlink()
        logger.debug(f"Converted thumbnail to JPEG: {jpeg_path.name}")
        return jpeg_path

    except Exception as e:
        logger.warning(f"Failed to convert thumbnail to JPEG: {e}")
        return thumbnail_path


def download_image(url: str, output_dir: Path, base_filename: str, timeout: int = 30) -> Optional[Path]:
    """
    Download an image from URL and save it to disk.

    Args:
        url: URL of the image to download
        output_dir: Directory to save the image
        base_filename: Base filename (without extension) for the saved image
        timeout: Request timeout in seconds

    Returns:
        Path to downloaded image, or None if download failed
    """
    try:
        logger.debug(f"Downloading image from: {url}")

        response = requests.get(url, timeout=timeout, stream=True)
        response.raise_for_status()

        # Determine file extension from content-type or URL
        content_type = response.headers.get('content-type', '')
        if 'png' in content_type:
            ext = '.png'
        elif 'webp' in content_type:
            ext = '.webp'
        elif 'gif' in content_type:
            ext = '.gif'
        elif 'svg' in content_type:
            ext = '.svg'
        elif 'ico' in content_type or url.endswith('.ico'):
            ext = '.ico'
        else:
            # Default to jpg for jpeg or unknown types
            ext = '.jpg'

        # Also check URL for extension hint
        url_lower = url.lower()
        for check_ext in ['.png', '.webp', '.gif', '.ico', '.svg']:
            if check_ext in url_lower:
                ext = check_ext
                break

        # Create output path
        output_path = output_dir / f"{base_filename}{ext}"

        # Write file
        with open(output_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)

        logger.debug(f"Downloaded image to: {output_path}")

        # Convert to JPEG for better compatibility (except SVG)
        if ext != '.svg' and HAS_PIL:
            output_path = convert_thumbnail_to_jpeg(output_path)

        return output_path

    except requests.RequestException as e:
        logger.warning(f"Failed to download image from {url}: {e}")
        return None
    except Exception as e:
        logger.warning(f"Error processing image from {url}: {e}")
        return None
