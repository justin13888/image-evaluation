#!/bin/bash
# DEPRECATED: This script is superseded by `./bench setup`.
# Data setup now happens automatically on `./bench perf`/`./bench quality`, or explicitly via:
#   ./bench setup              # all datasets
#   ./bench setup -d kodak     # specific dataset
#   ./bench setup --force      # force re-download/regenerate
#   ./bench setup --verify-only  # integrity check only
# This script remains functional but will be removed in a future release.
echo "Warning: setup_data.sh is deprecated. Use './bench setup' instead." >&2
echo "" >&2
set -e
# set -x
mkdir -p data data/kodak data/div2k data/pathological

echo "=== Image Evaluation Data Setup ==="
echo

# ============================================================================
# Dependency Checks
# ============================================================================
echo "Checking dependencies..."
MISSING_DEPS=()

command -v wget &> /dev/null || MISSING_DEPS+=("wget")
command -v unzip &> /dev/null || MISSING_DEPS+=("unzip")
command -v convert &> /dev/null || MISSING_DEPS+=("imagemagick (convert)")
command -v uv &> /dev/null || MISSING_DEPS+=("uv")

if [ ${#MISSING_DEPS[@]} -ne 0 ]; then
    echo "Error: Missing required dependencies:"
    for dep in "${MISSING_DEPS[@]}"; do
        echo "  - $dep"
    done
    echo
    echo "Install with:"
    echo "  sudo apt install wget unzip imagemagick  # Debian/Ubuntu"
    echo "  curl -LsSf https://astral.sh/uv/install.sh | sh  # uv"
    exit 1
fi
echo "  ✓ All required dependencies found"
echo

TARGET="${1:-all}"

# ============================================================================
# 1. KODAK Dataset (Standard - L2/L3 Cache Resident)
# ============================================================================
if [[ "$TARGET" == "all" || "$TARGET" == "kodak" ]]; then
    echo "[1/4] Downloading KODAK dataset..."
    if [ ! -d "data/kodak" ] || [ -z "$(ls -A data/kodak 2>/dev/null)" ]; then
        mkdir -p data/kodak
        for i in $(seq -f "%02g" 1 24); do
            if [ ! -f "data/kodak/kodim${i}.png" ]; then
                wget -q "http://r0k.us/graphics/kodak/kodak/kodim${i}.png" -O "data/kodak/kodim${i}.png" || {
                    echo "Error: Failed to download kodim${i}.png"
                    exit 1
                }
            fi
        done
        echo "  ✓ KODAK dataset ready (24 images)"
    else
        echo "  ✓ KODAK dataset already exists"
    fi
fi

# ============================================================================
# 2. DIV2K Dataset (High-Res - Memory Bound)
# ============================================================================
if [[ "$TARGET" == "all" || "$TARGET" == "div2k" ]]; then
    echo "[2/4] Setting up DIV2K dataset..."
    if [ ! -f "data/div2k/DIV2K_train_HR.zip" ]; then
        echo "  Downloading DIV2K training set (takes a while, ~3.5GB)..."
        mkdir -p data/div2k
        wget -q --show-progress "http://data.vision.ee.ethz.ch/cvl/DIV2K/DIV2K_train_HR.zip" -O "data/div2k/DIV2K_train_HR.zip" || {
            echo "Error: Failed to download DIV2K dataset"
            exit 1
        }
        echo "  ✓ DIV2K downloaded"
    fi

    if [ ! -d "data/div2k/DIV2K_train_HR" ]; then
        unzip -q data/div2k/DIV2K_train_HR.zip -d data/div2k/ || {
            echo "Error: Failed to extract DIV2K dataset"
            exit 1
        }
        echo "  ✓ DIV2K extracted"
    else
        echo "  ✓ DIV2K already extracted"
    fi

    # Select diverse images using the selection script
    if [ ! -f "data/div2k/selected.txt" ]; then
        echo "  Selecting 20 diverse images..."
        uv run scripts/select_div2k.py
    else
        echo "  ✓ DIV2K selection already exists"
    fi
fi

# ============================================================================
# 3. Pathological Test Cases
# ============================================================================
if [[ "$TARGET" == "all" || "$TARGET" == "pathological" ]]; then
    echo "[3/4] Generating pathological test cases..."

    # Solid color (4K)
    if [ ! -f "data/pathological/solid_4k.png" ]; then
        magick -size 3840x2160 xc:"#4287f5" data/pathological/solid_4k.png
        echo "  ✓ solid_4k.png (tests RLE/skip optimizations)"
    else
        echo "  ✓ solid_4k.png already exists"
    fi

    # Gaussian noise (4K)
    if [ ! -f "data/pathological/noise_4k.png" ]; then
        magick -size 3840x2160 xc: +noise Gaussian data/pathological/noise_4k.png
        echo "  ✓ noise_4k.png (worst-case for compressors)"
    else
        echo "  ✓ noise_4k.png already exists"
    fi

    # Screenshot with text and flat regions (4K)
    if [ ! -f "data/pathological/screenshot_4k.png" ]; then
        magick -size 3840x2160 xc:white \
            -fill "#2d2d2d" -draw "rectangle 0,0 3840,100" \
            -fill "#f5f5f5" -draw "rectangle 0,100 800,2160" \
            -fill white -draw "rectangle 800,100 3840,2160" \
            -fill black -pointsize 72 -annotate +100+60 "File Edit View Help" \
            -fill "#666666" -pointsize 48 -annotate +850+500 "Lorem ipsum dolor sit amet" \
            -fill black -pointsize 36 -annotate +850+600 "const foo = 'bar';" \
            data/pathological/screenshot_4k.png
        echo "  ✓ screenshot_4k.png (UI screenshot simulation)"
    else
        echo "  ✓ screenshot_4k.png already exists"
    fi

    # Alpha gradient (4K)
    if [ ! -f "data/pathological/alpha_gradient_4k.png" ]; then
        magick -size 3840x2160 gradient: \
            -size 3840x2160 gradient:"rgba(255,0,0,0)-rgba(0,0,255,255)" \
            -compose over -composite \
            data/pathological/alpha_gradient_4k.png
        echo "  ✓ alpha_gradient_4k.png (transparency gradient)"
    else
        echo "  ✓ alpha_gradient_4k.png already exists"
    fi
fi

# ============================================================================
# 4. Reference Encodings (for decode benchmarks)
# ============================================================================
if [[ "$TARGET" == "all" || "$TARGET" == "reference" ]]; then
    echo "[4/4] Generating reference encodings..."

    # Helper function to generate variants
    generate_variants() {
        local input="$1"
        local output_base="$2"
        
        # Skip if input doesn't exist
        if [ ! -f "$input" ]; then
            echo "Warning: Input file $input not found, skipping variants"
            return
        fi

        echo "  Processing $(basename "$input")..."

        # JPEG
        if [ ! -f "${output_base}_web-low.jpg" ]; then
            magick "$input" -quality 50 -sampling-factor 4:2:0 "${output_base}_web-low.jpg"
        fi
        if [ ! -f "${output_base}_web-high.jpg" ]; then
            magick "$input" -quality 80 -interlace Plane "${output_base}_web-high.jpg"
        fi
        if [ ! -f "${output_base}_archival.jpg" ]; then
            magick "$input" -quality 95 -sampling-factor 4:4:4 "${output_base}_archival.jpg"
        fi

        # WebP
        if command -v cwebp &> /dev/null; then
            if [ ! -f "${output_base}_web-low.webp" ]; then
                cwebp -q 50 -m 4 "$input" -o "${output_base}_web-low.webp" -quiet
            fi
            if [ ! -f "${output_base}_web-high.webp" ]; then
                cwebp -q 75 -m 4 "$input" -o "${output_base}_web-high.webp" -quiet
            fi
            if [ ! -f "${output_base}_archival.webp" ]; then
                cwebp -lossless -z 6 "$input" -o "${output_base}_archival.webp" -quiet
            fi
        fi

        # AVIF
        if command -v avifenc &> /dev/null; then
            # avifenc prefers PNG/JPEG input. If input is PPM, convert to PNG temp
            local avif_input="$input"
            local temp_png=""
            
            if [[ "$input" == *.ppm ]]; then
                temp_png="${input%.*}.temp.png"
                magick "$input" "$temp_png"
                avif_input="$temp_png"
            fi

            if [ ! -f "${output_base}_web-low.avif" ]; then
                avifenc -q 65 -s 6 "$avif_input" "${output_base}_web-low.avif" >/dev/null 2>&1
            fi
            if [ ! -f "${output_base}_web-high.avif" ]; then
                avifenc -q 65 "$avif_input" "${output_base}_web-high.avif" >/dev/null 2>&1
            fi
            if [ ! -f "${output_base}_archival.avif" ]; then
                avifenc -q 85 -y 444 "$avif_input" "${output_base}_archival.avif" >/dev/null 2>&1
            fi
            
            if [ -n "$temp_png" ] && [ -f "$temp_png" ]; then
                rm "$temp_png"
            fi
        fi

        # JXL
        if command -v cjxl &> /dev/null; then
            if [ ! -f "${output_base}_web-low.jxl" ]; then
                cjxl "$input" "${output_base}_web-low.jxl" -d 4.0 -e 7 >/dev/null 2>&1
            fi
            if [ ! -f "${output_base}_web-high.jxl" ]; then
                cjxl "$input" "${output_base}_web-high.jxl" -d 1.0 -e 7 >/dev/null 2>&1
            fi
            if [ ! -f "${output_base}_archival.jxl" ]; then
                cjxl "$input" "${output_base}_archival.jxl" -d 0 >/dev/null 2>&1
            fi
        fi
    }

    # 1. Test Dataset
    if [ ! -f "data/test.ppm" ]; then
        # Generate 8-bit PPM (all PPM files are normalized to 8-bit depth)
        magick -size 1024x1024 xc: +noise Random -depth 8 data/test.ppm
    fi
    # Ensure test.png exists for PNG benchmarks
    if [ ! -f "data/test.png" ]; then
        magick data/test.ppm data/test.png
    fi
    generate_variants "data/test.ppm" "data/test"

    # 2. Kodak Dataset
    echo "  Generating Kodak variants..."
    for img in data/kodak/*.png; do
        [ -e "$img" ] || continue
        base="${img%.*}"
        generate_variants "$img" "$base"
    done

    # 3. Pathological Dataset
    echo "  Generating Pathological variants..."
    for img in data/pathological/*.png; do
        [ -e "$img" ] || continue
        base="${img%.*}"
        generate_variants "$img" "$base"
    done

    # 4. DIV2K Dataset (Selected only)
    echo "  Generating DIV2K variants (selected subset)..."
    if [ -f "data/div2k/selected.txt" ]; then
        while IFS= read -r filename; do
            [ -z "$filename" ] && continue
            img="data/div2k/DIV2K_train_HR/$filename"
            if [ -f "$img" ]; then
                base="${img%.*}"
                generate_variants "$img" "$base"
            fi
        done < "data/div2k/selected.txt"
    fi
fi

echo
echo "✓ Setup complete! Core datasets ready:"
echo "  - data/kodak/          : 24 KODAK images (~0.4MP)"
echo "  - data/div2k/          : DIV2K dataset + 20 selected (2K/4K)"
echo "  - data/pathological/   : 4 synthetic stress tests"
echo "  - data/test*.{jpg,png} : JPEG and PNG reference encodings"
