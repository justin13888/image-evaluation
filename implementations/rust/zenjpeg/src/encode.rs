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

impl BenchmarkImplementation for ZenjpegEncodeBench {
    fn name(&self) -> &'static str {
        "zenjpeg-encode"
    }

    fn prepare(&self, args: &Args) -> Result<Box<dyn std::any::Any>> {
        let (width, height, rgb) = benchmark_harness::decode_ppm_rgb8(&args.input)?;
        let quality = args.param_u32("quality", 80).clamp(1, 100) as u8;
        let progressive = args.param_bool("progressive", true);
        // 4:4:4 (no subsampling) vs 4:2:0 (quarter chroma).
        let subsampling = if args.param_str("subsampling", "420") == "444" {
            ChromaSubsampling::None
        } else {
            ChromaSubsampling::Quarter
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
