use anyhow::Result;
use benchmark_harness::{Args, BenchmarkImplementation};
use zenwebp::{EncodeRequest, LosslessConfig, PixelLayout};

struct ZenwebpLosslessEncodeBench;

struct BenchContext {
    rgb: Vec<u8>,
    width: u32,
    height: u32,
    quality: f32,
    method: u8,
}

impl BenchmarkImplementation for ZenwebpLosslessEncodeBench {
    fn name(&self) -> &'static str {
        "zenwebp-lossless-encode"
    }

    fn prepare(&self, args: &Args) -> Result<Box<dyn std::any::Any>> {
        let (width, height, rgb) = benchmark_harness::decode_ppm_rgb8(&args.input)?;
        // For lossless VP8L, both quality (entropy-reduction effort) and method
        // affect size; the sweep steps `method` as the effort axis.
        let quality = args.param_f32("quality", 100.0).clamp(0.0, 100.0);
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

        let config = LosslessConfig::new()
            .with_quality(ctx.quality)
            .with_method(ctx.method);
        EncodeRequest::lossless(&config, &ctx.rgb, PixelLayout::Rgb8, ctx.width, ctx.height)
            .encode()
            .map_err(|e| anyhow::anyhow!("zenwebp lossless encode failed: {e}"))
    }
}

fn main() -> Result<()> {
    benchmark_harness::main(ZenwebpLosslessEncodeBench)
}
