use anyhow::Result;
use benchmark_harness::{Args, BenchmarkImplementation};

struct ZenjxlLosslessEncodeBench;

struct BenchContext {
    rgb: Vec<u8>,
    width: u32,
    height: u32,
}

impl BenchmarkImplementation for ZenjxlLosslessEncodeBench {
    fn name(&self) -> &'static str {
        "zenjxl-lossless-encode"
    }

    fn prepare(&self, args: &Args) -> Result<Box<dyn std::any::Any>> {
        let (width, height, rgb) = benchmark_harness::decode_ppm_rgb8(&args.input)?;
        Ok(Box::new(BenchContext { rgb, width, height }))
    }

    fn run(&self, _args: &Args, context: &mut dyn std::any::Any) -> Result<Vec<u8>> {
        let ctx = context
            .downcast_ref::<BenchContext>()
            .expect("Invalid context");

        // Modular (lossless) JXL; the convenience encoder picks its default effort.
        zenjxl::encode_rgb8_lossless(&ctx.rgb, ctx.width, ctx.height)
            .map_err(|e| anyhow::anyhow!("zenjxl lossless encode failed: {e}"))
    }
}

fn main() -> Result<()> {
    benchmark_harness::main(ZenjxlLosslessEncodeBench)
}
