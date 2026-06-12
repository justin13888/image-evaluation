use anyhow::Result;
use benchmark_harness::{Args, BenchmarkImplementation};
use zenwebp::{EncodeRequest, LossyConfig, PixelLayout};

struct ZenwebpEncodeBench;

struct BenchContext {
    rgb: Vec<u8>,
    width: u32,
    height: u32,
    quality: f32,
    method: u8,
}

impl BenchmarkImplementation for ZenwebpEncodeBench {
    fn name(&self) -> &'static str {
        "zenwebp-encode"
    }

    fn prepare(&self, args: &Args) -> Result<Box<dyn std::any::Any>> {
        let (width, height, rgb) = benchmark_harness::decode_ppm_rgb8(&args.input)?;
        let quality = args.param_f32("quality", 75.0).clamp(0.0, 100.0);
        let method = args.param_u32("method", 4).min(6) as u8;
        Ok(Box::new(BenchContext {
            rgb,
            width,
            height,
            quality,
            method,
        }))
    }

    fn run(&self, _args: &Args, context: &mut dyn std::any::Any) -> Result<Vec<u8>> {
        let ctx = context
            .downcast_ref::<BenchContext>()
            .expect("Invalid context");

        let config = LossyConfig::new()
            .with_quality(ctx.quality)
            .with_method(ctx.method);
        EncodeRequest::lossy(&config, &ctx.rgb, PixelLayout::Rgb8, ctx.width, ctx.height)
            .encode()
            .map_err(|e| anyhow::anyhow!("zenwebp lossy encode failed: {e}"))
    }
}

fn main() -> Result<()> {
    benchmark_harness::main(ZenwebpEncodeBench)
}
