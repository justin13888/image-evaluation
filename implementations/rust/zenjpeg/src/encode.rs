use anyhow::Result;
use benchmark_harness::{Args, BenchmarkImplementation};
use zenjpeg::encoder::{ChromaSubsampling, EncoderConfig, PixelLayout};

struct ZenjpegEncodeBench;

struct BenchContext {
    rgb: Vec<u8>,
    width: u32,
    height: u32,
    quality: u8,
    progressive: bool,
    subsampling: ChromaSubsampling,
}

// NOTE on XYB: zenjpeg exposes EncoderConfig::xyb() (jpegli's headline mode), but
// its XYB output does not round-trip to correct sRGB through any decoder wired
// into this harness -- jpegli's decoder rejects zenjpeg's XYB bitstream, and
// zenjpeg's own decoder does not invert XYB->sRGB via the buffered decode API, so
// the scored PPM is garbage. Rather than ship an unscoreable variant, zenjpeg is
// benchmarked in YCbCr only; jpegli carries the XYB comparison. See
// docs/zen-integration.md.

impl BenchmarkImplementation for ZenjpegEncodeBench {
    fn name(&self) -> &'static str {
        "zenjpeg-encode"
    }

    fn prepare(&self, args: &Args) -> Result<Box<dyn std::any::Any>> {
        let (width, height, rgb) = benchmark_harness::decode_ppm_rgb8(&args.input)?;
        let quality = args.param_u32("quality", 80).clamp(1, 100) as u8;
        let progressive = args.param_bool("progressive", true);
        // 4:4:4 (None) / 4:2:2 (HalfHorizontal) / 4:4:0 (HalfVertical) / 4:2:0 (Quarter).
        let subsampling = match args.param_str("subsampling", "420").as_str() {
            "444" => ChromaSubsampling::None,
            "422" => ChromaSubsampling::HalfHorizontal,
            "440" => ChromaSubsampling::HalfVertical,
            _ => ChromaSubsampling::Quarter,
        };
        Ok(Box::new(BenchContext {
            rgb,
            width,
            height,
            quality,
            progressive,
            subsampling,
        }))
    }

    fn run(&self, _args: &Args, context: &mut dyn std::any::Any) -> Result<Vec<u8>> {
        let ctx = context
            .downcast_ref::<BenchContext>()
            .expect("Invalid context");

        let config =
            EncoderConfig::ycbcr(ctx.quality, ctx.subsampling).progressive(ctx.progressive);
        config
            .encode_bytes(&ctx.rgb, ctx.width, ctx.height, PixelLayout::Rgb8Srgb)
            .map_err(|e| anyhow::anyhow!("zenjpeg encode failed: {e}"))
    }
}

fn main() -> Result<()> {
    benchmark_harness::main(ZenjpegEncodeBench)
}
