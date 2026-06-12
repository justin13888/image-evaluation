use anyhow::{Context, Result};
use benchmark_harness::{Args, BenchmarkImplementation};

struct ZenwebpDecodeBench;

struct BenchContext {
    input_data: Vec<u8>,
}

impl BenchmarkImplementation for ZenwebpDecodeBench {
    fn name(&self) -> &'static str {
        "zenwebp-decode"
    }

    fn prepare(&self, args: &Args) -> Result<Box<dyn std::any::Any>> {
        let input_data = std::fs::read(&args.input).context("Failed to read input file")?;
        Ok(Box::new(BenchContext { input_data }))
    }

    fn run(&self, _args: &Args, context: &mut dyn std::any::Any) -> Result<Vec<u8>> {
        let ctx = context
            .downcast_ref::<BenchContext>()
            .expect("Invalid context");

        // One-shot decode straight to interleaved RGB8.
        let (pixels, width, height) = zenwebp::oneshot::decode_rgb(&ctx.input_data)
            .map_err(|e| anyhow::anyhow!("zenwebp decode failed: {e}"))?;
        benchmark_harness::encode_ppm_rgb8(width, height, &pixels)
    }
}

fn main() -> Result<()> {
    benchmark_harness::main(ZenwebpDecodeBench)
}
