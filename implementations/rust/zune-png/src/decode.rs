use anyhow::{Context, Result};
use benchmark_harness::{Args, BenchmarkImplementation};
use std::fs;
use zune_png::PngDecoder;

struct ZunePngBench;

struct BenchContext {
    input_data: Vec<u8>,
}

impl BenchmarkImplementation for ZunePngBench {
    fn name(&self) -> &'static str {
        "zune-png-decode"
    }

    fn prepare(&self, args: &Args) -> Result<Box<dyn std::any::Any>> {
        let input_data = fs::read(&args.input).context("Failed to read input file")?;

        Ok(Box::new(BenchContext { input_data }))
    }

    fn run(&self, _args: &Args, context: &mut dyn std::any::Any) -> Result<Vec<u8>> {
        let ctx = context
            .downcast_ref::<BenchContext>()
            .expect("Invalid context");
        let mut decoder = PngDecoder::new(std::io::Cursor::new(&ctx.input_data));

        decoder
            .decode_headers()
            .context("Failed to decode headers")?;
        let (w, h) = decoder
            .dimensions()
            .ok_or_else(|| anyhow::anyhow!("Failed to get dimensions"))?;

        let pixels = decoder.decode().context("Failed to decode PNG")?;

        // Normalize to canonical 8-bit RGB so every PNG decoder emits identical
        // output (the bit-exactness comparison basis): truncate 16-bit samples to
        // their high byte (matching libpng's png_set_strip_16 and image's
        // to_rgb8), replicate grayscale to RGB, and drop any alpha channel.
        // zune-png otherwise returns the PNG's native channel layout, which would
        // mismatch encode_ppm_rgb8's w*h*3 expectation (e.g. grayscale fails) and
        // diverge from the other decoders.
        let samples = match pixels {
            zune_png::zune_core::result::DecodingResult::U8(data) => data,
            zune_png::zune_core::result::DecodingResult::U16(data) => {
                data.iter().map(|&v| (v >> 8) as u8).collect()
            }
            _ => anyhow::bail!("Unsupported pixel format"),
        };
        let pixel_count = w
            .checked_mul(h)
            .filter(|&n| n > 0)
            .context("invalid image dimensions")?;
        if samples.is_empty() || samples.len() % pixel_count != 0 {
            anyhow::bail!("unexpected sample count {} for {w}x{h}", samples.len());
        }
        let rgb = to_canonical_rgb8(&samples, samples.len() / pixel_count)?;
        benchmark_harness::encode_ppm_rgb8(w as u32, h as u32, &rgb)
    }
}

/// Expand interleaved samples to canonical 8-bit RGB so this decoder matches the
/// others byte-for-byte: grayscale (1ch) and gray+alpha (2ch) replicate the luma
/// to all three channels, RGB (3ch) passes through, and RGBA (4ch) drops alpha.
fn to_canonical_rgb8(samples: &[u8], channels: usize) -> Result<Vec<u8>> {
    let mut out = Vec::with_capacity(samples.len() / channels * 3);
    match channels {
        3 => out.extend_from_slice(samples),
        4 => samples
            .chunks_exact(4)
            .for_each(|c| out.extend_from_slice(&c[..3])),
        1 => samples
            .iter()
            .for_each(|&g| out.extend_from_slice(&[g, g, g])),
        2 => samples
            .chunks_exact(2)
            .for_each(|c| out.extend_from_slice(&[c[0], c[0], c[0]])),
        n => anyhow::bail!("unsupported channel count {n}"),
    }
    Ok(out)
}

fn main() -> Result<()> {
    benchmark_harness::main(ZunePngBench)
}
