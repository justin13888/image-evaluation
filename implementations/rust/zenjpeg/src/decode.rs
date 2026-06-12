use anyhow::{Context, Result};
use benchmark_harness::{Args, BenchmarkImplementation};
use zenjpeg::decoder::Decoder;

struct ZenjpegDecodeBench;

struct BenchContext {
    input_data: Vec<u8>,
}

impl BenchmarkImplementation for ZenjpegDecodeBench {
    fn name(&self) -> &'static str {
        "zenjpeg-decode"
    }

    fn prepare(&self, args: &Args) -> Result<Box<dyn std::any::Any>> {
        let input_data = std::fs::read(&args.input).context("Failed to read input file")?;
        Ok(Box::new(BenchContext { input_data }))
    }

    fn run(&self, _args: &Args, context: &mut dyn std::any::Any) -> Result<Vec<u8>> {
        let ctx = context
            .downcast_ref::<BenchContext>()
            .expect("Invalid context");

        // Default output target is interleaved 8-bit sRGB (PixelFormat::Rgb).
        let result = Decoder::new()
            .decode(&ctx.input_data, zenjpeg::encoder::Unstoppable)
            .map_err(|e| anyhow::anyhow!("zenjpeg decode failed: {e}"))?;
        let pixels = result
            .pixels_u8()
            .context("zenjpeg decode did not yield 8-bit pixels")?;
        benchmark_harness::encode_ppm_rgb8(result.width, result.height, pixels)
    }
}

fn main() -> Result<()> {
    benchmark_harness::main(ZenjpegDecodeBench)
}
