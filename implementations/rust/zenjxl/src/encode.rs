use anyhow::Result;
use benchmark_harness::{Args, BenchmarkImplementation};

struct ZenjxlEncodeBench;

struct BenchContext {
    rgb: Vec<u8>,
    width: u32,
    height: u32,
    distance: f32,
}

impl BenchmarkImplementation for ZenjxlEncodeBench {
    fn name(&self) -> &'static str {
        "zenjxl-encode"
    }

    fn prepare(&self, args: &Args) -> Result<Box<dyn std::any::Any>> {
        let (width, height, rgb) = benchmark_harness::decode_ppm_rgb8(&args.input)?;
        // Butteraugli distance: 0 = lossless, ~1.0 = visually lossless, higher =
        // lower quality. Effort uses the convenience encoder's default.
        let distance = args.param_f32("distance", 1.0).clamp(0.0, 25.0);
        Ok(Box::new(BenchContext {
            rgb,
            width,
            height,
            distance,
        }))
    }

    fn run(&self, _args: &Args, context: &mut dyn std::any::Any) -> Result<Vec<u8>> {
        let ctx = context
            .downcast_ref::<BenchContext>()
            .expect("Invalid context");

        zenjxl::encode_rgb8(&ctx.rgb, ctx.width, ctx.height, ctx.distance)
            .map_err(|e| anyhow::anyhow!("zenjxl encode failed: {e}"))
    }
}

fn main() -> Result<()> {
    benchmark_harness::main(ZenjxlEncodeBench)
}
