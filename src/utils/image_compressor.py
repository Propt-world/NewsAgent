"""
Image Compression Module
Compresses images to 1-2MB in JPEG format while maintaining quality
"""

from PIL import Image
import io
import os


def compress_image(
    input_path,
    output_path=None,
    target_size_mb=1.5,
    min_quality=85,
    max_quality=95
):
    """
    Compress an image to a target size in MB while maintaining quality.
    
    Args:
        input_path (str): Path to the input image file
        output_path (str, optional): Path to save compressed image. 
                                     If None, overwrites input file.
        target_size_mb (float): Target size in MB (default: 1.5MB)
        min_quality (int): Minimum JPEG quality to try (default: 85)
        max_quality (int): Maximum JPEG quality to start with (default: 95)
    
    Returns:
        dict: Information about compression result including:
              - success (bool)
              - original_size_mb (float)
              - compressed_size_mb (float)
              - compression_ratio (float)
              - output_path (str)
              - quality_used (int)
              - dimensions (tuple)
    """
    
    try:
        # Open the image
        img = Image.open(input_path)
        original_size = os.path.getsize(input_path)
        original_size_mb = original_size / (1024 * 1024)
        
        # Convert to RGB if necessary (for PNG with transparency, etc.)
        if img.mode in ('RGBA', 'LA', 'P'):
            # Create a white background
            background = Image.new('RGB', img.size, (255, 255, 255))
            if img.mode == 'P':
                img = img.convert('RGBA')
            background.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
            img = background
        elif img.mode != 'RGB':
            img = img.convert('RGB')
        
        original_dimensions = img.size
        target_size_bytes = target_size_mb * 1024 * 1024
        
        # Set output path
        if output_path is None:
            output_path = input_path
        
        # Try compression with different quality levels first
        quality = max_quality
        best_result = None
        
        while quality >= min_quality:
            buffer = io.BytesIO()
            img.save(
                buffer,
                format='JPEG',
                quality=quality,
                optimize=True,
                progressive=True
            )
            size = buffer.tell()
            
            # If size is within target (with 10% tolerance on upper end)
            if size <= target_size_bytes * 1.1:
                best_result = {
                    'buffer': buffer,
                    'size': size,
                    'quality': quality,
                    'dimensions': img.size
                }
                break
            
            quality -= 5
        
        # If quality adjustment alone didn't work, try resizing
        if best_result is None:
            scale_factor = 0.9
            current_img = img.copy()
            quality = max_quality
            
            while scale_factor >= 0.5:  # Don't resize below 50%
                new_width = int(original_dimensions[0] * scale_factor)
                new_height = int(original_dimensions[1] * scale_factor)
                resized_img = current_img.resize(
                    (new_width, new_height),
                    Image.Resampling.LANCZOS
                )
                
                buffer = io.BytesIO()
                resized_img.save(
                    buffer,
                    format='JPEG',
                    quality=quality,
                    optimize=True,
                    progressive=True
                )
                size = buffer.tell()
                
                if size <= target_size_bytes * 1.1:
                    best_result = {
                        'buffer': buffer,
                        'size': size,
                        'quality': quality,
                        'dimensions': resized_img.size
                    }
                    break
                
                scale_factor -= 0.05
        
        # Save the best result
        if best_result:
            with open(output_path, 'wb') as f:
                f.write(best_result['buffer'].getvalue())
            
            compressed_size_mb = best_result['size'] / (1024 * 1024)
            
            return {
                'success': True,
                'original_size_mb': round(original_size_mb, 2),
                'compressed_size_mb': round(compressed_size_mb, 2),
                'compression_ratio': round(original_size_mb / compressed_size_mb, 2),
                'output_path': output_path,
                'quality_used': best_result['quality'],
                'dimensions': best_result['dimensions'],
                'resized': best_result['dimensions'] != original_dimensions
            }
        else:
            return {
                'success': False,
                'error': 'Could not compress to target size',
                'original_size_mb': round(original_size_mb, 2)
            }
    
    except Exception as e:
        return {
            'success': False,
            'error': str(e)
        }


def compress_image_simple(input_path, output_path=None, quality=90):
    """
    Simple compression with fixed quality (faster, less control over size).
    
    Args:
        input_path (str): Path to input image
        output_path (str, optional): Path to output image
        quality (int): JPEG quality (1-100, default: 90)
    
    Returns:
        dict: Compression result information
    """
    try:
        img = Image.open(input_path)
        original_size = os.path.getsize(input_path)
        
        # Convert to RGB if needed
        if img.mode != 'RGB':
            if img.mode in ('RGBA', 'LA', 'P'):
                background = Image.new('RGB', img.size, (255, 255, 255))
                if img.mode == 'P':
                    img = img.convert('RGBA')
                if img.mode in ('RGBA', 'LA'):
                    background.paste(img, mask=img.split()[-1])
                img = background
            else:
                img = img.convert('RGB')
        
        if output_path is None:
            output_path = input_path
        
        # Save with optimization
        img.save(
            output_path,
            'JPEG',
            quality=quality,
            optimize=True,
            progressive=True
        )
        
        compressed_size = os.path.getsize(output_path)
        
        return {
            'success': True,
            'original_size_mb': round(original_size / (1024 * 1024), 2),
            'compressed_size_mb': round(compressed_size / (1024 * 1024), 2),
            'compression_ratio': round(original_size / compressed_size, 2),
            'output_path': output_path
        }
    
    except Exception as e:
        return {
            'success': False,
            'error': str(e)
        }


# Example usage
if __name__ == '__main__':
    import sys
    
    # Check if image path is provided
    if len(sys.argv) < 2:
        print("Image Compressor for News Articles")
        print("=" * 50)
        print("\nUsage:")
        print("  python image_compressor.py <input_image.jpg> [output_image.jpg] [target_size_mb]")
        print("\nExamples:")
        print("  python image_compressor.py news_photo.jpg")
        print("  python image_compressor.py news_photo.jpg compressed.jpg")
        print("  python image_compressor.py news_photo.jpg compressed.jpg 1.5")
        print("\n" + "=" * 50)
        sys.exit(1)
    
    input_path = sys.argv[1]
    output_path = sys.argv[2] if len(sys.argv) > 2 else None
    target_size = float(sys.argv[3]) if len(sys.argv) > 3 else 1.5
    
    # Check if input file exists
    if not os.path.exists(input_path):
        print(f"✗ Error: File '{input_path}' not found!")
        sys.exit(1)
    
    print(f"\nCompressing: {input_path}")
    print(f"Target size: {target_size} MB")
    print("-" * 50)
    
    # Compress the image
    result = compress_image(input_path, output_path, target_size_mb=target_size)
    
    if result['success']:
        print(f"✓ Compression successful!")
        print(f"  Original size: {result['original_size_mb']} MB")
        print(f"  Compressed size: {result['compressed_size_mb']} MB")
        print(f"  Compression ratio: {result['compression_ratio']}x")
        print(f"  Quality used: {result['quality_used']}")
        print(f"  Dimensions: {result['dimensions']}")
        print(f"  Resized: {'Yes' if result['resized'] else 'No'}")
        print(f"  Saved to: {result['output_path']}")
    else:
        print(f"✗ Compression failed: {result.get('error', 'Unknown error')}")
